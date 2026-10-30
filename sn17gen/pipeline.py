"""Per-prompt procedure (sn17miner 0.4.0 ``run_prompt`` / ``_refine``, ported to 404's runtime API).

gen, attempts 0..max_attempts-1 (fixed plan):
  build the request (rp1: after a RECORDED provider_incomplete whose reason is max_output_tokens, the next attempt
  carries the frozen compact line and asks for 32,000 output tokens if recorded spend + its worst case fits the
  cap, else 24,000) -> budget check (spend + worst case <= cap, else stop) -> witness call
  -> recorded provider failure (WitnessError with source "provider", as ``witness_call`` delivers it on either client
     generation): next attempt; any other WitnessError (client / gateway) or other exception propagates to the
     runtime (0.3.0 C1)
  -> reply: repair (pure Python) -> structural check, rejected: next attempt
  -> render the candidate (v1: default preview; v2: the refine views). RenderRejected (the service's 422 = the
     production validator rejected it) is a recorded outcome: next attempt. Anything else the renderer raises
     (RenderUnavailable: transport, 503, a render crash or timeout) propagates to the runtime. Else ACCEPTED.
no accepted candidate: the last scannable candidate that was never rendered is render-checked once (sn17miner's
  final gate); else the fallback module (render-checked; None only if even that is rejected).
refine (mode v2) after an accepted gen candidate: refine request with the gen PNGs exactly as rendered -> budget
  check against the REFINE cap (v2.2: recorded spend + the refine call's worst case <= refine_credit_cap 4,000,000;
  skip if it does not fit) -> one call (f2, or REFINE_RETRY: one retry after a RECORDED retryable provider failure,
  checked the same way) -> repair -> structural check -> render check -> the refined module replaces the gen module
  only if all pass.
Refine retry (0.2.2, REFINE_RETRY): the retry is attempt 1 of the same request (only the tag's attempt digit differs),
made after ``refine_retryable`` or, with REFINE_RETRY, after ``stream_cut`` (an HTTP-200 stream without a terminal
event, counted as provider_incomplete). With REFINE_RETRY every recorded failure whose usage cannot be decoded
(invalid_provider_response) is charged its worst case, so the retry's budget check counts the cut call.
  Gen calls and the rp1 plan are checked against credit_cap (3,000,000) only, so they are exactly v2's.
Service tier (0.2.1): every request of a prompt carries the same ``service_tier`` (Settings.service_tier(pid), a pure
function of sha256(image); none when SERVICE_TIER_FRACTION is 0.0); credits and caps are tier-independent, and the
response's echoed tier is never read.
Client generations (0.3.1, ``witness_call``): every call goes through one function that works with both published
witness-client generations:
  44d2a64 / 92bedb6 (rc6): create() returns a WitnessResult and raises a provider WitnessError for every recorded
    provider failure (this is the shape the classifiers below read);
  29a0d82: create(stream=False) returns an OpenAI Response (status completed / failed / incomplete), raises SDK
    APIStatusError subclasses for HTTP != 200, openai.APIError ``from`` the provider WitnessError for a stream
    ``error`` event, and the decoder's bare ValueError for an undecodable HTTP-200 record (e.g. a stream cut).
``stream`` is never passed to create(): at 29a0d82 stream=True returns an event iterator (ModelStream), and both
generations write "stream": true into the body themselves, so the recorded bytes do not change. A Response whose
status is not "completed", an SDK status error, a stream-error APIError and a ValueError raised inside the client's
decoder (``witness_client.response.completed_response``) become exactly the provider WitnessError rc6 raised for the
same recorded bytes (pure functions of those bytes: replay takes the same branch). Everything else create() raises
(client / gateway WitnessError, ExchangePending, ReplayMismatch, any other ValueError or APIError) propagates.
The reply text is read with ``reply_text`` on both generations.
Where the two generations still differ (both are pure functions of the recorded bytes, so replay on the same
client takes the same branch; tests/test_client_surfaces.py TestKnownDivergences pins each one):
  * the reply text: rc6 reads the text streamed in ``response.output_text.done`` events, 29a0d82 the message items
    of the terminal response's ``output`` (it rebuilds them from the streamed text only when ``output`` is empty).
    They agree whenever the terminal ``output`` carries the streamed message, which is every OpenAI API-mode
    completion; for a record whose ``output`` is non-empty but holds no output_text (only a reasoning item, a
    refusal, an empty message) 29a0d82 reads "" (a rejected output) where rc6 reads the streamed text;
  * a response.failed whose ``error`` is not an object: rc6's decoder raises AttributeError (its runtime then
    reports a client-side recording_unresolved, the prompt stays pending); on 29a0d82 it is a recorded
    provider_failed with no provider code (retryable), accepted as the safer outcome (no real OpenAI record has
    this shape).
Every branch depends on recorded responses, recorded usage and recorded render outcomes only, so a replay that
serves the same records takes the same branches. Wall clocks, host state and exception messages never enter a
request. One diagnostic JSON line goes to stderr (diagnostics only, never an input).
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass, field
from typing import Any

import openai
from witness_client import WitnessError
from witness_client.response import completed_response, provider_error

from . import __version__, views
from .credits import CallUsage, Ledger, credits_str, usage_from_error, worst_credits
from .repair import RepairResult, fallback_module, model_source, repair
from .reqbuild import VARIANT_OUT_CAP, build_gen_params, build_refine_params, check_seed, data_url, prompt_id
from .settings import ACTIVE, Settings
from .views import RenderRejected

__all__ = ["run", "run_detailed", "hit_output_cap", "refine_retryable", "stream_cut", "refine_retry_reason",
           "witness_call", "reply_text"]

CREDIT_FAILURE_CODES = ("insufficient_quota", "credit_balance_exhausted")
# The pinned decoder (witness_client.response.completed_response) raises ValueError(STREAM_CUT_MESSAGE) when an
# HTTP-200 Responses stream decodes event by event but has no response.completed / .failed / .incomplete event; the
# rc6 witness client wraps it as WitnessError(source="provider", code="invalid_provider_response") ``from`` that error
# (29a0d82 raises it bare; ``witness_call`` wraps it the same way).
STREAM_CUT_MESSAGE = "Missing completed OpenAI response"
_DECODER_CODE = getattr(completed_response, "__code__", None)


def _raised_by_decoder(err: BaseException) -> bool:
    """True iff ``err`` was raised while the client's response decoder (``completed_response``, incl. the SSE frame,
    JSON and UTF-8 decoding it runs) was reading recorded provider bytes. Any other ValueError (request validation,
    gateway / transport parsing) is not a recorded outcome."""
    tb = err.__traceback__
    while tb is not None:
        if _DECODER_CODE is not None and tb.tb_frame.f_code is _DECODER_CODE:
            return True
        tb = tb.tb_next
    return False


def witness_call(create: Any, params: dict[str, Any]) -> Any:
    """One witness call on either client generation (module docstring, "Client generations"). Returns the reply
    (rc6 WitnessResult / 29a0d82 completed Response) or raises a WitnessError: a recorded provider failure (source
    "provider") in rc6's exact form, or whatever non-provider failure the client raised. ``params`` keep their
    ``stream`` key (it is part of the frozen request dict and of the worst-case bound); only create() never sees it."""
    call = {k: v for k, v in params.items() if k != "stream"}
    try:
        result = create(**call)
    except WitnessError:
        raise
    except openai.APIStatusError as e:
        # 29a0d82: HTTP != 200 from the recorded status + body; rc6 decoded the same bytes with provider_error
        body = getattr(getattr(e, "response", None), "content", None)
        raise provider_error(e.status_code, body if isinstance(body, bytes) else b"") from None
    except openai.APIError as e:
        # 29a0d82: a stream ``error`` event; the rc6 provider WitnessError is chained as its cause
        cause = e.__cause__
        if isinstance(cause, WitnessError) and cause.source == "provider":
            raise cause from None
        raise
    except ValueError as e:
        if not _raised_by_decoder(e):
            raise
        raise WitnessError("Invalid recorded OpenAI response", source="provider",
                           code="invalid_provider_response") from e
    status = getattr(result, "status", "completed")       # rc6 WitnessResult has no status: always completed
    if status != "completed":
        err = getattr(result, "error", None)
        raise WitnessError(f"OpenAI response {status}; inference was not retried", source="provider",
                           code=f"provider_{status}", provider_code=getattr(err, "code", None) if err else None,
                           provider_message=getattr(err, "message", None) if err else None, response=result)
    return result


def reply_text(result: Any) -> str:
    """The text of a successful reply (rc6 WitnessResult.output_text, 29a0d82 Response.output_text), "" if there is
    none. The SDK property iterates the terminal response's ``output``: for a completed response recorded without an
    ``output`` it raises TypeError, so that record reads as "" (a rejected output, the next attempt), as rc6 reads
    it, instead of an exception escaping generate(). Every readable reply returns exactly the text it returned before
    (normal path unchanged)."""
    try:
        text = result.output_text
    except (AttributeError, TypeError):
        return ""
    return text or ""


def hit_output_cap(err: BaseException) -> bool:
    """True for a RECORDED ``provider_incomplete`` whose recorded reason is ``max_output_tokens`` (retry policy rp1).
    Reads only the decoded recorded response, which replay serves byte-identically."""
    if getattr(err, "source", None) != "provider" or getattr(err, "code", None) != "provider_incomplete":
        return False
    resp = getattr(err, "response", None)
    details = getattr(resp, "incomplete_details", None) if resp is not None else None
    if details is None and isinstance(resp, dict):
        details = resp.get("incomplete_details")
    reason = details.get("reason") if isinstance(details, dict) else getattr(details, "reason", None)
    return reason == "max_output_tokens"


def refine_retryable(err: BaseException) -> bool:
    """Refine f2: ONE retry after a RECORDED provider failure that a retry may cure: HTTP 5xx, a stream ``error``
    event on HTTP 200 (e.g. server_is_overloaded), response.failed or response.incomplete. Never after a provider
    rejection (400/401/403/404/429), an unparseable record, or a credit/quota failure (witness-client rc4 exposes
    the nested stream error code as ``provider_code``)."""
    if getattr(err, "source", None) != "provider":
        return False
    if getattr(err, "provider_code", None) in CREDIT_FAILURE_CODES:
        return False
    code = getattr(err, "code", None)
    if code in ("provider_failed", "provider_incomplete"):
        return True
    if code == "provider_error":
        status = getattr(err, "status_code", None)
        return type(status) is int and (status == 200 or 500 <= status <= 599)
    return False


def stream_cut(err: BaseException) -> bool:
    """REFINE_RETRY (0.2.2): True for a RECORDED HTTP-200 stream that ended without a terminal event (VPS_HOST.md F3:
    the stream stopped after the reasoning items, no response.completed). The witness client reports it as
    ``invalid_provider_response`` chained from the pinned decoder's ValueError(STREAM_CUT_MESSAGE); it is counted as
    ``provider_incomplete``. Only that exact decoder outcome counts: every other undecodable record (a broken JSON
    frame, bad UTF-8, duplicate or inconsistent events, ...) stays an invalid response and is never retried. A pure
    function of the recorded bytes (the decoder is pinned), so replay takes the same branch; if a runtime does not
    chain the decoder error, this is False and the prompt keeps 0.2.1's outcome (no retry)."""
    if getattr(err, "source", None) != "provider" or getattr(err, "code", None) != "invalid_provider_response":
        return False
    cause = getattr(err, "__cause__", None)
    return type(cause) is ValueError and cause.args == (STREAM_CUT_MESSAGE,)


def refine_retry_reason(err: BaseException, s: Settings) -> str | None:
    """Why the refine call may be retried once after ``err`` (None = no retry): f2's rule, plus a stream cut when
    ``s.refine_retry``. The attempt limit and the budget are checked by the caller."""
    if refine_retryable(err):
        return str(getattr(err, "code", None))
    if s.refine_retry and stream_cut(err):
        return "stream_cut"
    return None


def _usage_unknown(err: BaseException) -> bool:
    """A recorded failure whose provider usage cannot be decoded: the call was dispatched (and may be billed), so
    REFINE_RETRY charges it its worst case."""
    return getattr(err, "source", None) == "provider" and getattr(err, "code", None) == "invalid_provider_response"


@dataclass
class Candidate:
    step: str
    attempt: int
    rep: RepairResult
    text: str
    verdict: str | None = None          # None = never rendered | "ok" | "rejected:<rule>"
    pngs: list[bytes] = field(default_factory=list)


class _Prompt:
    def __init__(self, image: bytes, seed: int, witness: Any, renderer: Any, s: Settings) -> None:
        if not isinstance(image, (bytes, bytearray)) or not image:
            raise TypeError("image must be non-empty bytes")
        self.s = s
        self.image = bytes(image)
        self.seed = check_seed(seed)
        self.pid = prompt_id(self.image)
        self.url = data_url(self.image)
        self.witness = witness
        self.renderer = renderer
        self.ledger = Ledger(credit_cap=s.credit_cap, call_cap=s.call_cap)
        self.cands: list[Candidate] = []
        self.events: list[dict[str, Any]] = []
        self.final = "pending"

    # ------------------------------------------------------------------------------------------- helpers
    def _event(self, **kw: Any) -> None:
        self.events.append(kw)

    def _call(self, params: dict[str, Any], step: str, attempt: int,
              cap: int | None = None) -> tuple[Any, BaseException | None, bool]:
        """Budget-checked witness call (``cap`` None = the gen cap ``credit_cap``). Returns (result, recorded error,
        made)."""
        worst = worst_credits(params)
        if not self.ledger.allows(worst, cap):
            self._event(step=step, attempt=attempt, outcome="budget_stop", worst=credits_str(worst),
                        spent=credits_str(self.ledger.spent), cap=self.ledger.credit_cap if cap is None else cap)
            return None, None, False
        try:
            result = witness_call(self.witness.responses.create, params)
        except WitnessError as e:
            if e.source != "provider":
                # a client / gateway failure (transport, capture_failed, ExchangePending, budget refusal) is not a
                # recorded model outcome: the runtime owns it (recovery / resume), as in 404's reference (C1). Going on
                # would pay for more calls and leave a prefix that no resume can match
                raise
            usage = CallUsage(known=False) if self.s.refine_retry and _usage_unknown(e) else usage_from_error(e)
            cost = self.ledger.add(params["model"], usage, worst)
            self._event(step=step, attempt=attempt, outcome=f"recorded_failure:{e.code}", status=e.status_code,
                        provider_code=getattr(e, "provider_code", None), credits=credits_str(cost))
            return None, e, True
        cost = self.ledger.add(params["model"], CallUsage.from_usage(getattr(result, "usage", None)), worst)
        self._event(step=step, attempt=attempt, outcome="reply", credits=credits_str(cost),
                    max_output_tokens=params["max_output_tokens"])
        return result, None, True

    def _plan_gen(self, attempt: int, variant: str | None) -> dict[str, Any]:
        s = self.s
        if variant is None:
            return build_gen_params(s, self.pid, self.seed, self.url, attempt)
        params: dict[str, Any] = {}
        for cap in (s.retry_max_output_tokens, s.max_output_tokens):
            params = build_gen_params(s, self.pid, self.seed, self.url, attempt, variant=variant, max_output_tokens=cap)
            if self.ledger.allows(worst_credits(params)):
                break
        return params   # if neither fits, _call reports budget_stop

    def _repair(self, text: str) -> RepairResult:
        return repair(text, fit_wrapper=True, fit_target_milli=self.s.fit_target_milli, version=self.s.repair_version)

    # ------------------------------------------------------------------------------------------- steps
    def gen(self) -> Candidate | None:
        s = self.s
        variant: str | None = None
        for attempt in range(s.max_attempts):
            params = self._plan_gen(attempt, variant)
            result, err, made = self._call(params, "gen", attempt)
            if not made:
                return None
            if err is not None:
                variant = VARIANT_OUT_CAP if hit_output_cap(err) else None
                continue
            variant = None
            text = reply_text(result)
            cand = Candidate("gen", attempt, self._repair(text), text)
            self.cands.append(cand)
            if not cand.rep.ok:
                self._event(step="gen", attempt=attempt, outcome="rejected_output", reasons=cand.rep.reasons[:4])
                continue
            try:
                cand.pngs = views.previews(self.renderer, cand.rep.js or "", s.refine_views) if s.refine \
                    else [views.check(self.renderer, cand.rep.js or "")]
            except RenderRejected as r:
                stage, rule = views.rejection_info(r)
                cand.verdict = f"rejected:{rule}"
                self._event(step="gen", attempt=attempt, outcome="render_rejected", stage=stage, rule=rule)
                continue
            cand.verdict = "ok"
            self._event(step="gen", attempt=attempt, outcome="accepted", js_bytes=len((cand.rep.js or "").encode()))
            return cand
        return None

    def leftover(self) -> str | None:
        """sn17miner's final gate for a prompt without an accepted candidate: the last scannable candidate."""
        for cand in reversed(self.cands):
            if cand.rep.js is not None and not any(r.startswith("scan_error") for r in cand.rep.reasons):
                if cand.verdict is not None:          # rendered before: "ok" cannot be here, so rejected
                    return None
                try:
                    views.check(self.renderer, cand.rep.js)
                except RenderRejected as r:
                    self._event(step="leftover", attempt=cand.attempt, outcome="render_rejected",
                                rule=views.rejection_info(r)[1])
                    return None
                self._event(step="leftover", attempt=cand.attempt, outcome="accepted")
                return cand.rep.js
        return None

    def refine(self, gen: Candidate) -> str | None:
        s = self.s
        code = model_source(gen.text)
        if not code or not code.strip():
            self._event(step="refine", outcome="skipped:no_code")
            return None
        for r_attempt in range(s.refine_max_attempts):
            params = build_refine_params(s, self.pid, self.seed, self.url, code, gen.pngs, attempt=r_attempt)
            result, err, made = self._call(params, "refine", r_attempt, cap=s.refine_credit_cap)
            if not made:
                return None
            if err is not None:
                reason = refine_retry_reason(err, s) if r_attempt + 1 < s.refine_max_attempts else None
                if reason is not None:
                    self._event(step="refine", attempt=r_attempt, outcome="retry", reason=reason)
                    continue
                return None
            reply = reply_text(result)
            rep2 = self._repair(reply)
            self.cands.append(Candidate("refine", r_attempt, rep2, reply))
            if not rep2.ok:
                self._event(step="refine", attempt=r_attempt, outcome="rejected_output", reasons=rep2.reasons[:4])
                return None
            try:
                views.check_refined(self.renderer, rep2.js or "", s.refine_views)
            except RenderRejected as r:
                stage, rule = views.rejection_info(r)
                self._event(step="refine", attempt=r_attempt, outcome="render_rejected", stage=stage, rule=rule)
                return None
            self._event(step="refine", attempt=r_attempt, outcome="accepted",
                        changed=rep2.sha256 != gen.rep.sha256)
            return rep2.js
        return None

    def fallback(self) -> str | None:
        js = fallback_module(self.pid, self.s.repair_version)
        try:
            views.check(self.renderer, js)
        except RenderRejected as r:
            self._event(step="fallback", outcome="render_rejected", rule=views.rejection_info(r)[1])
            return None
        self._event(step="fallback", outcome="accepted")
        return js

    def report(self, final: str, source: str | None) -> None:
        """One diagnostic line on stderr (never an input to anything)."""
        try:
            line = json.dumps({"sn17gen": self.s.label, "version": __version__, "pid": self.pid, "final": final,
                               "caps": {"gen": self.s.credit_cap, "refine": self.s.refine_credit_cap},
                               "refine_retry": self.s.refine_retry,
                               "service_tier": self.s.service_tier(self.pid),
                               "final_sha256_16": None if source is None else _sha16(source),
                               "calls": self.ledger.calls, "credits": credits_str(self.ledger.spent),
                               "unknown_usage_calls": self.ledger.unknown_usage_calls, "events": self.events},
                              sort_keys=True, default=str)
            sys.stderr.write(line[:20_000] + "\n")
        except Exception:  # noqa: BLE001  diagnostics must never change the outcome
            pass


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def run_detailed(image: bytes, seed: int, witness: Any, renderer: Any,
                 settings: Settings | None = None) -> tuple[str | None, _Prompt]:
    """``run`` plus the per-prompt state (ledger, events) for tests and diagnostics."""
    p = _Prompt(image, seed, witness, renderer, settings or ACTIVE)
    gen = p.gen()
    source: str | None = None
    final = "none"
    if gen is not None:
        source, final = gen.rep.js, "gen"
        if p.s.refine:
            refined = p.refine(gen)
            if refined is not None:
                source, final = refined, "refine"
    else:
        left = p.leftover()
        if left is not None:
            source, final = left, "leftover"
    if source is None:
        source = p.fallback()
        final = "fallback" if source is not None else "none"
    p.final = final
    p.report(final, source)
    return source, p


def run(image: bytes, seed: int, witness: Any, renderer: Any, settings: Settings | None = None) -> str | None:
    """Generate one Three.js module for one prompt image. Returns the JavaScript, or None if nothing valid."""
    return run_detailed(image, seed, witness, renderer, settings)[0]

"""Committed generation settings: the ONE place that selects the pipeline mode.

Change a behaviour by editing the constants below and committing; nothing reads the environment, the clock, the
host or the network (README "Reproducibility"). Generation and replay run the same committed source, so they always
run the same settings. Tests build other variants with ``make_settings(...)`` without touching these constants.

Modes (ported from sn17miner 0.4.0, measured in rounds 3-4):
  v1  gen only: gpt-6-astra, effort high, prompt p4, repair r3, output-cap retry rp1; the module must pass the
      production validator (renderer.render raises RenderRejected otherwise) or the next attempt / the fallback is
      used. sn17miner v1 = this with repair r2 (r3 only adds structural checks for JS syntax errors).
  v2  v1 + ONE render-and-refine call (refine f1; E4 EVAL-B held-out: v2 - v1 = +45.6 pts, v2 vs the threejs
      leader 112/125). ``REFINE_VIEWS`` picks the images the refine call sees:
        "front4": rv1 = the judge's four Stage-1 front views (front_left 30/0, front_right 330/0, front_below 0/15,
                  front_above 0/-30), 512 px, neutral light, white background, one render() call per view
                  (byte-identical to sn17miner's one-call rv1 PNGs, runtime_emu measurement);
        "grid":   the runtime's default preview, ONE render() call with no options (2 x 2 grid, studio light, grey).
      ``REFINE_VERSION`` "f2" = keep-pose text + one refine retry after a recorded provider failure (E4: a tie with
      f1, -0.8 pts); f2 exists for front4 only.
      Refine budget (sn17miner v2.2, R7): ``REFINE_CREDIT_CAP`` = 4,000,000 credits. Before each refine call, recorded
      spend + that call's worst case must stay within it; every gen call (and rp1's 32k-or-24k choice) keeps
      ``credit_cap`` = 3,000,000, so the gen requests, gen spend and every refine body are exactly v2's. v2.2 refines
      where v2 did, plus where gen spend + the refine worst case is in (3M, 4M] (R7: 8 of 381 prompt-draws, judged
      5 wins / 2 draws / 0 losses vs the unrefined module; ~$1.0 per rescued refine). Per-prompt worst case <= 4M
      (README nominal 5M). 3,000,000 here = sn17miner v2 / submission 0.1.0 behaviour.
  The refine text stays f1: R7's f3 (-2.7 pts pooled) and f4 (0.0) missed the pre-registered +7 bar for EVAL-B.

Service tier (0.2.1, a window-fit contingency; U1 round 8): ``SERVICE_TIER_FRACTION`` = the share of prompts whose
calls carry ``"service_tier": FAST_TIER``. A prompt is on the fast tier iff
    int(sha256(image bytes).hexdigest()[:8], 16) / 2**32 < SERVICE_TIER_FRACTION
(``image`` = the prompt bytes the runtime passes to generate(); pid = sha256(image)[:16], so this is pid[:8]); then
EVERY call of that prompt (gen attempts, the rp1 retry, the refine call and f2's retry) carries it, appended as the
last request key. Any other prompt sends no ``service_tier`` key at all, so 0.0 (committed) = 0.2.0's request bytes
exactly, and unselected prompts under 0.5 = 0.2.0's bytes too. The decision is a pure function of the image bytes:
never the seed, the ticket, the clock, the queue or the remaining window (replay compares the body). FAST_TIER "fast"
(U1: ~1.7-1.9x tokens/s, 2x real cash; competition credits unchanged, so the budget rules and the ledger are the same);
"ultrafast" (~4-5x, 6x cash) is accepted but not used by any variant. The response's echoed tier is never read.

Refine retry (0.2.2, variant v2r; VPS_HOST.md F3): ``REFINE_RETRY`` True = after a RECORDED retryable failure of
the refine call, make exactly ONE more refine call (attempt 1) whose body differs from attempt 0's only in the
``attempt=`` digit of the tag; f1's text, images and code are unchanged. Retryable = refine f2's rule
(pipeline.refine_retryable: HTTP 5xx, an HTTP-200 stream ``error`` event that is not a credit/quota code,
response.failed, response.incomplete) PLUS an HTTP-200 stream that ended without a terminal event (F3: the pinned
decoder's "Missing completed OpenAI response", wrapped as invalid_provider_response; counted as provider_incomplete,
pipeline.stream_cut). Never after a provider rejection (HTTP 400/401/403/404/429), any other undecodable record, a
credit/quota code, a non-provider (transport/gateway/client) error, a structural rejection of the reply or a
RenderRejected of the refined module. The retry runs only if recorded spend + its worst case <= REFINE_CREDIT_CAP;
with REFINE_RETRY a recorded failure whose usage the decoder cannot read (invalid_provider_response, gen or refine)
is charged its worst case, so the cut call's unknown billing counts against the cap. False = 0.2.1 behaviour (f1:
one refine call; the same request bytes and branches as 0.2.1/0.2.0 on every path). Every branch is a pure function
of the recorded bytes, so replay reproduces it. The normal path (no refine failure) sends exactly 0.2.0's bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .contract import INSTRUCTIONS, REFINE_MAX_ATTEMPTS, REFINE_TEXT_BY_VIEWS, REFINE_VIEW_MODES, RETRY_COMPACT_TEXT

# ------------------------------------------------------------------------------------------------------------------
# THE COMMITTED MODE. Edit, commit, then publish that commit.
MODE = "v2"              # "v1" (no refine) | "v2" (gen + one render-and-refine call)
REFINE_VERSION = "f1"    # v2 only: "f1" | "f2"
REFINE_VIEWS = "front4"  # v2 only: "front4" | "grid"
REFINE_CREDIT_CAP = 4_000_000  # v2 only: refine call(s) run while recorded spend + worst case <= this (v2.2); gen keeps 3M
TICKET = "sven_000"            # label carried in every request tag; give each own hotkey's commit its own ticket
SERVICE_TIER_FRACTION = 0.0  # share of prompts on FAST_TIER, picked by sha256(image) (0.0 = no service_tier key = 0.2.0 bytes)
FAST_TIER = "fast"       # service_tier of the selected prompts' gen + refine calls ("fast" = 2x cash, ~1.8x speed; U1)
REFINE_RETRY = True      # v2 only: one refine retry (attempt 1) after a recorded retryable failure incl. a stream cut (0.2.2 v2r; False = 0.2.1)
# ------------------------------------------------------------------------------------------------------------------

MODES = ("v1", "v2")
REFINE_VERSIONS = ("f1", "f2")
FAST_TIERS = ("fast", "ultrafast")   # never "flex" (forbidden by 404) and never "default"/"auto" (= no key)
GEN_CREDIT_CAP = 3_000_000      # per-prompt worst-case cap of every gen call (sn17miner's $3 per model)
README_CREDITS_PER_PROMPT = 5_000_000
_LABEL = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_PID = re.compile(r"^[0-9a-f]{16}$")


@dataclass(frozen=True)
class Settings:
    """Everything that shapes a request, a retry decision or the returned JavaScript."""

    mode: str
    model: str
    effort: str
    detail: str                         # prompt image detail (and refine image detail)
    max_output_tokens: int
    max_attempts: int                   # gen attempts: attempt 0 + one retry after a recorded/rejected outcome
    credit_cap: int                     # per-prompt worst-case cap in credits of every gen call (README nominal 5,000,000)
    refine_credit_cap: int | None       # v2: per-prompt worst-case cap of the refine call(s) (v2.2: 4,000,000); v1: None
    call_cap: int                       # per-prompt call cap (README nominal 60)
    prompt_version: str
    repair_version: str
    fit_target_milli: int
    retry_policy: str                   # rp1: output-cap retry with the compact line
    retry_max_output_tokens: int
    refine_version: str | None
    refine_views: str | None
    refine_effort: str | None
    refine_max_output_tokens: int | None
    ticket: str
    service_tier_fraction: float = 0.0  # share of prompts (by sha256(image)) whose calls carry service_tier=fast_tier
    fast_tier: str = "fast"
    refine_retry: bool = False          # v2: one refine retry after a recorded retryable failure / stream cut (0.2.2)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.prompt_version not in INSTRUCTIONS:
            raise ValueError(f"unknown prompt_version {self.prompt_version!r}")
        if self.retry_policy not in RETRY_COMPACT_TEXT:
            raise ValueError(f"unknown retry_policy {self.retry_policy!r}")
        if not _LABEL.match(self.ticket):
            raise ValueError(f"ticket must match {_LABEL.pattern}, got {self.ticket!r}")
        f = self.service_tier_fraction
        if type(f) not in (int, float) or not (0 <= f <= 1):           # also refuses NaN and +-inf
            raise ValueError(f"service_tier_fraction must be a number in [0, 1], got {f!r}")
        if f != 0:
            # 0.3.0 (C4): fast tiers dormant BY CHOICE. 404's Pod admits fast / ultrafast at Standard rates (only flex is
            # refused, budget.py at 44d2a64), but with the Pod's concurrency the window no longer needs them, and a
            # non-zero fraction would need a re-gate of its bodies through the real GenerationWitness / ReplayWitness
            raise ValueError(f"service_tier_fraction must be 0.0 (fast tiers dormant since 0.3.0), got {f!r}")
        if self.fast_tier not in FAST_TIERS:
            raise ValueError(f"fast_tier must be one of {FAST_TIERS}, got {self.fast_tier!r}")
        if not (self.max_output_tokens < self.retry_max_output_tokens <= 128_000):
            raise ValueError("retry_max_output_tokens must exceed max_output_tokens")
        if not (1 <= self.max_attempts <= 3):
            raise ValueError("max_attempts out of range")
        if not (0 < self.credit_cap <= README_CREDITS_PER_PROMPT) or not (0 < self.call_cap <= 60):
            raise ValueError("caps must stay within the README's nominal per-prompt limits")
        if type(self.refine_retry) is not bool:
            raise ValueError(f"refine_retry must be a bool, got {self.refine_retry!r}")
        refine = (self.refine_version, self.refine_views, self.refine_effort, self.refine_max_output_tokens,
                  self.refine_credit_cap)
        if self.mode == "v1":
            if any(v is not None for v in refine) or self.refine_retry:
                raise ValueError("mode v1 has no refine settings")
        else:
            if self.refine_version not in REFINE_VERSIONS:
                raise ValueError(f"refine_version must be one of {REFINE_VERSIONS}, got {self.refine_version!r}")
            if self.refine_views not in REFINE_VIEW_MODES:
                raise ValueError(f"refine_views must be one of {REFINE_VIEW_MODES}, got {self.refine_views!r}")
            if (self.refine_version, self.refine_views) not in REFINE_TEXT_BY_VIEWS:
                raise ValueError(f"refine {self.refine_version} has no {self.refine_views!r} variant")
            if self.refine_effort not in ("low", "medium", "high", "xhigh"):
                raise ValueError("refine_effort out of range")
            if not isinstance(self.refine_max_output_tokens, int) or not (1_000 <= self.refine_max_output_tokens <= 128_000):
                raise ValueError("refine_max_output_tokens out of range")
            c = self.refine_credit_cap
            if type(c) is not int or not (self.credit_cap <= c <= README_CREDITS_PER_PROMPT):
                raise ValueError(f"refine_credit_cap must be an int in [credit_cap {self.credit_cap:,}, "
                                 f"{README_CREDITS_PER_PROMPT:,}], got {c!r}")

    @property
    def refine(self) -> bool:
        return self.mode == "v2"

    @property
    def refine_max_attempts(self) -> int:
        """f1: 1 call; f2 or refine_retry: attempt 0 + at most one retry (attempt 1)."""
        if not self.refine:
            return 0
        return max(REFINE_MAX_ATTEMPTS[self.refine_version], 2 if self.refine_retry else 1)

    @property
    def label(self) -> str:
        base = f"{self.mode}-{self.refine_version}-{self.refine_views}" if self.refine else self.mode
        if self.refine_retry:
            base += "-rr"
        return base if self.service_tier_fraction == 0 else f"{base}-{self.fast_tier}{self.service_tier_fraction:g}"

    def service_tier(self, pid: str) -> str | None:
        """The prompt's tier: ``fast_tier`` iff int(pid[:8], 16) / 2**32 < service_tier_fraction, else None (the request
        carries no service_tier key). ``pid`` = sha256(image bytes).hexdigest()[:16], so pid[:8] is the image hash's
        first 8 hex digits. A pure function of the image: the same for every call of the prompt."""
        if not isinstance(pid, str) or not _PID.match(pid):
            raise ValueError(f"pid must be 16 lowercase hex digits, got {pid!r}")
        return self.fast_tier if int(pid[:8], 16) / 2**32 < self.service_tier_fraction else None


def make_settings(mode: str = MODE, refine_version: str = REFINE_VERSION, refine_views: str = REFINE_VIEWS,
                  ticket: str = TICKET, refine_credit_cap: int = REFINE_CREDIT_CAP,
                  service_tier_fraction: float = SERVICE_TIER_FRACTION, fast_tier: str = FAST_TIER,
                  refine_retry: bool = REFINE_RETRY) -> Settings:
    """``refine_credit_cap`` 3,000,000 = the v2 / 0.1.0 refine budget (for comparisons); ignored in mode v1.
    ``service_tier_fraction`` 0.0 = no service_tier key in any request (0.2.0 bytes).
    ``refine_retry`` False = 0.2.1 behaviour; ignored in mode v1 (no refine step)."""
    refine = mode == "v2"
    return Settings(
        mode=mode, model="gpt-6-astra", effort="high", detail="high", max_output_tokens=24_000, max_attempts=2,
        credit_cap=GEN_CREDIT_CAP, refine_credit_cap=refine_credit_cap if refine else None, call_cap=60,
        prompt_version="p4", repair_version="r3", fit_target_milli=970,
        retry_policy="rp1", retry_max_output_tokens=32_000,
        refine_version=refine_version if refine else None, refine_views=refine_views if refine else None,
        refine_effort="high" if refine else None, refine_max_output_tokens=24_000 if refine else None,
        ticket=ticket, service_tier_fraction=service_tier_fraction, fast_tier=fast_tier,
        refine_retry=refine_retry if refine else False,
    )


ACTIVE = make_settings()

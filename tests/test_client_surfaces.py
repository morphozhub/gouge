"""0.3.1: one witness call for both published witness-client generations (pipeline.witness_call).

rc6 (44d2a64 / 92bedb6) and 29a0d82 hand the same recorded provider exchange to generate() in different shapes
(tests/fakes.py). On whichever witness_client is on sys.path (fakes decodes with that generation's real decoder and a
port of the other one), these tests check that:
  * create() never receives ``stream`` (nor request_id / session_id), on either surface, on every path;
  * every recorded outcome type takes the same branch on both surfaces (the same requests, renders, events, ledger and
    output) at every call site (gen attempt 0 / 1, refine, refine retry) under v2r, v2, f2 and v1, and never escapes
    generate();
  * witness_call turns each 29a0d82 object into rc6's exact provider WitnessError (fields, cause, classifiers, usage);
  * client / gateway failures, ValueErrors raised outside the decoder and APIErrors without a provider cause propagate
    with no further call;
  * the 0.3.0 call shape (``stream`` passed through) is visible: on 29a0d82 every prompt falls back;
  * the two known, documented differences between the generations (pipeline docstring "Where the two generations
    still differ") stay exactly as documented (TestKnownDivergences);
  * the flow tests of test_pipeline / test_refine_retry also pass on rc6 (by default they run on 29a0d82).
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

import openai
import fakes
import test_pipeline as tp
import test_refine_retry as trr
from fakes import (EXCHANGE_ID, FIXTURE_TICKET, FIXTURE_TIER_FRACTION, IMAGE, REAL_29_DECODER, SEED, SURFACE_29,
                   SURFACE_RC6, SURFACES, FakeRenderer, FakeWitness, _invalid, _response, _sse,
                   completed_without_output, err_failed, err_gateway, err_http, err_http_body, err_incomplete,
                   err_invalid, err_stream, err_stream_cut, err_stream_cut_mid_text, err_transport, exchange_of, fenced,
                   model_result_29a0d82, module, ok, rc6_outcome, script_from)
from openai.types.responses import Response
from witness_client import ExchangePending, WitnessError
try:
    from witness_client import ReplayMismatch           # <= 4c247b8
except ImportError:                                     # 404 e0e156c moved it to the miner runtime
    from miner_runtime.observations import ReplayMismatch
from witness_client import response as wc_response

from sn17gen import pipeline
from sn17gen.credits import CallUsage, usage_from_error
from sn17gen.pipeline import (STREAM_CUT_MESSAGE, hit_output_cap, refine_retry_reason, refine_retryable, reply_text,
                              run_detailed, stream_cut, witness_call)
from sn17gen.settings import make_settings


def fx(*args, retry=False, **kw):
    return make_settings(*args, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=retry, **kw)


V2R = fx("v2", "f1", "front4", retry=True)      # the committed launch configuration (v2r) with the fixture ticket
V2 = fx("v2", "f1", "front4")
V21 = fx("v2", "f2", "front4")
V1 = fx("v1")
SETTINGS = (V2R, V2, V21, V1)
GEN = fenced(module("336699"))
REF = fenced(module("884422"), prefix="- colour was off\n")
REF2 = fenced(module("117733"))
# the keyword arguments our requests hand to create(): the request dict minus "stream" (service_tier is dormant)
CREATE_KEYS = {"model", "instructions", "input", "reasoning", "max_output_tokens", "store"}
RC6_DELIVERED = {"WitnessError", "WitnessResult", "Result"}

# name -> (scripted outcome, what 29a0d82's create() delivers for its recorded bytes)
OUTCOMES = {
    "completed": (lambda: ok(GEN), "Response"),
    "completed_no_code": (lambda: ok("I cannot do that."), "Response"),
    # review F2 (10-26): no "output" key; 29a0d82's Response.output_text raises TypeError, reply_text reads "" = rc6
    "completed_without_output": (completed_without_output, "Response"),
    "incomplete_max_output_tokens": (lambda: err_incomplete(), "Response"),
    "incomplete_partial_text": (lambda: err_incomplete(partial=GEN[: len(GEN) // 2]), "Response"),
    "incomplete_content_filter": (lambda: err_incomplete(3_800, 100, reason="content_filter"), "Response"),
    "failed": (err_failed, "Response"),
    "stream_cut": (err_stream_cut, "ValueError"),
    "stream_cut_mid_text": (err_stream_cut_mid_text, "ValueError"),
    "empty_200": (lambda: _invalid(b""), "ValueError"),
    "done_only_200": (lambda: _invalid(b"data: [DONE]\n\n"), "ValueError"),
    "stream_error_overloaded": (lambda: err_stream("overloaded"), "APIError"),
    "stream_error_quota": (lambda: err_stream("quota"), "APIError"),
    "http_400": (lambda: err_http(400), "BadRequestError"),
    "http_401": (lambda: err_http(401), "AuthenticationError"),
    "http_403": (lambda: err_http(403), "PermissionDeniedError"),
    "http_404": (lambda: err_http(404), "NotFoundError"),
    "http_409": (lambda: err_http(409, "conflict"), "ConflictError"),
    "http_418": (lambda: err_http(418, "teapot"), "APIStatusError"),
    "http_422": (lambda: err_http(422, "unprocessable"), "UnprocessableEntityError"),
    "http_429": (lambda: err_http(429, "rate_limit_exceeded"), "RateLimitError"),
    "http_429_quota": (lambda: err_http(429, "insufficient_quota"), "RateLimitError"),
    "http_500": (lambda: err_http(500, "server_error"), "InternalServerError"),
    "http_503": (lambda: err_http(503, "server_error"), "InternalServerError"),
    "http_503_quota": (lambda: err_http(503, "insufficient_quota"), "InternalServerError"),
    # bodies without the {"error": {...}} wrapper: rc6 reads no provider code from them (so this 5xx stays retryable)
    "http_503_unwrapped_code": (lambda: err_http_body(503, b'{"code":"insufficient_quota","message":"x"}'),
                                "InternalServerError"),
    "http_502_not_json": (lambda: err_http_body(502, b"bad gateway"), "InternalServerError"),
    # review F4 (10-26): invalid UTF-8 that parses after replacement decoding; rc6 decodes the raw bytes (no provider
    # code: retryable), so witness_call must hand provider_error the raw bytes (response.content), never .text
    "http_503_quota_non_utf8": (lambda: err_http_body(503, b'{"error": {"code": "insufficient_quota", '
                                                           b'"message": "caf\xe9"}}'), "InternalServerError"),
    "invalid_partial_frame": (lambda: err_invalid("partial_frame"), "JSONDecodeError"),
    "invalid_inconsistent": (lambda: err_invalid("inconsistent"), "ValueError"),
    "invalid_two_terminals": (lambda: err_invalid("two_terminals"), "ValueError"),
    "invalid_bad_utf8": (lambda: err_invalid("bad_utf8"), "UnicodeDecodeError"),
}


def positions(mk):
    """Call sites for one outcome (``mk`` builds it): (name, script table, the call that gets it first)."""
    return [
        ("gen0", {("gen", 0): mk(), ("gen", 1): ok(GEN), ("refine", 0): ok(REF), ("refine", 1): ok(REF2)}, ("gen", 0)),
        ("gen1", {("gen", 0): ok("no code"), ("gen", 1): mk(), ("refine", 0): ok(REF), ("refine", 1): ok(REF2)},
         ("gen", 1)),
        ("gen0_gen1", {("gen", 0): mk(), ("gen", 1): mk(), ("refine", 0): ok(REF), ("refine", 1): ok(REF2)}, ("gen", 0)),
        ("refine0", {("gen", 0): ok(GEN), ("refine", 0): mk(), ("refine", 1): ok(REF2)}, ("refine", 0)),
        ("refine1", {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): mk()}, ("refine", 1)),
        ("refine0_refine1", {("gen", 0): ok(GEN), ("refine", 0): mk(), ("refine", 1): mk()}, ("refine", 0)),
    ]


def run_on(surface, s, table, witness_cls=FakeWitness):
    w, r = witness_cls(script_from(table), surface), FakeRenderer()
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            out, p = run_detailed(IMAGE, SEED, w, r, s)
        except Exception as e:  # noqa: BLE001
            return ("raised", type(e).__name__, str(e)), w, r
    return ("returned", out, p.final, p.events, p.ledger.spent, p.ledger.calls, p.ledger.unknown_usage_calls), w, r


def delivered_29(status, raw):
    """Type name of what 29a0d82's create(stream=False) returns or raises for one recorded exchange."""
    try:
        return type(model_result_29a0d82(status, raw)).__name__
    except BaseException as e:  # noqa: BLE001
        return type(e).__name__


def mapped(status, raw):
    """witness_call over 29a0d82's delivery of one exchange: (result, None) or (None, the raised error)."""
    def create(**kw):
        assert "stream" not in kw
        return model_result_29a0d82(status, raw)
    try:
        return witness_call(create, {"stream": True, "model": "gpt-6-astra"}), None
    except BaseException as e:  # noqa: BLE001
        return None, e


def error_view(e):
    """Everything about a provider WitnessError that the pipeline, the ledger or the diagnostics read."""
    resp, cause = e.response, e.__cause__
    details = getattr(resp, "incomplete_details", None)
    return {"type": type(e).__name__, "args": e.args, "source": e.source, "code": e.code, "status_code": e.status_code,
            "provider_code": e.provider_code, "param": e.param, "provider_message": e.provider_message,
            "response_status": getattr(resp, "status", None), "incomplete_reason": getattr(details, "reason", None),
            "response_text": None if resp is None else resp.output_text,
            "cause": None if cause is None else (type(cause).__name__, str(cause)),
            "hit_output_cap": hit_output_cap(e), "refine_retryable": refine_retryable(e), "stream_cut": stream_cut(e),
            "retry_v2r": refine_retry_reason(e, V2R), "retry_f2": refine_retry_reason(e, V21),
            "usage": usage_from_error(e), "usage_unknown": pipeline._usage_unknown(e)}


class TestFakes(unittest.TestCase):
    def test_helpers_are_the_rc6_decode_of_their_recorded_bytes(self):
        """ok() / err_*() objects (used directly by the classifier tests) = what rc6's recorded_result makes of the
        exchange they carry, which is what both FakeWitness surfaces decode."""
        for name, (mk, _) in OUTCOMES.items():
            x = mk()
            got = rc6_outcome(*exchange_of(x))
            if isinstance(x, BaseException):
                self.assertIsInstance(got, WitnessError, name)
                self.assertEqual(error_view(got), error_view(x), name)
            else:
                self.assertEqual((got.output_text, CallUsage.from_usage(got.usage)),
                                 (x.output_text, CallUsage.from_usage(x.usage)), name)

    def test_29a0d82_delivery_table(self):
        """The 29a0d82 surface really delivers 29a0d82 objects: Responses with a status, SDK status errors, APIError
        for a stream error event, bare decoder ValueErrors (the real model_result on the 29a0d82 site, its port on rc6
        sites)."""
        for name, (mk, want) in OUTCOMES.items():
            status, raw = exchange_of(mk())
            self.assertEqual(delivered_29(status, raw), want, name)
        r = model_result_29a0d82(*exchange_of(err_incomplete()))
        self.assertEqual((r.status, r.incomplete_details.reason), ("incomplete", "max_output_tokens"))
        r = model_result_29a0d82(*exchange_of(ok(GEN, 3_800, 5_000)))
        self.assertEqual((r.status, r.output_text, r.usage.output_tokens), ("completed", GEN, 5_000))
        with self.assertRaises(openai.APIError) as cm:
            model_result_29a0d82(*exchange_of(err_stream("overloaded")))
        self.assertEqual((type(cm.exception.__cause__), cm.exception.code), (WitnessError, "server_is_overloaded"))
        stream = model_result_29a0d82(*exchange_of(ok(GEN)), stream=True)
        self.assertEqual(type(stream).__name__, "ModelStream")
        self.assertFalse(hasattr(stream, "output_text"))

    def test_fake_witness_surfaces(self):
        with self.assertRaises(ValueError):
            FakeWitness(script_from({}), "rc5")
        self.assertEqual(FakeWitness(script_from({})).surface, fakes.DEFAULT_SURFACE)
        self.assertEqual(fakes.DEFAULT_SURFACE, SURFACE_29)
        self.assertEqual(SURFACES, (SURFACE_RC6, SURFACE_29))


class TestNeverStream(unittest.TestCase):
    def test_create_never_receives_stream(self):
        """On both surfaces and every path, create() gets exactly the request dict minus "stream": no stream (29a0d82:
        stream=True returns a ModelStream), no request_id / session_id (they would change the call key). (The branch
        tests below check the same on every run.)"""
        n = 0
        for name, (mk, _) in OUTCOMES.items():
            for pos, table, _ in positions(mk):
                for s in (V2R, V1):
                    for surface in SURFACES:
                        _, w, _ = run_on(surface, s, table)
                        self.assertTrue(w.received, (name, pos))
                        for kw, params in zip(w.received, w.params):
                            self.assertEqual(set(kw), CREATE_KEYS, (name, pos, surface))
                            self.assertEqual({**kw, "stream": True}, params)
                            n += 1
        self.assertGreater(n, 1000)

    def test_the_0_3_0_call_shape_falls_back_on_29a0d82(self):
        """The fakes can see blocker 1: with "stream" passed through (0.3.0), 29a0d82's create() returns a ModelStream,
        so every gen reply reads as empty text and the prompt ends as the fallback after two paid calls; rc6 refines."""
        class StreamThrough(FakeWitness):
            def create(self, **kw):
                return super().create(stream=True, **kw)
        table = {("gen", 0): ok(GEN), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)}
        r6, w6, _ = run_on(SURFACE_RC6, V2R, table, StreamThrough)
        r29, w29, _ = run_on(SURFACE_29, V2R, table, StreamThrough)
        self.assertEqual((r6[2], w6.steps), ("refine", [("gen", 0), ("refine", 0)]))
        self.assertEqual((r29[2], w29.steps, w29.delivered), ("fallback", [("gen", 0), ("gen", 1)], ["ModelStream"] * 2))
        self.assertEqual(w29.bodies, [w6.bodies[0], w6.bodies[0].replace(b"step=gen attempt=0]", b"step=gen attempt=1]")])
        r, w, _ = run_on(SURFACE_29, V2R, table)                       # 0.3.1: stream dropped
        self.assertEqual((r[1], r[2], w.delivered), (r6[1], "refine", ["Response", "Response"]))


class TestSameBranches(unittest.TestCase):
    def test_every_outcome_takes_the_same_branch_on_both_surfaces(self):
        """Each recorded outcome at each call site, under v2r / v2 / f2 / v1: identical requests (bodies), renders,
        events, ledger (spent, calls, unknown usage), final and output on rc6 and 29a0d82; no provider outcome escapes
        generate(); and the 29a0d82 run really received the 29a0d82 object for it."""
        for name, (mk, want29) in OUTCOMES.items():
            for pos, table, first in positions(mk):
                for s in SETTINGS:
                    key = (name, pos, s.label)
                    r6, w6, rr6 = run_on(SURFACE_RC6, s, table)
                    r29, w29, rr29 = run_on(SURFACE_29, s, table)
                    self.assertEqual(r6[0], "returned", key)
                    self.assertEqual(r29, r6, key)
                    self.assertEqual((w29.steps, w29.bodies, rr29.calls), (w6.steps, w6.bodies, rr6.calls), key)
                    self.assertTrue(all(set(kw) == CREATE_KEYS for kw in w6.received + w29.received), key)
                    if first in w29.steps:
                        k = w29.steps.index(first)
                        self.assertEqual(w29.delivered[k], want29, key)
                        self.assertIn(w6.delivered[k], RC6_DELIVERED, key)
                    self.assertTrue(set(w6.delivered) <= RC6_DELIVERED, key)

    def test_multi_failure_paths(self):
        """Every recorded gen failure followed by every recorded refine failure (then the retry), under v2r."""
        failures = [k for k in OUTCOMES if not k.startswith("completed")]
        for g in failures:
            for f in failures:
                for s in (V2R,):
                    table = {("gen", 0): OUTCOMES[g][0](), ("gen", 1): ok(GEN), ("refine", 0): OUTCOMES[f][0](),
                             ("refine", 1): ok(REF)}
                    r6, w6, rr6 = run_on(SURFACE_RC6, s, table)
                    r29, w29, rr29 = run_on(SURFACE_29, s, table)
                    self.assertEqual(r6[0], "returned", (g, f, s.label))
                    self.assertEqual((r29, w29.bodies, rr29.calls), (r6, w6.bodies, rr6.calls), (g, f, s.label))


class TestMapping(unittest.TestCase):
    def test_29a0d82_objects_become_rc6s_provider_errors(self):
        """witness_call on 29a0d82's delivery = rc6's object for the same bytes: the same WitnessError fields (message,
        source, code, status, provider code, response, cause) and the same classifier and usage results."""
        for name, (mk, _) in OUTCOMES.items():
            status, raw = exchange_of(mk())
            rc6 = rc6_outcome(status, raw)
            result, err = mapped(status, raw)
            if isinstance(rc6, BaseException):
                self.assertIsNone(result, name)
                self.assertIs(type(err), WitnessError, name)
                self.assertEqual(error_view(err), error_view(rc6), name)
            else:
                self.assertIsNone(err, name)
                self.assertEqual((reply_text(result), CallUsage.from_usage(result.usage)),
                                 (reply_text(rc6), CallUsage.from_usage(rc6.usage)), name)
        # the four classifiers see the cases they exist for
        cut = mapped(*exchange_of(err_stream_cut()))[1]
        self.assertEqual((cut.code, type(cut.__cause__), cut.__cause__.args), ("invalid_provider_response", ValueError,
                                                                              (STREAM_CUT_MESSAGE,)))
        self.assertTrue(stream_cut(cut))
        self.assertTrue(hit_output_cap(mapped(*exchange_of(err_incomplete()))[1]))
        self.assertTrue(refine_retryable(mapped(*exchange_of(err_http(503, "server_error")))[1]))
        self.assertFalse(refine_retryable(mapped(*exchange_of(err_stream("quota")))[1]))
        self.assertEqual(usage_from_error(mapped(*exchange_of(err_failed(9_000, 3_000)))[1]), CallUsage(9_000, 3_000))

    def test_rc6_objects_pass_through(self):
        """On rc6, witness_call returns the reply and re-raises the client's own WitnessError unchanged."""
        for name, (mk, _) in OUTCOMES.items():
            x = rc6_outcome(*exchange_of(mk()))
            def create(**kw):
                if isinstance(x, BaseException):
                    raise x
                return x
            try:
                got = witness_call(create, {"stream": True, "model": "gpt-6-astra"})
            except WitnessError as e:
                got = e
            self.assertIs(got, x, name)

    def test_decoder_reference(self):
        """The ValueError rule keys on the client's own decoder function (the one create() runs on recorded bytes)."""
        self.assertIs(pipeline.completed_response, wc_response.completed_response)
        self.assertIs(pipeline.provider_error, wc_response.provider_error)
        try:
            wc_response.completed_response(b"")
        except ValueError as e:
            self.assertTrue(pipeline._raised_by_decoder(e))
        self.assertFalse(pipeline._raised_by_decoder(ValueError(STREAM_CUT_MESSAGE)))


class TestReplyText(unittest.TestCase):
    def test_reply_text(self):
        """reply_text = the reply's output_text on every readable reply (normal path unchanged), "" when the SDK
        property cannot read the terminal output (review F2: a completed response without ``output``)."""
        for name, (mk, _) in OUTCOMES.items():
            status, raw = exchange_of(mk())
            rc6 = rc6_outcome(status, raw)
            if isinstance(rc6, BaseException):
                continue
            self.assertEqual(reply_text(rc6), rc6.output_text, name)
            r29 = mapped(status, raw)[0]
            if name != "completed_without_output":
                self.assertEqual(reply_text(r29), r29.output_text, name)
        r29 = mapped(*exchange_of(completed_without_output()))[0]
        with self.assertRaises(TypeError):
            r29.output_text
        self.assertEqual(reply_text(r29), "")
        self.assertEqual(reply_text(model_result_29a0d82(*exchange_of(ok(GEN)))), GEN)

    def test_completed_without_output_no_longer_escapes(self):
        """Review F2: at every call site the record is a rejected output on both generations (rc6's branch), never
        an exception out of generate() (which left the prompt pending / not_submitted on 29a0d82)."""
        for pos, table, _ in positions(completed_without_output):
            r6, w6, _ = run_on(SURFACE_RC6, V2R, table)
            r29, w29, _ = run_on(SURFACE_29, V2R, table)
            self.assertEqual(r29[0], "returned", pos)
            self.assertEqual((r29, w29.steps), (r6, w6.steps), pos)
            self.assertIn("rejected_output", [e.get("outcome") for e in r29[3]], pos)


class _Delivered:
    """A witness whose create() hands back one fixed delivery per call (what one client generation's create() returns
    or raises for that call's recorded bytes)."""

    def __init__(self, table):
        self.responses, self.table, self.steps = self, table, []

    def create(self, **kw):
        assert "stream" not in kw
        text0 = kw["input"][0]["content"][0]["text"]
        step = text0.split(" step=", 1)[1].split(" ", 1)[0]
        attempt = int(text0.split(" attempt=", 1)[1].split("]", 1)[0])
        self.steps.append((step, attempt))
        x = self.table[(step, attempt)]()
        if isinstance(x, BaseException):
            raise x
        return x


REASONING_ITEM = {"id": "rs_x", "type": "reasoning", "summary": [], "encrypted_content": "gAAAA"}


def text_done_reasoning_only(text):
    """Review F1: the text arrives in a response.output_text.done event, the terminal output holds only a reasoning
    item (non-empty, so 29a0d82 does not rebuild the message from the streamed text)."""
    resp = _response("completed", 3_800, 5_000)
    resp["output"] = [REASONING_ITEM]
    return _sse([{"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": text},
                 {"type": "response.completed", "response": resp}])


def failed_error_string():
    """Review F3: a response.failed whose ``error`` is a string, not an object."""
    resp = _response("failed", 9_000, 3_000)
    resp["error"] = "server_error"
    return _sse([{"type": "response.failed", "response": resp}]), resp


class TestKnownDivergences(unittest.TestCase):
    """The two documented differences between the generations (pipeline docstring). Pinned, so that a later edit (or
    a re-pinned client) cannot change either one silently; both are pure functions of the recorded bytes."""

    def test_streamed_text_missing_from_terminal_output(self):
        """F1: rc6 reads the streamed text, 29a0d82 reads "" (no output_text item in ``output``). At gen attempt 0:
        rc6 accepts and refines; 29a0d82 rejects the output, pays gen attempt 1, then refines. At refine: rc6 ends on
        the refined module, 29a0d82 rejects the refine output and keeps the gen module."""
        raw_gen, raw_ref = text_done_reasoning_only(GEN), text_done_reasoning_only(REF)
        self.assertEqual(reply_text(rc6_outcome(200, raw_gen)), GEN)
        self.assertEqual(reply_text(model_result_29a0d82(200, raw_gen)), "")
        if REAL_29_DECODER is not None:
            r = REAL_29_DECODER(200, raw_gen, "api")
            self.assertEqual([getattr(i, "type", None) for i in r.output], ["reasoning"])
        rec = lambda raw: fakes.ok_raw(raw)  # noqa: E731
        gen0 = {("gen", 0): rec(raw_gen), ("gen", 1): ok(GEN), ("refine", 0): ok(REF), ("refine", 1): ok(REF2)}
        r6, w6, _ = run_on(SURFACE_RC6, V2R, gen0)
        r29, w29, _ = run_on(SURFACE_29, V2R, gen0)
        self.assertEqual((r6[2], w6.steps), ("refine", [("gen", 0), ("refine", 0)]))
        self.assertEqual((r29[2], w29.steps), ("refine", [("gen", 0), ("gen", 1), ("refine", 0)]))
        self.assertIn(("gen", 0, "rejected_output"),
                      [(e.get("step"), e.get("attempt"), e.get("outcome")) for e in r29[3]])
        ref0 = {("gen", 0): ok(GEN), ("refine", 0): rec(raw_ref), ("refine", 1): ok(REF2)}
        r6, w6, _ = run_on(SURFACE_RC6, V2R, ref0)
        r29, w29, _ = run_on(SURFACE_29, V2R, ref0)
        self.assertEqual((r6[2], w6.steps), ("refine", [("gen", 0), ("refine", 0)]))
        self.assertEqual((r29[2], w29.steps), ("gen", [("gen", 0), ("refine", 0)]))
        self.assertIsInstance(r29[1], str)                          # a module (the gen one), not None
        self.assertNotEqual(r29[1], r6[1])

    def test_failed_with_string_error(self):
        """F3 (accepted as an intended improvement): rc6's decoder raises AttributeError for these bytes (the rc6
        runtime reports recording_unresolved, the prompt stays pending); on 29a0d82 witness_call makes it a recorded
        provider_failed without a provider code: retryable, charged its recorded usage. Pipeline on 29a0d82: gen
        attempt 0 -> gen attempt 1 -> refine; refine attempt 0 -> the retry -> refined."""
        raw, terminal = failed_error_string()
        with self.assertRaises(AttributeError):
            rc6_outcome(200, raw)
        # 29a0d82's delivery: completed_response(raise_failed=False) = Response.model_construct(**terminal)
        made = lambda: Response.model_construct(**json.loads(json.dumps(terminal)))  # noqa: E731
        if REAL_29_DECODER is not None:
            real = REAL_29_DECODER(200, raw, "api")
            self.assertEqual((real.status, real.error), (made().status, made().error))
            deliver = lambda: REAL_29_DECODER(200, raw, "api")  # noqa: E731
        else:
            deliver = made
        result, err = None, None
        try:
            result = witness_call(lambda **kw: deliver(), {"stream": True, "model": "gpt-6-astra"})
        except WitnessError as e:
            err = e
        self.assertIsNone(result)
        self.assertEqual((err.source, err.code, err.provider_code, err.provider_message, err.status_code),
                         ("provider", "provider_failed", None, None, None))
        self.assertTrue(refine_retryable(err))
        self.assertEqual((refine_retry_reason(err, V2R), refine_retry_reason(err, V21)),
                         ("provider_failed", "provider_failed"))
        self.assertEqual(usage_from_error(err), CallUsage(9_000, 3_000))
        ok29 = lambda text: (lambda: model_result_29a0d82(*exchange_of(ok(text))))  # noqa: E731
        w = _Delivered({("gen", 0): deliver, ("gen", 1): ok29(GEN), ("refine", 0): ok29(REF)})
        with contextlib.redirect_stderr(io.StringIO()):
            out, p = run_detailed(IMAGE, SEED, w, FakeRenderer(), V2R)
        self.assertEqual((p.final, w.steps), ("refine", [("gen", 0), ("gen", 1), ("refine", 0)]))
        w = _Delivered({("gen", 0): ok29(GEN), ("refine", 0): deliver, ("refine", 1): ok29(REF2)})
        with contextlib.redirect_stderr(io.StringIO()):
            out, p = run_detailed(IMAGE, SEED, w, FakeRenderer(), V2R)
        self.assertEqual((p.final, w.steps), ("refine", [("gen", 0), ("refine", 0), ("refine", 1)]))
        refine = [(e.get("attempt"), e.get("outcome")) for e in p.events if e.get("step") == "refine"]
        self.assertEqual(refine[:2], [(0, "recorded_failure:provider_failed"), (0, "retry")])


class ClientValueError(ValueError):
    """Stands for a ValueError a client raises outside its decoder (e.g. a pydantic ValidationError while parsing a
    gateway reply)."""


def _api_error_from(cause):
    try:
        try:
            raise cause
        except WitnessError as c:
            raise openai.APIError("wrapped", fakes._PROVIDER_REQUEST, body=None) from c
    except openai.APIError as e:
        return e


INFRA = {
    "transport": err_transport,
    "gateway": err_gateway,
    "exchange_pending": lambda: ExchangePending("Exchange is unresolved", EXCHANGE_ID),
    "replay_mismatch": lambda: ReplayMismatch("Retained observation produced a different outcome"),
    "request_validation": lambda: ValueError("Request body exceeds 8 MiB"),
    "cut_text_outside_decoder": lambda: ValueError(STREAM_CUT_MESSAGE),
    "client_value_error": lambda: ClientValueError("Invalid gateway reply"),
    "api_connection": lambda: openai.APIConnectionError(request=fakes._PROVIDER_REQUEST),
    "api_error_no_cause": lambda: openai.APIError("x", fakes._PROVIDER_REQUEST, body=None),
    "api_error_gateway_cause": lambda: _api_error_from(err_gateway()),
    "key_error": lambda: KeyError("delta"),
}


class TestInfrastructurePropagates(unittest.TestCase):
    def test_non_provider_failures_propagate_on_both_surfaces(self):
        """Client / gateway WitnessErrors, ValueErrors not raised by the decoder (even with the stream-cut text), and
        APIErrors without a provider WitnessError cause are not recorded outcomes: the same object propagates out of
        generate() at gen, refine and the refine retry, with no further call (0.3.0 C1)."""
        for name, mk in INFRA.items():
            for table, steps in (({("gen", 0): None}, [("gen", 0)]),
                                 ({("gen", 0): ok(GEN), ("refine", 0): None}, [("gen", 0), ("refine", 0)]),
                                 ({("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): None},
                                  [("gen", 0), ("refine", 0), ("refine", 1)])):
                for surface in SURFACES:
                    exc = mk()
                    t = {k: (exc if v is None else v) for k, v in table.items()}
                    w = FakeWitness(script_from(t), surface)
                    with self.assertRaises(type(exc), msg=(name, surface)) as cm, \
                            contextlib.redirect_stderr(io.StringIO()):
                        run_detailed(IMAGE, SEED, w, FakeRenderer(), V2R)
                    self.assertIs(cm.exception, exc, (name, surface))
                    self.assertEqual(w.steps, steps, (name, surface))


# ------------------------------------------------------------------ the flow tests, re-run on the rc6 surface
class _OnRC6:
    """Run an inherited TestCase with FakeWitness on rc6 (44d2a64 / 92bedb6) instead of DEFAULT_SURFACE (29a0d82)."""

    def setUp(self):
        super().setUp()
        previous = fakes.DEFAULT_SURFACE
        fakes.DEFAULT_SURFACE = SURFACE_RC6
        self.addCleanup(setattr, fakes, "DEFAULT_SURFACE", previous)

    def test_runs_on_rc6(self):
        self.assertEqual(FakeWitness(script_from({})).surface, SURFACE_RC6)


class TestV1OnRC6(_OnRC6, tp.TestV1):
    pass


class TestV2Front4OnRC6(_OnRC6, tp.TestV2Front4):
    pass


class TestV22RefineCapOnRC6(_OnRC6, tp.TestV22RefineCap):
    pass


class TestV2GridOnRC6(_OnRC6, tp.TestV2Grid):
    pass


class TestBudgetOnRC6(_OnRC6, tp.TestBudget):
    pass


class TestDeterminismOnRC6(_OnRC6, tp.TestDeterminism):
    pass


class TestRejectionInfoOnRC6(_OnRC6, tp.TestRejectionInfo):
    pass


class TestRenderInfraPropagatesOnRC6(_OnRC6, tp.TestRenderInfraPropagates):
    pass


class TestRetryPathsOnRC6(_OnRC6, trr.TestRetryPaths):
    pass


class TestRetryBudgetOnRC6(_OnRC6, trr.TestRetryBudget):
    pass


class TestCommittedSettingsOnRC6(_OnRC6, trr.TestCommittedSettings):
    pass


if __name__ == "__main__":
    unittest.main()

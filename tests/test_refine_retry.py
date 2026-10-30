"""Refine retry (0.2.2, REFINE_RETRY; variant v2r).

v2r = v2 / f1 / front4 / refine cap 4M + REFINE_RETRY: after a RECORDED retryable refine failure (f2's rule) or an
HTTP-200 stream cut without a terminal event (VPS_HOST.md F3), exactly ONE more refine call whose body differs only
in the tag's attempt digit, if recorded spend + its worst case fits the refine cap. Never after a provider rejection,
another undecodable record, a credit/quota code, a non-provider failure, or a structural / render rejection. On every
path without a retry, v2r sends exactly 0.2.1's (= 0.2.0's) requests and takes 0.2.1's branches."""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import unittest

from fakes import (FIXTURE_REFINE_RETRY, FIXTURE_TICKET, FIXTURE_TIER_FRACTION, IMAGE, PNG, REJECT, SEED, FakeRenderer,
                   FakeWitness, err_failed, err_gateway, err_http, err_incomplete, err_invalid, err_stream, err_stream_cut,
                   err_stream_cut_mid_text, err_transport, f3_stream, fenced, module, ok, script_from)
from test_requests import CODE, FIXED_PNGS, FROZEN_BODY, bodies
from witness_client import WitnessError
from witness_client.request import serialize_body

from sn17gen import settings, views
from sn17gen.credits import call_credits, worst_credits
from sn17gen.pipeline import STREAM_CUT_MESSAGE, refine_retry_reason, refine_retryable, run, run_detailed, stream_cut
from sn17gen.repair import model_source, repair
from sn17gen.reqbuild import build_gen_params, build_refine_params, data_url, prompt_id
from sn17gen.settings import make_settings


def fx(*args, retry=True, **kw):
    kw.setdefault("service_tier_fraction", FIXTURE_TIER_FRACTION)
    return make_settings(*args, ticket=FIXTURE_TICKET, refine_retry=retry, **kw)


V2R = fx("v2", "f1", "front4")                       # the v2r launch configuration with the fixture ticket
V2 = fx("v2", "f1", "front4", retry=False)          # 0.2.1 / 0.2.0 behaviour
V21 = fx("v2", "f2", "front4", retry=False)
V21R = fx("v2", "f2", "front4")
V2R_3M = fx("v2", "f1", "front4", refine_credit_cap=3_000_000)
GEN = fenced(module("336699"))
REF = fenced(module("884422"), prefix="- colour was off\n")
PID = prompt_id(IMAGE)
A0, A1 = b"step=refine attempt=0]", b"step=refine attempt=1]"
# sha256 of the v2r refine retry body for test_requests' fixed inputs (= FROZEN_BODY["refine_f1_front4"] with the tag's
# attempt digit 0 -> 1; pinned rc4 serializer)
FROZEN_RETRY_BODY = "3dbf63630ad12cb49616cc06b0e2390c6beb5a3c3b1d5f255715a217cb2673dc"

RETRYABLE = {
    "stream_cut": err_stream_cut, "stream_cut_mid_text": err_stream_cut_mid_text,
    "http_500": lambda: err_http(500, "server_error"), "http_503": lambda: err_http(503, "server_error"),
    "stream_error_overloaded": lambda: err_stream("overloaded"), "failed": err_failed,
    "incomplete": err_incomplete, "incomplete_filter": lambda: err_incomplete(3_800, 100, reason="content_filter"),
}
NOT_RETRYABLE = {
    "http_400": lambda: err_http(400), "http_401": lambda: err_http(401), "http_403": lambda: err_http(403),
    "http_404": lambda: err_http(404), "http_429": lambda: err_http(429, "rate_limit_exceeded"),
    "http_429_quota": lambda: err_http(429, "insufficient_quota"), "http_409": lambda: err_http(409, "conflict"),
    "stream_error_quota": lambda: err_stream("quota"),
    "invalid_partial_frame": lambda: err_invalid("partial_frame"), "invalid_inconsistent": lambda: err_invalid("inconsistent"),
    "invalid_two_terminals": lambda: err_invalid("two_terminals"), "invalid_bad_utf8": lambda: err_invalid("bad_utf8"),
    "invalid_no_cause": lambda: WitnessError("Invalid recorded OpenAI response", "x", source="provider",
                                             code="invalid_provider_response"),
    "transport": err_transport, "gateway": err_gateway,
}
INFRA = ("transport", "gateway")                     # source client / gateway: the runtime's (0.3.0 C1)
RECORDED_NOT_RETRYABLE = {k: v for k, v in NOT_RETRYABLE.items() if k not in INFRA}


def rjs(text: str) -> str:
    return repair(text, version="r3").js


def go(s, table, renderer=None, image=IMAGE):
    w = FakeWitness(script_from(table))
    r = renderer or FakeRenderer()
    with contextlib.redirect_stderr(io.StringIO()):
        out, p = run_detailed(image, SEED, w, r, s)
    return out, w, r, p


def refine_worst(s, gen_text=GEN):
    return worst_credits(build_refine_params(s, PID, SEED, data_url(IMAGE), model_source(gen_text), [PNG + bytes(64)] * 4))


def one_byte_apart(a: bytes, b: bytes) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


class TestClassification(unittest.TestCase):
    def test_stream_cut_is_exactly_the_decoders_missing_terminal_outcome(self):
        for name in ("stream_cut", "stream_cut_mid_text"):
            e = RETRYABLE[name]()
            self.assertEqual((e.source, e.code, e.status_code, e.response), ("provider", "invalid_provider_response", None, None))
            self.assertEqual((type(e.__cause__), e.__cause__.args), (ValueError, (STREAM_CUT_MESSAGE,)))
            self.assertTrue(stream_cut(e), name)
            self.assertFalse(refine_retryable(e), name)            # f2's rule alone never retries it
        for name, mk in NOT_RETRYABLE.items():
            self.assertFalse(stream_cut(mk()), name)
        for name in ("failed", "incomplete", "http_503", "stream_error_overloaded"):
            self.assertFalse(stream_cut(RETRYABLE[name]()), name)
        # same decoder outcome but not from the provider (e.g. a client-side wrapper) -> not a recorded stream cut
        e = err_stream_cut()
        e.source = "client"
        self.assertFalse(stream_cut(e))
        self.assertIn(b"response.output_item.added", f3_stream())
        self.assertNotIn(b"response.completed", f3_stream())

    def test_retry_reason_table(self):
        for name, mk in RETRYABLE.items():
            self.assertIsNotNone(refine_retry_reason(mk(), V2R), name)
            want_f2 = None if name.startswith("stream_cut") else mk().code
            self.assertEqual(refine_retry_reason(mk(), V21), want_f2, name)   # f2 without REFINE_RETRY = 0.2.1's rule
        self.assertEqual(refine_retry_reason(err_stream_cut(), V2R), "stream_cut")
        self.assertEqual(refine_retry_reason(err_stream_cut(), V21R), "stream_cut")
        for name, mk in NOT_RETRYABLE.items():
            for s in (V2R, V21R, V21):
                self.assertIsNone(refine_retry_reason(mk(), s), (name, s.label))


class TestRequestBytes(unittest.TestCase):
    def test_normal_path_bodies_are_0_2_0_bytes(self):
        """v2r's gen and refine attempt-0 requests = 0.2.0's frozen table (FROZEN_BODY, unchanged since 0.2.0)."""
        pid, url = prompt_id(IMAGE), data_url(IMAGE)
        got = {"gen_a0": serialize_body(build_gen_params(V2R, pid, SEED, url, 0), "api"),
               "refine_f1_front4": serialize_body(build_refine_params(V2R, pid, SEED, url, CODE, FIXED_PNGS), "api")}
        self.assertEqual({k: hashlib.sha256(v).hexdigest() for k, v in got.items()},
                         {k: FROZEN_BODY[k] for k in got})
        self.assertEqual(got["refine_f1_front4"], bodies()["refine_f1_front4"])

    def test_retry_body_differs_only_in_the_attempt_digit(self):
        a0 = serialize_body(build_refine_params(V2R, PID, SEED, data_url(IMAGE), CODE, FIXED_PNGS), "api")
        a1 = serialize_body(build_refine_params(V2R, PID, SEED, data_url(IMAGE), CODE, FIXED_PNGS, attempt=1), "api")
        self.assertEqual(a1, a0.replace(A0, A1))
        self.assertTrue(one_byte_apart(a0, a1))
        self.assertEqual(hashlib.sha256(a1).hexdigest(), FROZEN_RETRY_BODY)
        with self.assertRaises(ValueError):
            build_refine_params(V2R, PID, SEED, data_url(IMAGE), CODE, FIXED_PNGS, attempt=2)    # never a third call
        with self.assertRaises(ValueError):
            build_refine_params(V2, PID, SEED, data_url(IMAGE), CODE, FIXED_PNGS, attempt=1)     # 0.2.1 f1: no retry



class TestRetryPaths(unittest.TestCase):
    def test_every_retryable_failure_gives_exactly_one_retry(self):
        for name, mk in RETRYABLE.items():
            out, w, r, p = go(V2R, {("gen", 0): ok(GEN), ("refine", 0): mk(), ("refine", 1): ok(REF)})
            self.assertEqual(out, rjs(REF), name)
            self.assertEqual(w.steps, [("gen", 0), ("refine", 0), ("refine", 1)], name)
            self.assertEqual(w.bodies[2], w.bodies[1].replace(A0, A1), name)
            self.assertTrue(one_byte_apart(w.bodies[1], w.bodies[2]), name)
            # the retry re-uses the gen module's front4 PNGs (no re-render); the refined module gets its check render
            self.assertEqual([c[0] for c in r.calls], [rjs(GEN)] * 4 + [rjs(REF)], name)
            self.assertEqual(p.final, "refine")
            self.assertIn({"step": "refine", "attempt": 0, "outcome": "retry",
                           "reason": "stream_cut" if name.startswith("stream_cut") else mk().code}, p.events)
            # 0.2.1 (REFINE_RETRY False) on the same records: one refine call, gen module kept, same first two bodies
            o0, w0, r0, _ = go(V2, {("gen", 0): ok(GEN), ("refine", 0): mk()})
            self.assertEqual((o0, w0.steps), (rjs(GEN), [("gen", 0), ("refine", 0)]), name)
            self.assertEqual(w0.bodies, w.bodies[:2], name)

    def test_never_retried(self):
        for name, mk in RECORDED_NOT_RETRYABLE.items():
            out, w, r, p = go(V2R, {("gen", 0): ok(GEN), ("refine", 0): mk()})
            self.assertEqual((out, w.steps), (rjs(GEN), [("gen", 0), ("refine", 0)]), name)
            o0, w0, r0, _ = go(V2, {("gen", 0): ok(GEN), ("refine", 0): mk()})
            self.assertEqual((o0, w0.bodies, r0.calls), (out, w.bodies, r.calls), name)          # = 0.2.1
            self.assertNotIn("retry", [e.get("outcome") for e in p.events], name)

    def test_no_retry_after_a_rejected_reply_or_module(self):
        bad = fenced(module(marker=REJECT))
        for reply in (ok("I cannot improve it."), ok(bad)):
            out, w, r, p = go(V2R, {("gen", 0): ok(GEN), ("refine", 0): reply})
            self.assertEqual((out, w.steps), (rjs(GEN), [("gen", 0), ("refine", 0)]))
            o0, w0, r0, _ = go(V2, {("gen", 0): ok(GEN), ("refine", 0): reply})
            self.assertEqual((o0, w0.bodies, r0.calls), (out, w.bodies, r.calls))
        # nor after the RETRY's reply is rejected (structural or render): still exactly two refine calls
        for reply in (ok("no code"), ok(bad)):
            out, w, _, _ = go(V2R, {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): reply})
            self.assertEqual((out, w.steps), (rjs(GEN), [("gen", 0), ("refine", 0), ("refine", 1)]))

    def test_infrastructure_failures_propagate_to_the_runtime(self):
        """0.3.0 (C1): a client / gateway WitnessError is not a recorded outcome: it propagates out of run() at gen, at
        refine and at the refine retry, with no further call, as in 404's reference (the runtime recovers / resumes)."""
        for name in INFRA:
            mk = NOT_RETRYABLE[name]
            for table, steps in (({("gen", 0): mk()}, [("gen", 0)]),
                                 ({("gen", 0): ok(GEN), ("refine", 0): mk()}, [("gen", 0), ("refine", 0)]),
                                 ({("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): mk()},
                                  [("gen", 0), ("refine", 0), ("refine", 1)])):
                w = FakeWitness(script_from(table))
                with self.assertRaises(WitnessError, msg=name) as cm, contextlib.redirect_stderr(io.StringIO()):
                    run_detailed(IMAGE, SEED, w, FakeRenderer(), V2R)
                self.assertNotEqual(cm.exception.source, "provider", name)
                self.assertEqual(w.steps, steps, name)

    def test_a_failed_retry_keeps_the_gen_module(self):
        for name, mk in list(RETRYABLE.items()) + list(RECORDED_NOT_RETRYABLE.items()):
            out, w, _, p = go(V2R, {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): mk()})
            self.assertEqual((out, p.final, w.steps), (rjs(GEN), "gen", [("gen", 0), ("refine", 0), ("refine", 1)]), name)

    def test_0_2_1_settings_never_retry_a_stream_cut(self):
        out, w, _, _ = go(V2, {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut()})
        self.assertEqual((out, w.steps), (rjs(GEN), [("gen", 0), ("refine", 0)]))
        out, w, _, _ = go(V21, {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut()})        # f2 alone: no
        self.assertEqual(w.steps, [("gen", 0), ("refine", 0)])
        out, w, _, _ = go(V21R, {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): ok(REF)})
        self.assertEqual((out, w.steps), (rjs(REF), [("gen", 0), ("refine", 0), ("refine", 1)]))

    def test_paths_without_a_refine_failure_are_0_2_1(self):
        bad = fenced(module(marker=REJECT))
        tables = [
            {("gen", 0): ok(GEN), ("refine", 0): ok(REF)},
            {("gen", 0): ok("no code"), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)},
            {("gen", 0): ok(bad), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)},
            {("gen", 0): err_incomplete(3_800, 24_000), ("gen", 1): ok(GEN, 3_800, 20_000), ("refine", 0): ok(REF)},
            {("gen", 0): err_incomplete(3_800, 24_000), ("gen", 1): ok(GEN, 3_900, 31_000)},      # refine budget skip
            {("gen", 0): err_http(500, "server_error"), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)},
            {("gen", 0): err_stream("overloaded"), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)},
            {("gen", 0): ok(bad), ("gen", 1): ok(bad)},                                            # fallback
            {("gen", 0): err_http(400), ("gen", 1): err_http(400)},
        ]
        for t in tables:
            a, b = go(V2R, t), go(V2, t)
            self.assertEqual((a[0], a[1].bodies, a[2].calls, a[3].final), (b[0], b[1].bodies, b[2].calls, b[3].final))
            self.assertEqual(a[3].ledger.spent, b[3].ledger.spent)

    def test_gen_stream_cut_keeps_the_gen_requests(self):
        """A cut gen attempt is retried as attempt 1 anyway (any recorded gen failure is); v2r charges the cut its worst
        case, which never changes the gen requests (two gen attempts always fit 3M)."""
        t = {("gen", 0): err_stream_cut(), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)}
        a, b = go(V2R, t), go(V2, t)
        self.assertEqual((a[0], a[1].bodies, a[2].calls), (b[0], b[1].bodies, b[2].calls))
        self.assertEqual(a[3].ledger.unknown_usage_calls, 1)
        self.assertEqual(b[3].ledger.unknown_usage_calls, 0)
        self.assertEqual(a[3].ledger.spent - b[3].ledger.spent, worst_credits(a[1].params[0]))


class TestRetryBudget(unittest.TestCase):
    def test_stream_cut_is_charged_its_worst_case(self):
        _, w, _, p = go(V2R, {("gen", 0): ok(GEN, 3_800, 5_000), ("refine", 0): err_stream_cut(), ("refine", 1): ok(REF, 9_000, 6_000)})
        self.assertEqual(p.ledger.spent, call_credits("gpt-6-astra", 3_800, 5_000) + worst_credits(w.params[1])
                         + call_credits("gpt-6-astra", 9_000, 6_000))
        self.assertEqual(p.ledger.unknown_usage_calls, 1)
        # a 5xx (no response, no billed tokens: sn17miner F2 rule) is charged 0, as in 0.2.1
        _, w, _, p = go(V2R, {("gen", 0): ok(GEN, 3_800, 5_000), ("refine", 0): err_http(503, "server_error"), ("refine", 1): ok(REF, 9_000, 6_000)})
        self.assertEqual(p.ledger.spent, call_credits("gpt-6-astra", 3_800, 5_000) + call_credits("gpt-6-astra", 9_000, 6_000))

    def test_retry_skipped_when_its_worst_case_does_not_fit(self):
        rw = refine_worst(V2R)
        # gen spend g with g + rw <= 4M < g + 2 rw: attempt 0 runs, the cut is charged rw, the retry does not fit
        gen_in, gen_out = 3_800, 20_000
        g = call_credits("gpt-6-astra", 3_800, 24_000) + call_credits("gpt-6-astra", gen_in, gen_out)
        self.assertTrue(g + rw <= 4_000_000 < g + 2 * rw)
        t = {("gen", 0): err_incomplete(3_800, 24_000), ("gen", 1): ok(GEN, gen_in, gen_out), ("refine", 0): err_stream_cut(),
             ("refine", 1): ok(REF)}
        out, w, _, p = go(V2R, t)
        self.assertEqual((out, w.steps), (rjs(GEN), [("gen", 0), ("gen", 1), ("refine", 0)]))
        self.assertEqual(p.events[-1], {"step": "refine", "attempt": 1, "outcome": "budget_stop", "worst": str(rw),
                                        "spent": str(g + rw), "cap": 4_000_000})
        self.assertLessEqual(p.ledger.spent, 4_000_000)
        # the same records after a 503 (charged 0): the retry fits and runs
        t5 = dict(t)
        t5[("refine", 0)] = err_http(503, "server_error")
        out, w, _, _ = go(V2R, t5)
        self.assertEqual((out, w.steps[-1]), (rjs(REF), ("refine", 1)))
        # under a 3M refine cap even attempt 0 does not fit: no refine at all (0.1.0's rule)
        out, w, _, _ = go(V2R_3M, t)
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1)])

    def test_worst_case_cap_holds_on_every_retry_path(self):
        """Every refine call billed at its FULL worst case (the cut at its worst case by the ledger rule): the per-prompt
        spend never exceeds 4M, the retry runs exactly when it fits, and gen bodies equal 0.2.1's."""
        rw = refine_worst(V2R)
        seen = set()
        for gen_out in (500, 4_000, 8_000, 12_000, 16_000, 20_000, 24_000, 28_000, 32_000):
            for capped in (False, True):
                for fail in ("stream_cut", "http_503", "failed"):
                    def script(step, attempt, params, gen_out=gen_out, capped=capped, fail=fail):
                        if step == "gen":
                            if capped and attempt == 0:
                                return err_incomplete(3_800, 24_000)
                            return ok(GEN, 3_800, min(gen_out, params["max_output_tokens"]))
                        if attempt == 0:
                            w_ = worst_credits(params)
                            it = int((w_ - 50 * params["max_output_tokens"]) / 10)
                            return {"stream_cut": err_stream_cut(), "http_503": err_http(503, "server_error"),
                                    "failed": err_failed(it, params["max_output_tokens"])}[fail]
                        w_ = worst_credits(params)
                        return ok(REF, int((w_ - 50 * params["max_output_tokens"]) / 10), params["max_output_tokens"])
                    wv = FakeWitness(script)
                    with contextlib.redirect_stderr(io.StringIO()):
                        out, p = run_detailed(IMAGE, SEED, wv, FakeRenderer(), V2R)
                    self.assertLessEqual(p.ledger.spent, 4_000_000, (gen_out, capped, fail))
                    gen_spent = sum(call_credits("gpt-6-astra", 3_800, 24_000 if (capped and a == 0) else
                                                 min(gen_out, q["max_output_tokens"]))
                                    for q, (st, a) in zip(wv.params, wv.steps) if st == "gen")
                    charged0 = 0 if fail == "http_503" else rw
                    n_ref = sum(st == "refine" for st, _ in wv.steps)
                    if gen_spent + rw > 4_000_000:
                        want = 0
                    elif gen_spent + charged0 + rw > 4_000_000:
                        want = 1
                    else:
                        want = 2
                    self.assertEqual(n_ref, want, (gen_out, capped, fail))
                    self.assertEqual(p.final, "refine" if want == 2 else "gen")
                    seen.add(want)
                    w0 = FakeWitness(script)
                    with contextlib.redirect_stderr(io.StringIO()):
                        run_detailed(IMAGE, SEED, w0, FakeRenderer(), V2)
                    self.assertEqual([b for b, (st, _) in zip(wv.bodies, wv.steps) if st == "gen"],
                                     [b for b, (st, _) in zip(w0.bodies, w0.steps) if st == "gen"])
        self.assertEqual(seen, {0, 1, 2})             # the grid reaches all three outcomes


class TestCommittedSettings(unittest.TestCase):
    def test_committed_refine_retry(self):
        self.assertIs(settings.REFINE_RETRY, True)
        self.assertEqual(settings.ACTIVE.refine_retry, settings.MODE == "v2")
        self.assertEqual(settings.ACTIVE.refine_max_attempts, 2 if settings.MODE == "v2" else 0)
        self.assertFalse(FIXTURE_REFINE_RETRY)
        # the committed settings send 0.2.0's bytes on the normal path (fixture ticket so the frozen table applies)
        act = dataclasses.replace(settings.ACTIVE, ticket=FIXTURE_TICKET)
        if settings.MODE == "v2" and settings.SERVICE_TIER_FRACTION == 0:
            self.assertEqual(serialize_body(build_refine_params(act, PID, SEED, data_url(IMAGE), CODE, FIXED_PNGS), "api"),
                             bodies()["refine_f1_front4"])

    def test_settings_validation(self):
        self.assertEqual((V2R.refine_max_attempts, V2.refine_max_attempts, V21.refine_max_attempts, V21R.refine_max_attempts),
                         (2, 1, 2, 2))
        self.assertEqual((V2R.label, V2.label), ("v2-f1-front4-rr", "v2-f1-front4"))
        self.assertFalse(make_settings("v1", refine_retry=True).refine_retry)              # ignored: no refine step
        self.assertTrue(make_settings("v2").refine_retry)                                    # default = committed
        for bad in (1, 0, "yes", None):
            with self.assertRaises(ValueError, msg=bad):
                dataclasses.replace(V2R, refine_retry=bad)
        with self.assertRaises(ValueError):
            dataclasses.replace(make_settings("v1"), refine_retry=True)

    def test_diagnostic_line(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run(IMAGE, SEED, FakeWitness(script_from({("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(),
                                                      ("refine", 1): ok(REF)})), FakeRenderer(), V2R)
        line = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual((line["sn17gen"], line["refine_retry"], line["final"], line["version"], line["calls"],
                          line["unknown_usage_calls"]), ("v2-f1-front4-rr", True, "refine", "0.3.1", 3, 1))
        self.assertIn({"step": "refine", "attempt": 0, "outcome": "retry", "reason": "stream_cut"}, line["events"])

    def test_front4_views_are_the_retry_images(self):
        import base64
        _, w, _, _ = go(V2R, {("gen", 0): ok(GEN), ("refine", 0): err_stream_cut(), ("refine", 1): ok(REF)})
        for k in (1, 2):
            sent = [base64.b64decode(p["image_url"].split(",", 1)[1]) for p in w.params[k]["input"][0]["content"][4:8]]
            self.assertEqual(sent, [FakeRenderer().render(rjs(GEN), o) for o in views.front4_options()])


if __name__ == "__main__":
    unittest.main()

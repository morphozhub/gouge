"""Pipeline flows with the fake clients: v1, v2 front4 / grid, refine f1 / f2, retries, budget, fallback."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import unittest
from fractions import Fraction

from fakes import (FIXTURE_REFINE_RETRY, FIXTURE_TICKET, FIXTURE_TIER_FRACTION, IMAGE, PNG, REJECT, SEED, FakeRenderer, FakeWitness, err_failed, err_http, err_incomplete,
                   err_stream, fenced, module, ok, script_from)

from renderer_client import RenderRejected, RenderUnavailable

from sn17gen import views
from sn17gen.contract import REFINE_LABELS, REFINE_LABELS_BY_VIEWS, REFINE_TEXT, REFINE_TEXT_BY_VIEWS, RETRY_COMPACT_TEXT
from sn17gen.credits import call_credits, worst_credits
from sn17gen.pipeline import refine_retryable, run, run_detailed
from sn17gen.repair import fallback_module, model_source, repair
from sn17gen.reqbuild import VARIANT_OUT_CAP, build_gen_params, build_refine_params, data_url, prompt_id
from sn17gen.settings import make_settings

V1 = make_settings("v1", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
V2 = make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
V2G = make_settings("v2", "f1", "grid", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
V21 = make_settings("v2", "f2", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
# submission 0.1.0 / sn17miner v2 refine budget (3M) for comparisons with v2.2's 4M refine cap
V2_3M = make_settings("v2", "f1", "front4", refine_credit_cap=3_000_000, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
V2G_3M = make_settings("v2", "f1", "grid", refine_credit_cap=3_000_000, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
V21_3M = make_settings("v2", "f2", "front4", refine_credit_cap=3_000_000, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
GEN = fenced(module("336699"))
REF = fenced(module("884422"), prefix="- colour was off\n")
PID = prompt_id(IMAGE)
FRONT4_NORM = [{"thetas": f"{float(t)}", "phis": f"{float(p)}", "img_size": "512", "cam_radius": "2.0", "cam_fov_deg": "49.1",
                "gap": "5", "bg_color": "ffffff", "lighting": "neutral"}
               for t, p in ((30, 0), (330, 0), (0, 15), (0, -30))]


def rjs(text: str) -> str:
    return repair(text, version="r3").js


def refine_worst(s, gen_text=GEN):
    """Worst case of the refine request for ``gen_text`` (PNG bytes never change it)."""
    pngs = [PNG + bytes(64)] * (4 if s.refine_views == "front4" else 1)
    return worst_credits(build_refine_params(s, PID, SEED, data_url(IMAGE), model_source(gen_text), pngs))


def go(settings, table, renderer=None):
    w = FakeWitness(script_from(table))
    r = renderer or FakeRenderer()
    with contextlib.redirect_stderr(io.StringIO()):
        out = run(IMAGE, SEED, w, r, settings)
    return out, w, r


class TestV1(unittest.TestCase):
    def test_accept_first_attempt(self):
        out, w, r = go(V1, {("gen", 0): ok(GEN)})
        self.assertEqual(out, rjs(GEN))
        self.assertEqual(w.steps, [("gen", 0)])
        self.assertEqual(r.calls, [(rjs(GEN), None)])           # validity check = the default preview
        text = w.params[0]["input"][0]["content"][0]["text"]
        self.assertTrue(text.startswith(f"[pid={PID} seed={SEED} ticket=t0 step=gen attempt=0] "))

    def test_structural_reject_then_accept(self):
        out, w, r = go(V1, {("gen", 0): ok("Sorry, no code."), ("gen", 1): ok(GEN)})
        self.assertEqual(out, rjs(GEN))
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1)])
        self.assertEqual(w.bodies[1], w.bodies[0].replace(b"step=gen attempt=0]", b"step=gen attempt=1]"))
        self.assertEqual(len(r.calls), 1)

    def test_render_rejected_twice_gives_the_rendered_fallback(self):
        bad = fenced(module(marker=REJECT))
        out, w, r = go(V1, {("gen", 0): ok(bad), ("gen", 1): ok(bad)})
        self.assertEqual(out, fallback_module(PID, "r3"))
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1)])
        self.assertEqual([c[0] for c in r.calls], [rjs(bad), rjs(bad), out])

    def test_nothing_valid_returns_none(self):
        out, w, r = go(V1, {("gen", 0): ok(GEN), ("gen", 1): ok(GEN)}, FakeRenderer(lambda s, o: "EMPTY_SCENE"))
        self.assertIsNone(out)
        self.assertEqual(len(r.calls), 3)                          # a0, a1, fallback

    def test_rp1_after_output_cap_asks_32k_with_compact_line(self):
        out, w, r = go(V1, {("gen", 0): err_incomplete(3_800, 24_000), ("gen", 1): ok(GEN)})
        self.assertEqual(out, rjs(GEN))
        self.assertEqual(w.params[1]["max_output_tokens"], 32_000)
        self.assertIn(RETRY_COMPACT_TEXT["rp1"], w.params[1]["input"][0]["content"][0]["text"])
        self.assertEqual(w.params[0]["max_output_tokens"], 24_000)

    def test_rp1_falls_back_to_24k_when_32k_does_not_fit(self):
        out, w, r = go(V1, {("gen", 0): err_incomplete(30_000, 24_000), ("gen", 1): ok(GEN)})
        self.assertEqual(w.params[1]["max_output_tokens"], 24_000)
        self.assertIn(RETRY_COMPACT_TEXT["rp1"], w.params[1]["input"][0]["content"][0]["text"])

    def test_budget_stop_when_nothing_fits(self):
        out, w, r = go(V1, {("gen", 0): err_incomplete(200_000, 24_000)})
        self.assertEqual(w.steps, [("gen", 0)])
        self.assertEqual(out, fallback_module(PID, "r3"))

    def test_incomplete_for_another_reason_is_a_plain_retry(self):
        out, w, r = go(V1, {("gen", 0): err_incomplete(3_800, 100, reason="content_filter"), ("gen", 1): ok(GEN)})
        self.assertEqual(w.bodies[1], w.bodies[0].replace(b"step=gen attempt=0]", b"step=gen attempt=1]"))

    def test_provider_rejections_give_the_fallback(self):
        out, w, r = go(V1, {("gen", 0): err_http(400), ("gen", 1): err_http(429, "rate_limit_exceeded")})
        self.assertEqual(out, fallback_module(PID, "r3"))
        self.assertEqual(len(r.calls), 1)

    def test_leftover_candidate_is_render_checked_once(self):
        # attempt 0 structurally rejected (a scannable module r3 flags), attempt 1 a provider failure
        flagged = fenced("export default function generate(THREE) { const c = true; let a = 2; if (c) -a ** 2; return new THREE.Group(); }\n")
        out, w, r = go(V1, {("gen", 0): ok(flagged), ("gen", 1): err_http(500, "server_error")})
        self.assertEqual(out, rjs(flagged))                       # the fake renderer accepts it
        self.assertEqual(len(r.calls), 1)


class TestV2Front4(unittest.TestCase):
    def test_refine_accepted(self):
        out, w, r = go(V2, {("gen", 0): ok(GEN), ("refine", 0): ok(REF)})
        self.assertEqual(out, rjs(REF))
        self.assertEqual(w.steps, [("gen", 0), ("refine", 0)])
        self.assertEqual([c[1] for c in r.calls], FRONT4_NORM + [FRONT4_NORM[0]])
        self.assertEqual([c[0] for c in r.calls], [rjs(GEN)] * 4 + [rjs(REF)])
        parts = w.params[1]["input"][0]["content"]
        self.assertEqual([p["type"] for p in parts],
                         ["input_text", "input_text", "input_image", "input_text"] + ["input_image"] * 4 + ["input_text"])
        self.assertTrue(parts[0]["text"].startswith(f"[pid={PID} seed={SEED} ticket=t0 step=refine attempt=0] "))
        self.assertTrue(parts[0]["text"].endswith(REFINE_TEXT["f1"]))
        self.assertEqual(parts[1]["text"], REFINE_LABELS["f1"]["reference"])
        self.assertEqual(parts[2], w.params[0]["input"][0]["content"][1])     # the same prompt data URL
        self.assertEqual(parts[3]["text"], REFINE_LABELS["f1"]["renders"])
        sent = [base64.b64decode(p["image_url"].split(",", 1)[1], validate=True) for p in parts[4:8]]
        self.assertEqual(sent, [FakeRenderer().render(rjs(GEN), o) for o in views.front4_options()])
        code = GEN.split("```javascript\n", 1)[1].split("```", 1)[0]
        self.assertEqual(parts[8]["text"], f"{REFINE_LABELS['f1']['code']}\n```javascript\n{code}```")

    def test_refined_module_rejected_keeps_gen(self):
        out, w, r = go(V2, {("gen", 0): ok(GEN), ("refine", 0): ok(fenced(module(marker=REJECT)))})
        self.assertEqual(out, rjs(GEN))
        self.assertEqual(len(r.calls), 5)

    def test_refine_without_code_keeps_gen(self):
        out, w, r = go(V2, {("gen", 0): ok(GEN), ("refine", 0): ok("I cannot improve it.")})
        self.assertEqual(out, rjs(GEN))
        self.assertEqual(len(r.calls), 4)

    def test_gen_rejected_then_accepted_refines_the_accepted_one(self):
        bad = fenced(module(marker=REJECT))
        out, w, r = go(V2, {("gen", 0): ok(bad), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)})
        self.assertEqual(out, rjs(REF))
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1), ("refine", 0)])
        self.assertEqual(len(r.calls), 1 + 4 + 1)                  # first front view rejected a0

    def test_no_valid_gen_no_refine(self):
        bad = fenced(module(marker=REJECT))
        out, w, r = go(V2, {("gen", 0): ok(bad), ("gen", 1): ok(bad)})
        self.assertEqual(out, fallback_module(PID, "r3"))
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1)])

    def test_f1_never_retries_a_recorded_refine_failure(self):
        for e in (err_stream("overloaded"), err_http(500, "server_error"), err_failed()):
            out, w, r = go(V2, {("gen", 0): ok(GEN), ("refine", 0): e})
            self.assertEqual(out, rjs(GEN))
            self.assertEqual(w.steps, [("gen", 0), ("refine", 0)])

    def test_f2_retries_once_after_a_retryable_failure(self):
        for e in (err_stream("overloaded"), err_http(503, "server_error"), err_failed(), err_incomplete()):
            out, w, r = go(V21, {("gen", 0): ok(GEN), ("refine", 0): e, ("refine", 1): ok(REF)})
            self.assertEqual(out, rjs(REF))
            self.assertEqual(w.steps, [("gen", 0), ("refine", 0), ("refine", 1)])
            self.assertEqual(w.bodies[2], w.bodies[1].replace(b"step=refine attempt=0]", b"step=refine attempt=1]"))
            self.assertTrue(w.params[1]["input"][0]["content"][0]["text"].endswith(REFINE_TEXT["f2"]))

    def test_f2_no_retry_after_rejection_or_quota(self):
        for e in (err_http(400), err_http(429, "insufficient_quota"), err_stream("quota")):
            out, w, r = go(V21, {("gen", 0): ok(GEN), ("refine", 0): e})
            self.assertEqual(out, rjs(GEN))
            self.assertEqual(w.steps, [("gen", 0), ("refine", 0)])

    def test_refine_skipped_when_it_cannot_fit(self):
        # gen attempt 0 hits the cap, the rp1 32k retry is accepted with a big usage: refine cannot fit the 4M refine cap
        table = {("gen", 0): err_incomplete(3_800, 24_000), ("gen", 1): ok(GEN, 3_900, 31_000)}
        out, w, r = go(V2, table)
        self.assertEqual(out, rjs(GEN))
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1)])
        gen_spent = call_credits("gpt-6-astra", 3_800, 24_000) + call_credits("gpt-6-astra", 3_900, 31_000)
        self.assertGreater(gen_spent + refine_worst(V2), 4_000_000)
        with contextlib.redirect_stderr(io.StringIO()):
            _, p = run_detailed(IMAGE, SEED, FakeWitness(script_from(table)), FakeRenderer(), V2)
        self.assertEqual(p.events[-1], {"step": "refine", "attempt": 0, "outcome": "budget_stop",
                                        "worst": str(refine_worst(V2)), "spent": str(gen_spent), "cap": 4_000_000})


class TestV22RefineCap(unittest.TestCase):
    """sn17miner v2.2 (R7): the refine call may run while recorded spend + its worst case <= 4,000,000 credits; every
    gen call and the rp1 plan keep 3,000,000. 3M variants = submission 0.1.0."""

    # gen attempt 0 hits the output cap (1,238,000), the rp1 32k retry is accepted with 20k out (1,038,000)
    TABLE = {("gen", 0): err_incomplete(3_800, 24_000), ("gen", 1): ok(GEN, 3_800, 20_000), ("refine", 0): ok(REF)}

    def test_refine_runs_where_the_3m_rule_skipped_it(self):
        gen_spent = call_credits("gpt-6-astra", 3_800, 24_000) + call_credits("gpt-6-astra", 3_800, 20_000)
        self.assertTrue(3_000_000 < gen_spent + refine_worst(V2) <= 4_000_000)
        old, wo, ro = go(V2_3M, self.TABLE)
        new, wn, rn = go(V2, self.TABLE)
        self.assertEqual((old, wo.steps), (rjs(GEN), [("gen", 0), ("gen", 1)]))
        self.assertEqual((new, wn.steps), (rjs(REF), [("gen", 0), ("gen", 1), ("refine", 0)]))
        self.assertEqual(wn.bodies[:2], wo.bodies)                         # same gen requests
        self.assertEqual(wn.params[1]["max_output_tokens"], 32_000)
        self.assertEqual(rn.calls[:4], ro.calls)                          # same front4 renders of the gen module
        self.assertEqual(rn.calls[4], (rjs(REF), FRONT4_NORM[0]))         # the refined module's check
        # the refine body is what the 3M rule would have sent had it fitted
        code = model_source(GEN)
        pngs = [FakeRenderer().render(rjs(GEN), o) for o in views.front4_options()]
        self.assertEqual(wn.params[2], build_refine_params(V2_3M, PID, SEED, data_url(IMAGE), code, pngs))

    def test_gen_keeps_the_3m_rule(self):
        # attempt 0 billed 1,600,000: the rp1 32k retry would fit 4M but not 3M -> it asks for 24k, as in 0.1.0
        table = {("gen", 0): err_incomplete(40_000, 24_000), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)}
        spent0 = call_credits("gpt-6-astra", 40_000, 24_000)
        w32 =worst_credits(build_gen_params(V2, PID, SEED, data_url(IMAGE), 1, variant=VARIANT_OUT_CAP, max_output_tokens=32_000))
        w24 = worst_credits(build_gen_params(V2, PID, SEED, data_url(IMAGE), 1, variant=VARIANT_OUT_CAP))
        self.assertTrue(spent0 + w24 <= 3_000_000 < spent0 + w32 <= 4_000_000)
        new, wn, _ = go(V2, table)
        old, wo, _ = go(V2_3M, table)
        self.assertEqual(wn.params[1]["max_output_tokens"], 24_000)
        self.assertEqual(wn.bodies[:2], wo.bodies[:2])
        self.assertEqual((wn.steps, wo.steps), ([("gen", 0), ("gen", 1), ("refine", 0)], [("gen", 0), ("gen", 1)]))
        # and a gen attempt that fits only the refine cap is never made
        table2 = {("gen", 0): err_incomplete(100_000, 24_000)}             # 2,200,000 spent: no attempt 1 fits 3M
        self.assertTrue(call_credits("gpt-6-astra", 100_000, 24_000) + w24 <= 4_000_000)
        out, w2, _ = go(V2, table2)
        self.assertEqual((w2.steps, out), ([("gen", 0)], fallback_module(PID, "r3")))

    def test_diagnostic_line_reports_both_caps(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run(IMAGE, SEED, FakeWitness(script_from(self.TABLE)), FakeRenderer(), V2)
        line = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual((line["caps"], line["final"], line["version"]), ({"gen": 3_000_000, "refine": 4_000_000}, "refine", "0.3.1"))


class TestV2Grid(unittest.TestCase):
    def test_grid_refine_uses_the_default_preview(self):
        out, w, r = go(V2G, {("gen", 0): ok(GEN), ("refine", 0): ok(REF)})
        self.assertEqual(out, rjs(REF))
        self.assertEqual(r.calls, [(rjs(GEN), None), (rjs(REF), None)])
        parts = w.params[1]["input"][0]["content"]
        self.assertEqual([p["type"] for p in parts],
                         ["input_text", "input_text", "input_image", "input_text", "input_image", "input_text"])
        self.assertTrue(parts[0]["text"].endswith(REFINE_TEXT_BY_VIEWS[("f1", "grid")]))
        self.assertEqual(parts[3]["text"], REFINE_LABELS_BY_VIEWS[("f1", "grid")]["renders"])
        self.assertEqual(base64.b64decode(parts[4]["image_url"].split(",", 1)[1]), FakeRenderer().render(rjs(GEN)))

    def test_grid_has_no_f2_variant(self):
        with self.assertRaises(ValueError):
            make_settings("v2", "f2", "grid")


class TestBudget(unittest.TestCase):
    def test_worst_case_cap_holds_in_every_path(self):
        """Gen output 500..24k (and an rp1 32k retry); every refine call (and f2's failed attempt) billed at its
        FULL worst case: gen spend never exceeds credit_cap (3M), the per-prompt spend never exceeds the refine cap
        (v2.2 4M; 0.1.0 3M), and refine runs whenever it fits that cap. The gen calls are the same under both caps."""
        def full(params):
            w = worst_credits(params)
            mot = params["max_output_tokens"]
            it = (w - 50 * mot) / 10
            self.assertEqual(it.denominator, 1)
            return int(it), mot
        paths = {}
        rescued = 0
        for s in (V2, V2G, V21, V2_3M, V2G_3M, V21_3M):
            for gen_out in (500, 8_000, 16_000, 20_000, 24_000):
                for capped in (False, True):
                    def script(step, attempt, params, gen_out=gen_out, capped=capped, s=s):
                        if step == "gen" and capped and attempt == 0:
                            return err_incomplete(3_800, 24_000)
                        if step == "gen":
                            return ok(GEN, 3_800, min(gen_out, params["max_output_tokens"]))
                        it, ot = full(params)
                        if attempt == 0 and s.refine_version == "f2":
                            return err_failed(it, ot)
                        return ok(REF, it, ot)
                    w = FakeWitness(script)
                    with contextlib.redirect_stderr(io.StringIO()):
                        out, p = run_detailed(IMAGE, SEED, w, FakeRenderer(), s)
                    cap = s.refine_credit_cap
                    self.assertLessEqual(p.ledger.spent, cap, (s.label, cap, gen_out, capped, w.steps))
                    gen_spent = sum(call_credits("gpt-6-astra", 3_800, 24_000 if (capped and a == 0) else
                                                 min(gen_out, q["max_output_tokens"]))
                                    for q, (st, a) in zip(w.params, w.steps) if st == "gen")
                    self.assertLessEqual(gen_spent, s.credit_cap)
                    rw = refine_worst(s)
                    n_ref = sum(1 for st, _ in w.steps if st == "refine")
                    tries = 2 if s.refine_version == "f2" else 1   # f2: a failed attempt 0 billed in full, then the retry
                    if gen_spent + tries * rw <= cap:
                        self.assertEqual((n_ref, p.final), (tries, "refine"), (s.label, cap, gen_out, capped))
                    elif gen_spent + rw <= cap:
                        self.assertEqual((n_ref, p.final), (1, "gen"), (s.label, cap, gen_out, capped))
                    else:
                        self.assertEqual((n_ref, p.final), (0, "gen"), (s.label, cap, gen_out, capped))
                    if cap > 3_000_000 and gen_spent + rw > 3_000_000 and n_ref:
                        rescued += 1
                    gen_bodies = [b for b, (st, _) in zip(w.bodies, w.steps) if st == "gen"]
                    key = (s.refine_version, s.refine_views, gen_out, capped)
                    paths.setdefault(key, gen_bodies)
                    self.assertEqual(paths[key], gen_bodies, key)            # the refine cap never touches gen
        self.assertGreater(rescued, 0)          # the grid reaches the (3M, 4M] region the 4M cap opens

    def test_refine_cap_is_checked_on_f2_retry_too(self):
        # gen 1,238,000 + 1,038,000 (rp1 32k retry, 20k out): refine attempt 0 fits 4M; it fails billed at its full
        # worst case, so the retry would exceed 4M -> no retry, gen module kept
        def script(step, attempt, params):
            if step == "gen":
                return err_incomplete(3_800, 24_000) if attempt == 0 else ok(GEN, 3_800, 20_000)
            w = worst_credits(params)
            return err_failed(int((w - 50 * params["max_output_tokens"]) / 10), params["max_output_tokens"])
        gen_spent = call_credits("gpt-6-astra", 3_800, 24_000) + call_credits("gpt-6-astra", 3_800, 20_000)
        rw = refine_worst(V21)
        self.assertTrue(3_000_000 < gen_spent + rw <= 4_000_000 < gen_spent + 2 * rw)
        w = FakeWitness(script)
        with contextlib.redirect_stderr(io.StringIO()):
            out, p = run_detailed(IMAGE, SEED, w, FakeRenderer(), V21)
        self.assertEqual(w.steps, [("gen", 0), ("gen", 1), ("refine", 0)])
        self.assertEqual((out, p.final), (rjs(GEN), "gen"))
        self.assertEqual(p.events[-1]["outcome"], "budget_stop")
        self.assertEqual((p.events[-1]["step"], p.events[-1]["attempt"], p.events[-1]["cap"]), ("refine", 1, 4_000_000))

    def test_credits_follow_the_readme(self):
        self.assertEqual(call_credits("gpt-6.1-sol", 10_000, 20_000), 220_000)
        self.assertEqual(call_credits("gpt-6-astra", 272_000, 1), 272_000 * 10 + 50)
        self.assertEqual(call_credits("gpt-6-astra", 272_001, 1), 272_001 * 20 + 75)
        self.assertEqual(call_credits("gpt-6-luna", 3, 3), Fraction(18, 10))


class TestDeterminism(unittest.TestCase):
    def test_same_records_same_requests_and_output(self):
        table = {("gen", 0): ok("no code"), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)}
        a = go(V2, table)
        b = go(V2, table)
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1].bodies, b[1].bodies)
        self.assertEqual(a[2].calls, b[2].calls)

    def test_seed_and_image_identify_the_requests(self):
        w1, w2, w3 = (FakeWitness(script_from({("gen", 0): ok(GEN)})) for _ in range(3))
        with contextlib.redirect_stderr(io.StringIO()):
            run(IMAGE, SEED, w1, FakeRenderer(), V1)
            run(IMAGE, SEED + 1, w2, FakeRenderer(), V1)
            run(IMAGE + b"\0", SEED, w3, FakeRenderer(), V1)
        self.assertNotEqual(w1.bodies[0], w2.bodies[0])
        self.assertNotEqual(w1.bodies[0], w3.bodies[0])
        self.assertEqual(w1.bodies[0].replace(f"seed={SEED} ".encode(), f"seed={SEED + 1} ".encode()), w2.bodies[0])

    def test_seed_validation(self):
        for bad in (-1, 2**64, True, 1.0, "7"):
            with self.assertRaises(ValueError):
                run(IMAGE, bad, FakeWitness(script_from({})), FakeRenderer(), V1)
        with contextlib.redirect_stderr(io.StringIO()):
            run(IMAGE, 2**64 - 1, FakeWitness(script_from({("gen", 0): ok(GEN)})), FakeRenderer(), V1)

    def test_refine_retryable_table(self):
        cases = [(err_http(500, "server_error"), True), (err_http(503, "server_error"), True),
                 (err_stream("overloaded"), True), (err_failed(), True), (err_incomplete(), True),
                 (err_http(400), False), (err_http(401), False), (err_http(429, "rate_limit_exceeded"), False),
                 (err_http(409, "conflict"), False), (err_stream("quota"), False)]
        for e, want in cases:
            self.assertIs(refine_retryable(e), want, (e.code, e.status_code, e.provider_code))


class TestRejectionInfo(unittest.TestCase):
    def test_reads_404s_422_report(self):
        """0.3.0 (C2): 404's RenderRejected carries the service's 422 JSON; (stage, rule) come from its first failure,
        else the error text; malformed or foreign objects give (None, None) and never raise."""
        r = RenderRejected(json.dumps({"error": "validation failed", "failures": [
            {"stage": "post_validation", "rule": "BOUNDING_BOX_OUT_OF_RANGE", "detail": "x"},
            {"stage": "static", "rule": "OTHER", "detail": "y"}], "truncated": False}))
        self.assertEqual(views.rejection_info(r), ("post_validation", "BOUNDING_BOX_OUT_OF_RANGE"))
        self.assertEqual(views.rejection_info(RenderRejected(json.dumps({"error": "static analysis failed", "failures": []}))),
                         (None, "static analysis failed"))
        self.assertEqual(views.rejection_info(object()), (None, None))
        self.assertEqual(views.rejection_info(RenderRejected(json.dumps({"error": "e", "failures": "bad"}))), (None, "e"))

    def test_rejected_gen_module_is_logged_and_the_next_attempt_runs(self):
        bad = fenced(module(marker=REJECT))
        w = FakeWitness(script_from({("gen", 0): ok(bad), ("gen", 1): ok(GEN), ("refine", 0): ok(REF)}))
        with contextlib.redirect_stderr(io.StringIO()):
            out, p = run_detailed(IMAGE, SEED, w, FakeRenderer(), V2)
        self.assertEqual(out, rjs(REF))
        ev = [e for e in p.events if e.get("outcome") == "render_rejected"]
        self.assertEqual([(e["stage"], e["rule"]) for e in ev], [("post_validation", "BOUNDING_BOX_OUT_OF_RANGE")])


class FlakyRenderer(FakeRenderer):
    """FakeRenderer whose ``fail_at``-th render call raises ``exc`` instead of answering."""

    def __init__(self, fail_at: int, exc: BaseException) -> None:
        super().__init__()
        self.n, self.fail_at, self.exc = 0, fail_at, exc

    def render(self, source, options=None):
        self.n += 1
        if self.n == self.fail_at:
            raise self.exc
        return super().render(source, options)


class TestRenderInfraPropagates(unittest.TestCase):
    """0.3.0 (review 10-25 F2): only the service's 422 (RenderRejected) is a recorded outcome. RenderUnavailable
    (transport, 503, a render crash or timeout) or any other renderer exception propagates out of run() at every render
    site of the committed configurations (v2 front4 and v1; the dormant grid refine mode is not covered), with no further
    witness call: the runtime owns it, and treating it as a rejection would pay for more calls and
    record a sequence the validator's replay cannot reproduce."""

    FLAGGED = fenced("export default function generate(THREE) { const c = true; let a = 2; if (c) -a ** 2; return new THREE.Group(); }\n")

    def cases(self):
        both = {("gen", 0): ok(GEN), ("refine", 0): ok(REF)}
        return [
            ("v2 gen: 1st front4 view", V2, both, 1, [("gen", 0)]),
            ("v2 gen: 3rd front4 view", V2, both, 3, [("gen", 0)]),
            ("v2 refined module check", V2, both, 5, [("gen", 0), ("refine", 0)]),
            ("v1 gen check", V1, {("gen", 0): ok(GEN)}, 1, [("gen", 0)]),
            ("v1 leftover check", V1, {("gen", 0): ok(self.FLAGGED), ("gen", 1): err_http(500, "server_error")}, 1,
             [("gen", 0), ("gen", 1)]),
            ("v1 fallback check", V1, {("gen", 0): err_http(400), ("gen", 1): err_http(429, "rate_limit_exceeded")}, 1,
             [("gen", 0), ("gen", 1)]),
        ]

    def test_renderer_failures_propagate_from_every_render_site(self):
        for name, s, table, at, steps in self.cases():
            for exc in (RenderUnavailable("renderer down"), RuntimeError("boom")):
                w, r = FakeWitness(script_from(table)), FlakyRenderer(at, exc)
                with self.assertRaises(type(exc), msg=name), contextlib.redirect_stderr(io.StringIO()):
                    run_detailed(IMAGE, SEED, w, r, s)
                self.assertEqual((w.steps, r.n), (steps, at), (name, type(exc).__name__))


if __name__ == "__main__":
    unittest.main()

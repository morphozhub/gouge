"""A3 (0.3.1): render artifacts per prompt stay within 9.

404's Pod (29a0d82 managed artifacts) stores every renderer.render() of a prompt as an artifact and refuses a new
distinct (source, options) artifact past the round's ``max_renders`` (429 artifact_storage_limit -> ArtifactUnavailable,
a RenderUnavailable -> it propagates out of generate() -> the prompt stays pending -> not_submitted). The final save
renders only a source the Pod holds no render of (find_render matches the source sha), and our final is always a source
this prompt already rendered and the renderer accepted, so it adds none. Worst case per prompt: 9 = gen attempt 0
front4 (4) + gen attempt 1 front4 (4) + the refined module's check (1), or 8 + the leftover / fallback check (1); v1 and
grid: 3. Renders within a prompt are sequential (one call at a time), so at most one artifact is pending at once.

The test enumerates every gen outcome pair (provider failure, output-cap stop, no code, a scannable module the
structural check rejects, a module the renderer rejects at front view 1 / 2 / 3 / 4, accepted), every refine outcome
(accepted, render-rejected, no code, rejected, and the retry paths) and an accepted or rejected fallback, under the
committed configuration (v2r, on both witness-client surfaces) and v2, f2, f2 + retry, v1, grid."""

from __future__ import annotations

import contextlib
import io
import json
import re
import unittest

from fakes import (DEFAULT_SURFACE, FIXTURE_TICKET, FIXTURE_TIER_FRACTION, IMAGE, SEED, SURFACES, FakeRenderer, FakeWitness, err_failed,
                   err_http, err_incomplete, err_stream_cut, fenced, module, ok, script_from)

from sn17gen import views
from sn17gen.pipeline import run_detailed
from sn17gen.repair import fallback_module
from sn17gen.reqbuild import prompt_id
from sn17gen.settings import make_settings

MAX_RENDERS_PER_PROMPT = 9          # 0.3.1's worst case; 404's reference: 10 per prompt; the example config: 16


def fx(*args, retry=False, **kw):
    return make_settings(*args, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=retry, **kw)


SETTINGS = {"v2r": (fx("v2", "f1", "front4", retry=True), MAX_RENDERS_PER_PROMPT),
            "v2": (fx("v2", "f1", "front4"), MAX_RENDERS_PER_PROMPT),
            "f2": (fx("v2", "f2", "front4"), MAX_RENDERS_PER_PROMPT),
            "f2r": (fx("v2", "f2", "front4", retry=True), MAX_RENDERS_PER_PROMPT),
            "v1": (fx("v1"), 3),
            "grid": (fx("v2", "f1", "grid"), 3)}
FALLBACK = fallback_module(prompt_id(IMAGE), "r3")
FRONT = [o.query() for o in views.front4_options()]
MARK = re.compile(r'const tag = "REJ([1-4])"')


def flagged(k: int) -> str:
    """A scannable module the structural check rejects (r3 flags it; it stays a leftover candidate)."""
    return fenced(f"export default function generate(THREE) {{ const c = true; let a = {k + 2}; if (c) -a ** 2; "
                  "return new THREE.Group(); }\n")


def gen_outcomes(k: int) -> dict:
    colour = ("336699", "993366")[k]
    out = {"fail": err_http(500, "server_error"), "cap": err_incomplete(), "nocode": ok("Sorry, no code."),
           "flagged": ok(flagged(k)), "accept": ok(fenced(module(colour)))}
    for v in (1, 2, 3, 4):
        out[f"rej{v}"] = ok(fenced(module(colour, f"REJ{v}")))
    return out


def refine_outcomes() -> dict:
    acc, rej = ok(fenced(module("884422"))), ok(fenced(module("884422", "REJ1")))
    acc1, rej1 = ok(fenced(module("117733"))), ok(fenced(module("117733", "REJ1")))
    return {"accept": {("refine", 0): acc}, "reject": {("refine", 0): rej}, "nocode": {("refine", 0): ok("no")},
            "http_400": {("refine", 0): err_http(400)},
            "cut_accept": {("refine", 0): err_stream_cut(), ("refine", 1): acc1},
            "cut_reject": {("refine", 0): err_stream_cut(), ("refine", 1): rej1},
            "cut_nocode": {("refine", 0): err_stream_cut(), ("refine", 1): ok("no")},
            "cut_503": {("refine", 0): err_stream_cut(), ("refine", 1): err_http(503, "server_error")},
            "failed_accept": {("refine", 0): err_failed(), ("refine", 1): acc1},
            "failed_reject": {("refine", 0): err_failed(), ("refine", 1): rej1}}


def rule_for(reject_fallback: bool):
    """Reject a module marked REJ<v> at front view v (the default preview counts as view 1); the fallback if asked."""
    def reject(src, opts):
        if reject_fallback and src == FALLBACK:
            return "EMPTY_SCENE"
        m = MARK.search(src)
        view = 1 if opts is None else FRONT.index(opts) + 1
        return "BOUNDING_BOX_OUT_OF_RANGE" if m and int(m.group(1)) == view else None
    return reject


def paths(s):
    gen0, gen1 = gen_outcomes(0), gen_outcomes(1)
    # v1 and grid check a module on the default preview only (= view 1), so REJ2-4 modules pass there
    passes = {"accept"} if s.refine_views == "front4" else {"accept", "rej2", "rej3", "rej4"}
    for a in gen0:
        for b in ([None] if a in passes else list(gen1)):
            table = {("gen", 0): gen0[a]}
            if b is not None:
                table[("gen", 1)] = gen1[b]
            if passes & {a, b} and s.refine:
                for rname, rtable in refine_outcomes().items():
                    yield (a, b, rname, False), {**table, **rtable}, False
            else:
                for reject_fallback in (False, True):
                    yield (a, b, None, reject_fallback), table, reject_fallback


class TestRenderQuota(unittest.TestCase):
    def test_render_artifacts_per_prompt_stay_within_9(self):
        seen = {}
        for name, (s, bound) in SETTINGS.items():
            for surface in (SURFACES if name == "v2r" else (DEFAULT_SURFACE,)):
                worst = 0
                for key, table, reject_fallback in paths(s):
                    w, r = FakeWitness(script_from(table), surface), FakeRenderer(rule_for(reject_fallback))
                    with contextlib.redirect_stderr(io.StringIO()):
                        out, p = run_detailed(IMAGE, SEED, w, r, s)
                    distinct = {(src, json.dumps(o, sort_keys=True)) for src, o in r.calls}
                    self.assertLessEqual(len(distinct), len(r.calls))
                    self.assertLessEqual(len(r.calls), bound, (name, surface, key))
                    if out is None:
                        self.assertEqual(p.final, "none", key)
                    else:
                        # the final save adds no render: the final is a source this prompt rendered and the renderer
                        # accepted at least once
                        accepted = [o for src, o in r.calls if src == out and not rule_for(reject_fallback)(src, o)]
                        self.assertTrue(accepted, (name, surface, key))
                    worst = max(worst, len(r.calls))
                seen[(name, surface)] = worst
        # the bound is reached (the enumeration covers the worst case)
        self.assertEqual(seen, {(n, sf): b for n, (_, b) in SETTINGS.items()
                                for sf in (SURFACES if n == "v2r" else (DEFAULT_SURFACE,))})

    def test_the_worst_cases(self):
        """The two 9-render paths of the committed configuration, spelled out."""
        s = SETTINGS["v2r"][0]
        g0, g1 = gen_outcomes(0), gen_outcomes(1)
        for table, reject_fallback, final in (
                ({("gen", 0): g0["rej4"], ("gen", 1): g1["accept"], ("refine", 0): ok(fenced(module("884422")))},
                 False, "refine"),
                ({("gen", 0): g0["rej4"], ("gen", 1): g1["rej4"]}, False, "fallback"),
                ({("gen", 0): g0["rej4"], ("gen", 1): g1["rej4"]}, True, "none")):
            w, r = FakeWitness(script_from(table)), FakeRenderer(rule_for(reject_fallback))
            with contextlib.redirect_stderr(io.StringIO()):
                _, p = run_detailed(IMAGE, SEED, w, r, s)
            self.assertEqual((p.final, len(r.calls), len({(a, json.dumps(b, sort_keys=True)) for a, b in r.calls})),
                             (final, 9, 9))


if __name__ == "__main__":
    unittest.main()

"""Request bytes, frozen texts, settings and the import surface."""

from __future__ import annotations

import ast
import base64
import dataclasses
import difflib
import hashlib
import json
import os
import re
import unittest
from pathlib import Path

from fakes import FIXTURE_REFINE_RETRY, FIXTURE_TICKET, FIXTURE_TIER_FRACTION, IMAGE, PNG, SEED, pod_body
from witness_client.request import serialize_body

from sn17gen import contract, settings
from sn17gen.contract import (ALLOWED_ROOT_IDENTIFIERS, FORBIDDEN_IDENTIFIERS, INSTRUCTIONS, REFINE_LABELS,
                              REFINE_LABELS_BY_VIEWS, REFINE_TEXT, REFINE_TEXT_BY_VIEWS, RETRY_COMPACT_TEXT, THREE_ALLOWED,
                              USER_TEXT_GEN)
from sn17gen.credits import POD_BODY_EXTRA_BYTES, IMAGE_TOKEN_BOUND, body_bytes, call_credits, worst_credits
from sn17gen.reqbuild import VARIANT_OUT_CAP, build_gen_params, build_refine_params, data_url, prompt_id
from sn17gen.settings import make_settings
from sn17gen.views import front4_options

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_SRC = Path(os.environ.get("SN17_VALIDATOR_SRC", "/repo/render-service-js/src/validator"))
V1, V2, V2G, V21 = make_settings("v1", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), make_settings("v2", "f1", "grid", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), make_settings("v2", "f2", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
PID = prompt_id(IMAGE)
URL = data_url(IMAGE)
CODE = "export default function generate(THREE) {\n  return new THREE.Group();\n}\n"
FIXED_PNGS = [PNG + bytes([i]) * 100 for i in range(4)]

# sha256 prefixes of frozen texts: sn17miner values (p1-p4, rp1, f1, f2, f1 labels) and the port's grid variant
FROZEN_TEXT = {"p1": "3305027034205500", "p2": "51eafa47f3aac0e4", "p3": "8a33c27ea95c48e5", "p4": "2b2b215d5e905b45",
               "rp1": "6347644ac3c802c6", "f1": "3f8108fad6e541caad7dfddbe06c9a5a2f13bed7997d4d669646d4434ce13edc",
               "f2": "daa6cb53f286e984f4d6cccde7e47b6d7d8026664155bb861d26b07724f6e7f1",
               "f1_labels": "371cf7420ea590f5f4d514d19200c5f58d55e0f622a1ab237fcbfd78a64a3572",
               "f1_grid": "baeafd5f3ec8c154"}
# sha256 of request bodies for the fixed inputs above (pinned rc4 serializer); a change here changes what we send
FROZEN_BODY = {
    "gen_a0": "83ddb56c0d56490875844299014732c26b2714f7ba125928aec90e8d3c806caf",
    "gen_a1_outcap_32k": "b782c0c8dd3e6566065953acbfdc9a7034444b18c792d6ff55effc999ced6d33",
    "refine_f1_front4": "bda9dc2c2616f21794542b20cff2f9d635a2ee88212529e6e3d2b0faff61841b",
    "refine_f2_front4_a1": "effef99d5d66f6d8391297b9f2c1f7ecf8942d3e4f2f702e8e76b85c55c99735",
    "refine_f1_grid": "c190a66aae9e3ae57726b4a61a784a9287e1123ed58d3e53520d4d3cccd0c754",
}
# sha256 of the same bodies as 404's Pod witness records them (0.3.0: GenerationWitness.create puts
# "service_tier":"default" first and stream last; json = FROZEN_BODY's + that key, 25 bytes longer)
FROZEN_POD_BODY = {
    "gen_a0": "914f077948bb0a66bdbb222f351ef62c190de02faee3bae2836a500e79434629",
    "gen_a1_outcap_32k": "fdb5ef30b728bd2dfd9ad1a29f7959d8fb384e6fca0b0fdb70d44510b1569357",
    "refine_f1_front4": "a0ed2724f17395cc30ff3305161ad134ab02785b4bcad93548e5f52270b3beb9",
    "refine_f2_front4_a1": "53423dfb02779e1a706bf24756510df2f8e59fb47a8b19458d6bc9f30c77c3de",
    "refine_f1_grid": "c7ba2f443e9cb6c29dadc9fe754afd31ca70f01f4d92c9d2400364e29aeee6b9",
}
# byte copies of sn17miner 0.4.0 (src 0ecfc85c) modules
COPIED = {"repair.py": "258e65b26066b95695c5ec4b70f2170686179d9e83c093dad5d446e201ac5109",
          "jsscan.py": "1644f45441a7da1c0db944a65cdd082965670d983f4d6d6981d4921f52e51b83"}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def bodies() -> dict[str, bytes]:
    return {
        "gen_a0": serialize_body(build_gen_params(V2, PID, SEED, URL, 0), "api"),
        "gen_a1_outcap_32k": serialize_body(build_gen_params(V2, PID, SEED, URL, 1, variant=VARIANT_OUT_CAP,
                                                             max_output_tokens=32_000), "api"),
        "refine_f1_front4": serialize_body(build_refine_params(V2, PID, SEED, URL, CODE, FIXED_PNGS), "api"),
        "refine_f2_front4_a1": serialize_body(build_refine_params(V21, PID, SEED, URL, CODE, FIXED_PNGS, attempt=1), "api"),
        "refine_f1_grid": serialize_body(build_refine_params(V2G, PID, SEED, URL, CODE, FIXED_PNGS[:1]), "api"),
    }


class TestTexts(unittest.TestCase):
    def test_frozen_texts(self):
        for v in ("p1", "p2", "p3", "p4"):
            self.assertTrue(sha(INSTRUCTIONS[v].encode()).startswith(FROZEN_TEXT[v]), v)
        self.assertTrue(sha(RETRY_COMPACT_TEXT["rp1"].encode()).startswith(FROZEN_TEXT["rp1"]))
        self.assertEqual(sha(REFINE_TEXT["f1"].encode()), FROZEN_TEXT["f1"])
        self.assertEqual(sha(REFINE_TEXT["f2"].encode()), FROZEN_TEXT["f2"])
        self.assertEqual(sha(repr(sorted(REFINE_LABELS["f1"].items())).encode()), FROZEN_TEXT["f1_labels"])
        self.assertTrue(sha(REFINE_TEXT_BY_VIEWS[("f1", "grid")].encode()).startswith(FROZEN_TEXT["f1_grid"]))
        self.assertEqual(REFINE_TEXT_BY_VIEWS[("f1", "front4")], REFINE_TEXT["f1"])
        self.assertEqual(REFINE_TEXT_BY_VIEWS[("f2", "front4")], REFINE_TEXT["f2"])
        self.assertEqual(REFINE_LABELS_BY_VIEWS[("f1", "front4")], REFINE_LABELS["f1"])

    def test_grid_text_is_f1_with_four_sentence_edits(self):
        a, b = REFINE_TEXT["f1"], REFINE_TEXT_BY_VIEWS[("f1", "grid")]
        ops = [op for op in difflib.SequenceMatcher(a=a.split(". "), b=b.split(". "), autojunk=False).get_opcodes()
               if op[0] != "equal"]
        self.assertLessEqual(len(ops), 4)
        self.assertNotIn("white background", b)
        self.assertIn("2 x 2 grid", b)
        labels = REFINE_LABELS_BY_VIEWS[("f1", "grid")]
        self.assertEqual((labels["reference"], labels["code"]), (REFINE_LABELS["f1"]["reference"], REFINE_LABELS["f1"]["code"]))

    def test_copied_modules_are_byte_identical_to_sn17miner(self):
        for name, want in COPIED.items():
            self.assertEqual(sha((ROOT / "sn17gen" / name).read_bytes()), want, name)

    @unittest.skipUnless(VALIDATOR_SRC.is_dir(), "render-service validator source not mounted")
    def test_allowlists_match_production_validator(self):
        src = (VALIDATOR_SRC / "threeAllowlist.js").read_text()
        block = src.split("export const THREE_ALLOWED = new Set([", 1)[1].split("]);", 1)[0]
        self.assertEqual(tuple(n for n in re.findall(r"'([A-Za-z0-9_]+)'", block) if n != "LuminanceFormat"), THREE_ALLOWED)
        ids = (VALIDATOR_SRC / "identifiers.js").read_text()
        allowed = re.findall(r"'([A-Za-z0-9_]+)'", ids.split("ALLOWED_ROOT_IDENTIFIERS = new Set([", 1)[1].split("]);", 1)[0])
        forbidden = re.findall(r"'([A-Za-z0-9_]+)'", ids.split("FORBIDDEN_IDENTIFIERS = new Set([", 1)[1].split("]);", 1)[0])
        self.assertEqual(tuple(allowed), ALLOWED_ROOT_IDENTIFIERS)
        self.assertEqual(tuple(forbidden), FORBIDDEN_IDENTIFIERS)


class TestRequests(unittest.TestCase):
    def test_frozen_bodies(self):
        got = {k: sha(v) for k, v in bodies().items()}
        self.assertEqual(got, FROZEN_BODY)

    def test_frozen_pod_bodies(self):
        """What the Pod witness records (and replay compares) for the fixtures: 0.2.x's request json + the injected
        default tier, nothing else (0.3.0 changes no request content)."""
        plain = bodies()
        got = {k: pod_body(json.loads(b)) for k, b in plain.items()}
        self.assertEqual({k: sha(b) for k, b in got.items()}, FROZEN_POD_BODY)
        for k, b in got.items():
            doc = json.loads(b)
            self.assertEqual(doc, {**json.loads(plain[k]), "service_tier": "default"}, k)
            self.assertEqual((list(doc)[1], list(doc)[-1], len(b)), ("service_tier", "stream", len(plain[k]) + 25), k)

    def test_gen_shape(self):
        p = build_gen_params(V2, PID, SEED, URL, 0)
        doc = json.loads(serialize_body(p, "api"))
        self.assertEqual(list(doc), ["tools", "stream", "model", "instructions", "input", "reasoning", "max_output_tokens", "store"])
        self.assertEqual((doc["tools"], doc["stream"], doc["model"], doc["store"]), ([], True, "gpt-6-astra", False))
        self.assertEqual(doc["reasoning"], {"effort": "high"})
        self.assertEqual(doc["max_output_tokens"], 24_000)
        self.assertEqual(doc["instructions"], INSTRUCTIONS["p4"])
        parts = doc["input"][0]["content"]
        self.assertEqual(parts[0]["text"], f"[pid={PID} seed={SEED} ticket=t0 step=gen attempt=0] {USER_TEXT_GEN}")
        self.assertEqual(parts[1]["detail"], "high")
        self.assertTrue(parts[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(parts[1]["image_url"].split(",", 1)[1], validate=True), IMAGE)   # exact bytes
        self.assertEqual(PID, sha(IMAGE)[:16])
        # v1 and v2 send the same gen request (paired comparisons stay possible)
        self.assertEqual(serialize_body(build_gen_params(V1, PID, SEED, URL, 0), "api"), serialize_body(p, "api"))
        # the ticket separates own hotkeys
        t1 = make_settings(ticket="t1")
        self.assertNotEqual(serialize_body(build_gen_params(t1, PID, SEED, URL, 0), "api"), serialize_body(p, "api"))

    def test_outcap_variant(self):
        p = build_gen_params(V2, PID, SEED, URL, 1, variant=VARIANT_OUT_CAP, max_output_tokens=32_000)
        text = p["input"][0]["content"][0]["text"]
        self.assertEqual(text, f"[pid={PID} seed={SEED} ticket=t0 step=gen attempt=1] {RETRY_COMPACT_TEXT['rp1']} {USER_TEXT_GEN}")
        self.assertEqual(p["max_output_tokens"], 32_000)

    def test_refine_bodies(self):
        b = bodies()
        a0 = serialize_body(build_refine_params(V21, PID, SEED, URL, CODE, FIXED_PNGS), "api")
        self.assertEqual(b["refine_f2_front4_a1"], a0.replace(b"step=refine attempt=0]", b"step=refine attempt=1]"))
        with self.assertRaises(ValueError):
            build_refine_params(V2, PID, SEED, URL, CODE, FIXED_PNGS, attempt=1)       # f1: no retry
        with self.assertRaises(ValueError):
            build_refine_params(V2, PID, SEED, URL, CODE, FIXED_PNGS[:3])             # front4 needs 4 PNGs
        with self.assertRaises(ValueError):
            build_refine_params(V2G, PID, SEED, URL, CODE, FIXED_PNGS)                # grid needs 1 PNG
        with self.assertRaises(ValueError):
            build_refine_params(V1, PID, SEED, URL, CODE, FIXED_PNGS)
        other = list(FIXED_PNGS)
        other[2] = other[2] + b"\0"
        self.assertNotEqual(serialize_body(build_refine_params(V2, PID, SEED, URL, CODE, other), "api"), b["refine_f1_front4"])

    def test_worst_case_counts_images_as_images(self):
        p = build_gen_params(V2, PID, SEED, URL, 0)
        body = serialize_body(p, "api")
        self.assertEqual(body_bytes(p), len(body))
        payload = len(URL.split(",", 1)[1])
        pod = len(pod_body(p))                      # the Pod witness's body: + ',"service_tier":"default"' (C5)
        self.assertEqual(pod, len(body) + POD_BODY_EXTRA_BYTES)
        self.assertEqual(worst_credits(p), call_credits("gpt-6-astra", pod - payload + IMAGE_TOKEN_BOUND["high"], 24_000))
        big = [PNG + bytes(300_000) for _ in range(4)]
        self.assertEqual(worst_credits(build_refine_params(V2, PID, SEED, URL, CODE, big)),
                         worst_credits(build_refine_params(V2, PID, SEED, URL, CODE, FIXED_PNGS)))
        self.assertLessEqual(2 * worst_credits(p), 3_000_000)        # two gen attempts always fit the cap
        self.assertGreater(3 * worst_credits(p), 3_000_000)

    def test_front4_options_normal_form(self):
        self.assertEqual([o.query() for o in front4_options()],
                         [{"thetas": f"{float(t)}", "phis": f"{float(p)}", "img_size": "512", "cam_radius": "2.0",
                           "cam_fov_deg": "49.1", "gap": "5", "bg_color": "ffffff", "lighting": "neutral"}
                          for t, p in ((30, 0), (330, 0), (0, 15), (0, -30))])


class TestSettings(unittest.TestCase):
    def test_committed_mode(self):
        self.assertIn(settings.MODE, ("v1", "v2"))
        self.assertEqual(settings.ACTIVE, make_settings())
        self.assertEqual(settings.ACTIVE.ticket, settings.TICKET)          # the committed ticket reaches every tag
        self.assertEqual(settings.ACTIVE.credit_cap, 3_000_000)
        self.assertEqual((settings.ACTIVE.model, settings.ACTIVE.effort, settings.ACTIVE.prompt_version,
                          settings.ACTIVE.repair_version, settings.ACTIVE.retry_policy), ("gpt-6-astra", "high", "p4", "r3", "rp1"))

    def test_committed_v2_2_refine_cap(self):
        """R7_port: v2.2's refine cap is committed (gen keeps 3M); a switch to v1 at launch stays a one-line edit."""
        self.assertEqual(settings.REFINE_CREDIT_CAP, 4_000_000)
        self.assertEqual((settings.ACTIVE.credit_cap, settings.ACTIVE.refine_credit_cap),
                         (3_000_000, 4_000_000 if settings.MODE == "v2" else None))
        self.assertEqual(make_settings("v2", "f2", "front4").refine_credit_cap, 4_000_000)
        self.assertIsNone(make_settings("v1").refine_credit_cap)
        self.assertIsNone(make_settings("v1", refine_credit_cap=3_000_000).refine_credit_cap)
        self.assertEqual(make_settings("v2", refine_credit_cap=3_000_000).refine_credit_cap, 3_000_000)   # = 0.1.0

    def test_refine_cap_never_changes_a_request(self):
        legacy = make_settings("v2", "f1", "front4", refine_credit_cap=3_000_000, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
        legacy21 = make_settings("v2", "f2", "front4", refine_credit_cap=3_000_000, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY)
        self.assertEqual(serialize_body(build_gen_params(legacy, PID, SEED, URL, 0), "api"), bodies()["gen_a0"])
        self.assertEqual(serialize_body(build_refine_params(legacy, PID, SEED, URL, CODE, FIXED_PNGS), "api"),
                         bodies()["refine_f1_front4"])
        self.assertEqual(serialize_body(build_refine_params(legacy21, PID, SEED, URL, CODE, FIXED_PNGS, attempt=1), "api"),
                         bodies()["refine_f2_front4_a1"])

    def test_invalid_settings_refused(self):
        for kw in ({"mode": "v3"}, {"mode": "v2", "refine_views": "back4"}, {"mode": "v2", "refine_version": "f9"},
                   {"ticket": "bad ticket"}, {"mode": "v2", "refine_version": "f2", "refine_views": "grid"},
                   {"mode": "v2", "refine_version": "f3"}, {"mode": "v2", "refine_version": "f4"},
                   {"mode": "v2", "refine_credit_cap": 2_999_999}, {"mode": "v2", "refine_credit_cap": 5_000_001},
                   {"mode": "v2", "refine_credit_cap": 4_000_000.0}, {"mode": "v2", "refine_credit_cap": True}):
            with self.assertRaises(ValueError, msg=kw):
                make_settings(**kw)
        with self.assertRaises(ValueError):
            dataclasses.replace(make_settings("v1"), refine_credit_cap=4_000_000)     # v1 has no refine cap
        with self.assertRaises(ValueError):
            dataclasses.replace(make_settings("v2"), refine_credit_cap=None)
        self.assertEqual(make_settings("v2", refine_credit_cap=5_000_000).refine_credit_cap, 5_000_000)  # README nominal
        self.assertEqual(make_settings("v1", service_tier_fraction=0.0).label, "v1")
        self.assertIsNone(make_settings("v1").refine_views)


# openai (0.3.1): only for its exception classes (witness_call); a fixed runtime package (404 miner-reference
# runtime/requirements.txt pins openai==3.29.0) and a dependency of witness-client itself
ALLOWED_IMPORTS = {"__future__", "base64", "dataclasses", "fractions", "hashlib", "json", "re", "sys", "typing",
                   "renderer_client", "witness_client", "sn17gen", "openai"}


class TestSurface(unittest.TestCase):
    def test_only_allowed_imports(self):
        """No clock, randomness, OS/host state, network or subprocess modules anywhere in the submission code."""
        files = [ROOT / "main.py"] + sorted((ROOT / "sn17gen").glob("*.py"))
        for f in files:
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [] if node.level else [(node.module or "").split(".")[0]]
                else:
                    continue
                for n in names:
                    self.assertIn(n, ALLOWED_IMPORTS, f"{f.name} imports {n}")

    def test_no_builtin_hash_or_wall_clock_calls(self):
        for f in [ROOT / "main.py"] + sorted((ROOT / "sn17gen").glob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotIn(node.func.id, ("hash", "id", "input", "open", "eval", "exec", "__import__"),
                                     f"{f.name}: {node.func.id}()")

    def test_entry_point(self):
        import inspect

        import main
        self.assertEqual(list(inspect.signature(main.generate).parameters), ["image", "seed", "witness", "renderer"])


if __name__ == "__main__":
    unittest.main()

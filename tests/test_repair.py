"""Repair rules r1-r3 (ported from sn17miner tests/test_repair.py + the r3 cases of test_v2.py, pytest -> unittest).
repair.py and jsscan.py are byte copies of sn17miner 0.4.0, so the frozen v0 bytes still hold."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sn17gen.jsscan import ScanError, scan
from sn17gen.repair import extract_js, fallback_module, fix_unary_pow, repair, structural_check, token_rewrites

ROOT = Path(__file__).resolve().parents[1]
TDZ = """export default function generate(THREE) {
  const g = new THREE.Group();
  g.add(new THREE.Mesh(new THREE.BoxGeometry(SIZE, SIZE, SIZE), new THREE.MeshStandardMaterial({ color: 0x336699 })));
  return g;
}
const SIZE = 0.5;
class Helper {}
"""
# computed with the v0 build (sn17miner 0.1.0 sources, 2026-10-20) — r1 must keep producing these bytes
V0_TDZ_RAND_SHA256 = "750d5dfccdfa3c7baa3e14749f06272dd1f5407054d85563fc8aba0307dad965"
V0_FALLBACK_ABC_SHA256 = "447cf8d57e025fe5b33558f2e99ceb4e53b4867d235a8a1a77cbd66ff54f64aa"

POW_CASES = [
    ("const k = Math.exp(-((x - a) / s) ** 2);", "const k = Math.exp(-(((x - a) / s) ** 2));"),
    ("const k = -x ** 2;", "const k = -(x ** 2);"),
    ("return -x ** 2;", "return -(x ** 2);"),
    ("f(1, -a.b[c](d) ** e ** f);", "f(1, -(a.b[c](d) ** e ** f));"),
    ("const v = [-2 ** 0.5, +y ** 3];", "const v = [-(2 ** 0.5), +(y ** 3)];"),
    ("const n = c ? -t ** 2 : !u ** 2;", "const n = c ? -(t ** 2) : !(u ** 2);"),
    ("const q = -(-x ** 2) ** 3;", "const q = -((-(x ** 2)) ** 3);"),
    ("const w = -x ** -y;", "const w = -(x ** -y);"),
    ("const z = -i++ ** 2;", "const z = -(i++ ** 2);"),
]
POW_UNCHANGED = ["const k = a - b ** 2;", "const k = (-x) ** 2;", "const k = 2 ** -x;", "const k = x ** 2 - y ** 2;",
                 "const k = f(x) - g(y) ** 2;", "const k = arr[i] - m.n ** 2;", "x **= 2;"]


def fenced(js, lang="javascript"):
    return f"Sure.\n```{lang}\n{js}```\nDone."


class TestRepair(unittest.TestCase):
    def test_extract_prefers_last_block_with_export_default(self):
        text = ("```js\nconst a = 1;\n```\n```javascript\nexport default function generate(THREE) { return new THREE.Group(); }\n```\n"
                "```js\nexport default function generate(THREE) { return 2; }\n```")
        js, how = extract_js(text)
        self.assertIn("return 2", js)
        self.assertEqual(how, "fence:export default")

    def test_extract_unterminated_and_raw_and_empty(self):
        js, how = extract_js("```javascript\nexport default function generate(THREE) { return new THREE.Group(); }\n")
        self.assertEqual(how, "fence:unterminated")
        self.assertTrue(js.startswith("export default"))
        self.assertEqual(extract_js("export default function generate(THREE) { return new THREE.Group(); }")[1], "raw")
        self.assertEqual(extract_js(""), (None, "empty"))
        self.assertEqual(extract_js("no code here"), (None, "no_code"))

    def test_tdz_fit_mode_puts_wrapper_last(self):
        r = repair(fenced(TDZ))
        self.assertTrue(r.ok, r.reasons)
        toks = scan(r.js)
        self.assertEqual(len([k for k, t in enumerate(toks) if t.text == "export" and t.depth == 0]), 1)
        self.assertTrue(r.js.rstrip().endswith("return __sn17_fit(THREE, __sn17_model(THREE));\n}"))
        self.assertLess(r.js.index("const SIZE = 0.5;"), r.js.index("export default function generate"))
        self.assertIn("function __sn17_model(THREE)", r.js)
        self.assertIn("fit_wrapper", r.rules)
        self.assertIn("export_to_model_fn", r.rules)

    def test_tdz_hoist_mode_moves_tail_above_export(self):
        r = repair(fenced(TDZ), fit_wrapper=False)
        self.assertTrue(r.ok, r.reasons)
        self.assertLess(r.js.index("class Helper"), r.js.index("export default"))
        self.assertIn("hoist_tail_above_export", r.rules)
        self.assertNotIn("__sn17_fit", r.js)

    def test_token_rewrites(self):
        src = """import * as THREE from 'three';
import { X } from "three";
export const K = 2;
export function helper(THREE) { return THREE.LuminanceFormat; }
export default function generate(THREE) {
  console.log('a', (1 + 2));
  const t = Date.now() + performance.now();
  const r = Math.random();
  const same = geo.constructor === THREE.BoxGeometry;
  const diff = a.b.constructor !== c;
  const p = obj.__proto__;
  const q = obj.constructor.name;
  return new THREE.Group();
}
export { helper as h };
"""
        out, rules = token_rewrites(src)
        self.assertNotIn("import", out)
        for bad in ("export const", "export function", "export {", "LuminanceFormat", "console", "Math.random",
                    ".constructor", "__proto__"):
            self.assertNotIn(bad, out)
        for good in ("THREE.RedFormat", "void 0", "const t = 0 + 0;", "__sn17_rand()", "(geo instanceof THREE.BoxGeometry)",
                     "!(a.b instanceof c)", ".__sn17_blocked_proto", "obj.__sn17_blocked_constructor.name"):
            self.assertIn(good, out)
        for rule in ("drop_import", "drop_named_export_keyword", "drop_export_list", "luminance_to_red", "drop_console",
                     "clock_now_zero", "math_random", "constructor_eq_to_instanceof", "block_forbidden_property"):
            self.assertIn(rule, rules)

    def test_math_random_helper_appended_and_check_passes(self):
        r = repair(fenced("export default function generate(THREE) { const g = new THREE.Group(); g.position.x = Math.random(); return g; }\n"))
        self.assertTrue(r.ok, r.reasons)
        self.assertIn("function __sn17_rand()", r.js)
        self.assertNotIn("Math.random", r.js)

    def test_arrow_ident_and_bare_forms(self):
        a = repair(fenced("export default (THREE) => {\n  return new THREE.Group();\n};\n"))
        self.assertTrue(a.ok)
        self.assertIn("const __sn17_model = (THREE) => {", a.js)
        b = repair(fenced("function build(THREE) { return new THREE.Group(); }\nexport default build;\n"))
        self.assertTrue(b.ok)
        self.assertIn("function __sn17_model(THREE)", b.js)
        self.assertNotIn("export default build", b.js)
        c = repair(fenced("function generate(THREE) { return new THREE.Group(); }\n"))
        self.assertTrue(c.ok)
        d = repair(fenced("export default function make(THREE) { return generate(THREE); }\nfunction generate(THREE) { return new THREE.Group(); }\n"))
        self.assertTrue(d.ok, d.reasons)
        self.assertIn("__sn17_user_generate", d.js)
        self.assertEqual(d.js.count("function generate("), 1)

    def test_structural_rejections(self):
        self.assertFalse(repair(fenced("export default async function generate(THREE) { return new THREE.Group(); }\n")).ok)
        w = repair(fenced("export default function generate(THREE) { window.x = 1; return new THREE.Group(); }\n"))
        self.assertFalse(w.ok)
        self.assertIn("forbidden_identifier:window", w.reasons)
        u = repair(fenced("export default function generate(THREE) { return new THREE.Group(); \n"))
        self.assertFalse(u.ok)
        self.assertTrue(any(x.startswith("scan_error") for x in u.reasons))
        n = repair("Sorry, I can't.")
        self.assertFalse(n.ok)
        self.assertIsNone(n.js)
        self.assertEqual(n.reasons, ["no_code"])
        self.assertEqual(structural_check(fallback_module("x")), [])
        self.assertEqual(structural_check(fallback_module("x", "r3"), "r3"), [])

    def test_object_keys_and_properties_are_not_identifiers(self):
        r = repair(fenced("export default function generate(THREE) { const o = { window: 1, self: 2 }; o.document = 3; return new THREE.Group(); }\n"))
        self.assertTrue(r.ok, r.reasons)

    def test_scanner_handles_templates_regex_and_comments(self):
        src = "const a = `x ${ {b: 1}.b } y ${`nested ${1}`}`; // } (\nconst r = /[/]}/g; /* { */ const c = 1 / 2 / 3;\n"
        toks = scan(src)
        self.assertEqual(toks[-1].text, ";")
        self.assertTrue(all(t.depth == 0 for t in toks if t.text == "const"))
        with self.assertRaises(ScanError):
            scan("const a = `unterminated")

    def test_fallback_is_stable_and_per_prompt(self):
        self.assertEqual(fallback_module("abc"), fallback_module("abc"))
        self.assertNotEqual(fallback_module("abc"), fallback_module("abd"))
        self.assertNotIn("Math.random", fallback_module("abc"))

    def test_repair_is_hashseed_independent(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "in.txt"
            src.write_text(fenced(TDZ.replace("const SIZE", "const r = Math.random();\nconst SIZE")))
            code = ("import sys, hashlib; sys.path.insert(0, %r); from sn17gen.repair import repair; "
                    "r = repair(open(%r).read(), version='r3'); print(hashlib.sha256(r.js.encode()).hexdigest(), r.ok, r.rules)") % (
                str(ROOT), str(src))
            outs = set()
            for seed in ("0", "1", "4242", "random"):
                env = {"PYTHONHASHSEED": seed, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONDONTWRITEBYTECODE": "1"}
                outs.add(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout)
            self.assertEqual(len(outs), 1, outs)

    def test_r2_fixes_unary_before_exponent(self):
        for src, want in POW_CASES:
            out, rules = fix_unary_pow(src + "\n")
            self.assertEqual(out, want + "\n", src)
            self.assertEqual(rules, ["unary_pow"])
            self.assertEqual(structural_check("export default function generate(THREE) { " + out + " return new THREE.Group(); }\n", "r2"), [])
        for src in POW_UNCHANGED:
            out, rules = fix_unary_pow(src + "\n")
            self.assertEqual((out, rules), (src + "\n", []), src)

    def test_r2_structural_check_flags_what_r1_never_checked(self):
        bad = "export default function generate(THREE) { const k = -x ** 2; return new THREE.Group(); }\n"
        self.assertIn("unary_before_exponent", structural_check(bad, "r2"))
        self.assertEqual(structural_check(bad, "r1"), [])
        self.assertIn("unary_before_exponent", structural_check(bad.replace("-x", "typeof x"), "r2"))
        with self.assertRaises(ValueError):
            structural_check(bad, "r9")

    def test_r1_output_is_the_v0_output(self):
        src = fenced(TDZ.replace("const SIZE", "const r = Math.random();\nconst SIZE"))
        self.assertEqual(hashlib.sha256(repair(src).js.encode()).hexdigest(), V0_TDZ_RAND_SHA256)
        self.assertEqual(hashlib.sha256(fallback_module("abc").encode()).hexdigest(), V0_FALLBACK_ABC_SHA256)

    def test_r3_flags_the_three_patterns_r2_missed(self):
        w = "export default function generate(THREE) { const g = new THREE.Group(); %s return g; }\n"
        bad = ["const c = true; let a = 2; if (c) -a ** 2;", "const c = true; let a = 2; if (c) {} -a ** 2;",
               "function Foo() { this.x = 2; } const k = -new Foo().x ** 2;", "let a = 0; while (a < 1) -a ** 2;"]
        ok = ["const b = 1, c = 2; const k = b - c ** 2;", "const k = ({}) - 2 ** 2;", "const o = { a: 1 } - 2 ** 2;",
              "const k = Math.max(1, 2) - 2 ** 2;", "function Foo() { this.x = 2; } const k = 3 - new Foo().x ** 2;",
              "const a = 2; const k = (-a) ** 2;"]
        for b in bad:
            self.assertEqual(structural_check(w % b, "r2"), [], b)
            self.assertEqual(structural_check(w % b, "r3"), ["unary_before_exponent"], b)
        for o in ok:
            self.assertEqual(structural_check(w % o, "r3"), [], o)
        txt = "```js\n" + (w % "const x = 0.5; const k = Math.exp(-(x / 0.4) ** 2);") + "```"
        r2, r3 = repair(txt, version="r2"), repair(txt, version="r3")
        self.assertTrue(r3.ok)
        self.assertEqual(r3.js, r2.js.replace("sn17miner r2: fit", "sn17miner r3: fit"))
        self.assertFalse(repair("```js\n" + (w % bad[0]) + "```", version="r3").ok)


if __name__ == "__main__":
    unittest.main()

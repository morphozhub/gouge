"""Gouge additions: p5 prompt, second draft + judge (stubbed witness/renderer)."""
from __future__ import annotations
import io, json, sys
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sn17gen import contract, pipeline, settings  # noqa: E402

JS = "export default function generate(THREE) { const g = new THREE.Group(); g.add(new THREE.Mesh(new THREE.BoxGeometry(1, 1, 1), new THREE.MeshStandardMaterial({ color: 0x808080 }))); return g; }"
PNG = (lambda: (lambda b: (Image.new("RGB", (64, 64), (255, 255, 255)).save(b, format="PNG"), b.getvalue())[1])(io.BytesIO()))()


class Witness:
    def __init__(self, winner="B"):
        self.calls = []; self.winner = winner
        self.responses = SimpleNamespace(create=self.create)

    def create(self, **params):
        self.calls.append(params)
        if params["instructions"].startswith("You are the blind judge"):
            return SimpleNamespace(output_text=json.dumps({"winner": self.winner, "reason": "r", "discrepancies": []}),
                                   usage={"input_tokens": 10, "output_tokens": 5})
        n = len(self.calls)
        return SimpleNamespace(output_text=f"```javascript\n{JS} // draft {n}\n```", usage={"input_tokens": 100, "output_tokens": 50})


class Renderer:
    def render(self, js, options=None):
        return PNG


def test_p5_prompt_has_real_limits_and_detail_demand():
    p5 = contract.INSTRUCTIONS["p5"]
    assert "at most 250,000 vertices" in p5 and "6,000-8,000 tokens" in p5 and "at most 60,000" not in p5


def test_p6_prompt_has_material_table_and_active_flags():
    p6 = contract.INSTRUCTIONS["p6"]
    assert "#c8c8c8 to #dcdcdc" in p6 and "6,000-8,000 tokens" in p6 and "metalness at most 0.6 (more renders black" not in p6
    assert settings.ACTIVE.prompt_version == "p6" and not settings.ACTIVE.two_drafts and settings.ACTIVE.colour_calib
    assert settings.ACTIVE.label.endswith("-cc-r2g") and settings.ACTIVE.second_refine and settings.ACTIVE.refine2_guard


def _png(rgb, size=(96, 96), bg=None, box=(24, 24, 72, 72)):
    im = Image.new("RGB", size, bg or (255, 255, 255))
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            im.putpixel((x, y), rgb)
    b = io.BytesIO(); im.save(b, format="PNG"); return b.getvalue()


def test_colour_check_flags_dark_render_and_is_quiet_when_matching():
    from sn17gen import calib
    photo = _png((190, 190, 190), bg=(230, 230, 232))          # silver object on a studio background
    dark = [_png((20, 20, 20))] * 4                              # renders: nearly black object on white
    note, info = calib.colour_check(photo, dark)
    assert note and "DARKER" in note and info["flag"] == "dark" and "#" in info["photo_dom"]
    same = [_png((190, 190, 190))] * 4
    note2, info2 = calib.colour_check(photo, same)
    assert note2 is None and info2["flag"] is None
    assert calib.colour_check(photo, dark)[0] == note                 # deterministic


def test_second_refine_runs_after_the_accepted_refine_and_failures_keep_the_first():
    w = Witness()
    s = settings.make_settings(two_drafts=False, colour_calib=False, second_refine=True, refine2_guard=False)
    source, p = pipeline.run_detailed(b"\x89PNG" + b"z" * 100, 7, w, Renderer(), s)
    steps = [c["input"][0]["content"][0]["text"] for c in w.calls]
    assert sum("step=gen" in t for t in steps) == 1 and sum("step=refine " in t for t in steps) == 1 and sum("step=refine2" in t for t in steps) == 1
    r2 = next(c for c in w.calls if "step=refine2" in c["input"][0]["content"][0]["text"])
    assert "DETAIL PASS" in r2["input"][0]["content"][0]["text"] and r2["instructions"] == contract.INSTRUCTIONS["p6"]
    assert sum(1 for part in r2["input"][0]["content"] if part["type"] == "input_image") == 5   # photo + four judge views
    code_part = [part["text"] for part in r2["input"][0]["content"] if part["type"] == "input_text"][-1]
    assert "__sn17_" not in code_part and "draft 2" in code_part     # the model's own source, never the fit-wrapped module
    assert "under 150,000 vertices" in contract.INSTRUCTIONS["p6"]
    assert source is not None and "draft 3" in source and any(e.get("step") == "refine2" and e.get("outcome") == "accepted" for e in p.events)
    assert s.label.endswith("-r2") and not s.refine2_guard

    # guard on: the Sol judge (winner B by default in Witness) takes the detail pass; winner A keeps the first refine
    g = settings.make_settings(two_drafts=False, colour_calib=False, second_refine=True, refine2_guard=True)
    assert g.label.endswith("-r2g")
    wg = Witness(winner="B")
    sg, pg = pipeline.run_detailed(b"\x89PNG" + b"z" * 100, 7, wg, Renderer(), g)
    jcall = next(c for c in wg.calls if c["instructions"].startswith("You are the blind judge"))
    assert "revision of candidate A" in jcall["input"][0]["content"][0]["text"]
    assert sum(1 for part in jcall["input"][0]["content"] if part["type"] == "input_image") == 9    # photo + 4 + 4
    assert "draft 3" in sg and any(e.get("step") == "guard" and e.get("outcome") == "took_refine2" for e in pg.events)
    wa = Witness(winner="A")
    sa, pa = pipeline.run_detailed(b"\x89PNG" + b"z" * 100, 7, wa, Renderer(), g)
    assert "draft 2" in sa and any(e.get("step") == "guard" and e.get("outcome") == "kept_refine1" for e in pa.events)

    class BadSecond(Witness):
        def create(self, **params):
            if "step=refine2" in params["input"][0]["content"][0]["text"]:
                return SimpleNamespace(output_text="no code here", usage={"input_tokens": 1, "output_tokens": 1})
            return super().create(**params)
    w2 = BadSecond()
    source2, p2 = pipeline.run_detailed(b"\x89PNG" + b"z" * 100, 7, w2, Renderer(), s)
    assert source2 is not None and "draft 2" in source2 and any(e.get("step") == "refine2" and e.get("outcome") == "rejected_output" for e in p2.events)


def test_refine_request_carries_the_note_only_when_it_fires():
    s = settings.make_settings(two_drafts=False, colour_calib=True)
    w = Witness()
    photo = _png((190, 190, 190), bg=(230, 230, 232))

    class DarkRenderer:
        def render(self, js, options=None):
            return _png((20, 20, 20))
    source, p = pipeline.run_detailed(photo, 7, w, DarkRenderer(), s)
    assert source is not None
    refine_call = next(c for c in w.calls if "step=refine" in c["input"][0]["content"][0]["text"])
    texts = [part["text"] for part in refine_call["input"][0]["content"] if part["type"] == "input_text"]
    assert any(t.startswith("COLOUR CHECK") for t in texts)
    assert any(e.get("step") == "colour_check" and e.get("flag") == "dark" for e in p.events)
    w2 = Witness()
    pipeline.run_detailed(photo, 7, w2, Renderer(), settings.make_settings(two_drafts=False, colour_calib=True))
    refine2 = next(c for c in w2.calls if "step=refine" in c["input"][0]["content"][0]["text"])
    assert not any(part["type"] == "input_text" and part["text"].startswith("COLOUR CHECK") for part in refine2["input"][0]["content"])


def test_two_drafts_judge_picks_the_refine_input():
    w = Witness(winner="B")
    source, p = pipeline.run_detailed(b"\x89PNG" + b"x" * 100, 7, w, Renderer(), settings.make_settings(two_drafts=True, colour_calib=False))
    assert source is not None
    steps = [e.get("step") for e in p.events]
    assert steps.count("gen") >= 2 and "judge" in steps and "refine" in steps
    tags = [c["input"][0]["content"][0]["text"] for c in w.calls]
    assert any("attempt=0" in t for t in tags) and any("attempt=2" in t for t in tags)
    refine_code = next(c for c in w.calls if "step=refine" in c["input"][0]["content"][0]["text"])
    assert "draft 2" in json.dumps(refine_code)  # the judge chose B = the second draft


def test_judge_failure_keeps_the_first_draft():
    class Bad(Witness):
        def create(self, **params):
            if params["instructions"].startswith("You are the blind judge"):
                return SimpleNamespace(output_text="not json", usage={"input_tokens": 1, "output_tokens": 1})
            return super().create(**params)
    w = Bad()
    source, p = pipeline.run_detailed(b"\x89PNG" + b"y" * 100, 7, w, Renderer(), settings.make_settings(two_drafts=True, colour_calib=False))
    assert source is not None and any(e.get("outcome") == "unparseable" for e in p.events)
    refine_code = next(c for c in w.calls if "step=refine" in c["input"][0]["content"][0]["text"])
    assert "draft 1" in json.dumps(refine_code)


def test_flags_off_reproduce_round0_sequence():
    s = settings.make_settings(prompt_version="p4", two_drafts=False, colour_calib=False, second_refine=False)
    w = Witness()
    source, p = pipeline.run_detailed(b"\x89PNG" + b"z" * 100, 7, w, Renderer(), s)
    steps = []
    for e in p.events:
        st = e.get("step")
        if st in ("gen", "refine", "judge") and (not steps or steps[-1] != st):
            steps.append(st)
    assert source is not None and steps == ["gen", "refine"]

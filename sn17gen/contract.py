"""The fixed instruction texts (prompt versions p1-p4, refine f1/f2 and their view-mode variants) and the output-contract
constants. Copied from sn17miner 0.4.0 (src 0ecfc85c) unchanged; the submission-port additions are at the end.

The text is a literal: it never depends on data, the environment or iteration order (R5-R7).
Facts encoded here: render contract F1-F7 (01_RECONCILED_FACTS.md), production traps F5, judge B11/F3,
allowlist = render-service-js/src/validator/threeAllowlist.js @ ced2b09 minus LuminanceFormat (r183 trap).
"""

from __future__ import annotations

# Exactly the allowlist order of threeAllowlist.js, minus LuminanceFormat (allow-listed but not exported by r183).
THREE_ALLOWED: tuple[str, ...] = (
    "BufferGeometry", "BufferAttribute", "InterleavedBuffer", "InterleavedBufferAttribute",
    "Float32BufferAttribute", "Uint8BufferAttribute", "Uint16BufferAttribute", "Uint32BufferAttribute",
    "Int8BufferAttribute", "Int16BufferAttribute", "Int32BufferAttribute",
    "BoxGeometry", "SphereGeometry", "CylinderGeometry", "CapsuleGeometry", "ConeGeometry", "TorusGeometry",
    "TorusKnotGeometry", "PlaneGeometry", "CircleGeometry", "RingGeometry", "TetrahedronGeometry",
    "OctahedronGeometry", "DodecahedronGeometry", "IcosahedronGeometry", "PolyhedronGeometry",
    "ExtrudeGeometry", "LatheGeometry", "ShapeGeometry", "TubeGeometry", "EdgesGeometry", "WireframeGeometry",
    "MeshStandardMaterial", "MeshPhysicalMaterial", "MeshBasicMaterial", "PointsMaterial", "LineBasicMaterial",
    "LineDashedMaterial", "DataTexture",
    "Vector2", "Vector3", "Vector4", "Matrix3", "Matrix4", "Quaternion", "Euler", "Box2", "Box3", "Sphere",
    "Plane", "Ray", "Line3", "Triangle", "Spherical", "Cylindrical", "Color", "MathUtils",
    "Curve", "CurvePath", "Shape", "Path", "CatmullRomCurve3", "CubicBezierCurve3", "LineCurve3",
    "QuadraticBezierCurve3", "EllipseCurve", "ArcCurve", "LineCurve", "SplineCurve", "QuadraticBezierCurve",
    "CubicBezierCurve",
    "Object3D", "Group", "Mesh", "InstancedMesh", "Line", "LineSegments", "Points",
    "SRGBColorSpace", "LinearSRGBColorSpace", "NoColorSpace", "FrontSide", "BackSide", "DoubleSide",
    "NormalBlending", "AdditiveBlending", "SubtractiveBlending", "MultiplyBlending", "NoBlending",
    "NearestFilter", "LinearFilter", "NearestMipmapNearestFilter", "LinearMipmapNearestFilter",
    "NearestMipmapLinearFilter", "LinearMipmapLinearFilter", "RepeatWrapping", "ClampToEdgeWrapping",
    "MirroredRepeatWrapping", "RGBAFormat", "RGBFormat", "RGFormat", "RedFormat",
    "UnsignedByteType", "HalfFloatType", "FloatType", "StaticDrawUsage", "DynamicDrawUsage", "StreamDrawUsage",
)

# render-service-js/src/validator/identifiers.js @ ced2b09
ALLOWED_ROOT_IDENTIFIERS: tuple[str, ...] = (
    "Math", "Number", "String", "Array", "Object", "Symbol", "JSON", "Map", "Set", "Boolean", "Error",
    "TypeError", "RangeError", "Infinity", "NaN", "undefined", "parseInt", "parseFloat", "isFinite", "isNaN",
    "BigInt", "ArrayBuffer", "DataView", "Int8Array", "Uint8Array", "Uint8ClampedArray", "Int16Array",
    "Uint16Array", "Int32Array", "Uint32Array", "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
)
FORBIDDEN_IDENTIFIERS: tuple[str, ...] = (
    "eval", "Function", "setTimeout", "setInterval", "setImmediate", "queueMicrotask", "fetch",
    "XMLHttpRequest", "WebSocket", "document", "window", "navigator", "localStorage", "sessionStorage",
    "indexedDB", "OffscreenCanvas", "HTMLCanvasElement", "crypto", "Date", "performance", "Proxy", "Reflect",
    "WeakRef", "FinalizationRegistry", "SharedArrayBuffer", "Atomics", "Worker", "process", "module", "global",
    "globalThis", "self", "require",
)
FORBIDDEN_PROPERTY_NAMES: tuple[str, ...] = (
    "constructor", "__proto__", "__defineGetter__", "__defineSetter__", "__lookupGetter__", "__lookupSetter__",
)
MAX_FILE_BYTES = 1_048_576

INSTRUCTIONS_P1 = """You are an expert procedural 3D modeller. You write one JavaScript ES module for three.js r183 that recreates the single object shown in the reference image as faithfully as possible.

HOW THE RESULT IS JUDGED
- The module is executed offline and rendered: 8 views on a white background (front-left 30 degrees, front-right 30 degrees, front from 15 degrees below, front from 30 degrees above, right side, back, left side, top-down), the 4 front views again on a mid-grey background, and a 2x2 grid of three-quarter views from 15 degrees above under a darker studio light with no reflections.
- Camera: perspective, 49.1 degree field of view, 2.0 units from the origin, looking at the origin. The cube from -0.5 to 0.5 fills only about half of the frame, so the object must use the whole cube.
- A vision-language judge compares these renders with the reference image and picks the better of two competing models. It rewards the same object class, silhouette and proportions, the same colours and materials, the same number and arrangement of parts (legs, wheels, buttons, windows, handles, petals, slats, spokes), correct orientation, and clean, recognisable detail. It penalises missing or extra parts, floating or disconnected parts, wrong colours, floors, pedestals, backdrops, boxes or frames around the object, and anything that is not the object.

OUTPUT FORMAT
Reply with exactly one ```javascript fenced code block that contains the complete module, and nothing else.

MODULE CONTRACT (hard rules; one violation and the model scores zero)
1. Exactly one export: `export default function generate(THREE) { ... }`. It is synchronous (no async, await, Promise or generator) and returns a THREE.Group that contains the whole object.
2. Besides that export, the top level of the file contains ONLY `function` declarations. No top-level const, let, var or class anywhere in the file (put constants inside functions). No import, no require, no second export.
3. THREE exists only as the parameter of generate. A helper that needs it takes a first parameter named exactly `THREE` and is called as `helper(THREE, ...)`. Never store THREE in a variable, object, array or closure, never destructure, spread or return it, and never pass it to a method (`obj.build(THREE)` is forbidden).
4. Allowed THREE members, and nothing else: """ + ", ".join(THREE_ALLOWED) + """.
   Not available: lights, cameras, scenes, renderers, loaders, ShaderMaterial and every other material, textures other than DataTexture, sprites, text or font geometry, LineLoop, BufferGeometryUtils or mergeGeometries, RoundedBoxGeometry, CSG. THREE.LuminanceFormat does not exist in r183.
5. Allowed global identifiers: """ + ", ".join(ALLOWED_ROOT_IDENTIFIERS) + """ (Math.random excluded).
   Forbidden anywhere, even as local names: Math.random, Date, performance, console, crypto, window, document, globalThis, self, eval, Function, setTimeout, Proxy, Reflect, fetch, require, process. Never write `.constructor` or `.__proto__`, and never use computed access on THREE, Math, Object, Array or Symbol (such as `Math[name]` or `THREE[type]`).
   For pseudo-random variation use a deterministic helper such as `function hash01(i) { const x = Math.sin(i * 12.9898 + 78.233) * 43758.5453; return x - Math.floor(x); }`.
6. Budgets: keep the file under 200 KB; numeric and string literals under 50 KB in total (build repeated geometry with loops, never embed big arrays); at most 250,000 vertices in total; at most 200 Mesh/Line/Points objects (an InstancedMesh counts once; at most 50,000 instances in total); DataTexture data at most 4 MB; scene depth under 32; generate() must finish in well under one second.
7. Textures: only procedural THREE.DataTexture filled from a Uint8Array (RGBAFormat, needsUpdate = true); no images and no canvas. Use one only when a pattern clearly matters (stripes, a label, a screen, wood grain); plain colours are usually better.
8. LatheGeometry, Shape, ShapeGeometry and ExtrudeGeometry take THREE.Vector2 points; TubeGeometry and CatmullRomCurve3 take THREE.Vector3 points; never pass plain arrays. Keep surfaces closed and visible from every side; use side: THREE.DoubleSide for thin open surfaces (cloth, leaves, paper, blades, petals).

COORDINATES AND SIZE
- Y is up. The object's front faces +Z (toward the default camera); its left-right axis is X. Model the object upright in its natural, canonical pose; do not rotate it to copy the photo's camera angle, because the judge already looks from the front-left and front-right.
- Centre the object at the origin and scale it uniformly so that its largest dimension is about 0.97 units, keeping it entirely inside the cube from -0.5 to 0.5 on every axis. A final fit step re-centres and rescales the returned group, so concentrate on correct proportions between the parts.
- No ground plane, floor, shadow disc, pedestal, base, stand, backdrop, frame or box that is not part of the object itself.

APPEARANCE
- Use MeshStandardMaterial for almost everything (MeshPhysicalMaterial for glass or clear coat, MeshBasicMaterial only for small glowing details). Give every material an explicit hex `color` that matches the image.
- metalness at most 0.6 (more renders black under the studio light); carry metal shades in the colour, for example silver #b8b8b8 with metalness 0.5 and roughness 0.3. roughness between 0.25 and 0.9.
- Avoid pure or near-white surfaces, which vanish against the white background: use at most about #e6e6e6 for white parts and keep visible darker trim, seams or shading where the image shows them. Avoid pure black; use #1c1c1c to #2a2a2a.
- Match the dominant colours and where they are first, then the secondary colours and the small details (eyes, buttons, labels, seams, handles, lights, screws).

MODELLING STRATEGY
- First identify the object class, its main masses and their proportions (width : height : depth, measured from the image), the exact number of every repeated part, and the colour regions. Then build it from primitives (Box, Cylinder, Sphere, Capsule, Cone, Torus), LatheGeometry for round vessels and turned profiles, ExtrudeGeometry with a small bevel for flat custom silhouettes, TubeGeometry for handles, cables, pipes and curved frames, and InstancedMesh for many identical parts.
- Use enough segments for smooth curves (24 to 64 radial segments) and enough parts for recognisable detail (typically 15 to 120 meshes) while staying within the budgets.
- Every part is attached to the object; nothing floats unless it floats in the image.
- The sides, back, top and bottom are rendered too: model them plausibly and completely.
- Wheels of a +Z-facing vehicle face along X: TorusGeometry tyres need rotation.y = Math.PI / 2 and CylinderGeometry hubs need rotation.z = Math.PI / 2.
"""

# p2 (2026-10-19, after the 8-prompt smoke run): identical to p1 except rule 7. In the smoke run one module built
# several 768x768 RGBA DataTextures and failed TEXTURE_DATA_EXCEEDED (post-validate sums distinct textures).
INSTRUCTIONS_P2 = INSTRUCTIONS_P1.replace(
    '7. Textures: only procedural THREE.DataTexture filled from a Uint8Array (RGBAFormat, needsUpdate = true); no images and no canvas. Use one only when a pattern clearly matters (stripes, a label, a screen, wood grain); plain colours are usually better.',
    '7. Textures: only procedural THREE.DataTexture filled from a Uint8Array (RGBAFormat, needsUpdate = true); no images and no canvas. Every DataTexture object counts toward the 4 MB limit separately, even when two hold the same pixels, so make each one at most 256 x 256 pixels, create each texture once and reuse it, and use at most 6 in total. Use one only when a pattern clearly matters (stripes, a label, a screen, wood grain); plain colours are usually better.',
)
assert INSTRUCTIONS_P2 != INSTRUCTIONS_P1

# p3 (2026-10-19, after the p2 smoke run): p2 + a practical complexity budget in rule 6. A p2 module with 136k
# vertices / 125 draw calls timed out (30 s protocolTimeout) on the 8-view white render at 3 CPUs; the field's
# round-55 modules sit at p90 ~17k vertices / ~57 draw calls (L3 map 9b).
INSTRUCTIONS_P3 = INSTRUCTIONS_P2.replace(
    '6. Budgets: keep the file under 200 KB; numeric and string literals under 50 KB in total (build repeated geometry with loops, never embed big arrays); at most 250,000 vertices in total; at most 200 Mesh/Line/Points objects (an InstancedMesh counts once; at most 50,000 instances in total); DataTexture data at most 4 MB; scene depth under 32; generate() must finish in well under one second.',
    '6. Budgets: keep the file under 200 KB; numeric and string literals under 50 KB in total (build repeated geometry with loops, never embed big arrays); at most 60,000 vertices in total (the hard limit is 250,000, but heavy scenes time out in the software renderer); at most 120 Mesh/Line/Points objects (an InstancedMesh counts once; at most 5,000 instances in total); DataTexture data at most 4 MB; scene depth under 32; generate() must finish in well under one second. Spend detail where the judge sees it (silhouette, colour regions, distinctive parts), not on hidden or tiny geometry.',
)
assert INSTRUCTIONS_P3 != INSTRUCTIONS_P2

# p4 (2026-10-20, V1_fix, after E1): p3 with
#  (a) the pose rule: the canonical-upright line is replaced by two lines that make the model reproduce the photo's
#      pose/orientation (E1: pose = top loss reason, 18/51 losses at medium, 15/31 at high), and the wheel line is
#      rewritten so it no longer assumes a front-facing vehicle (side-view vehicles now run along X);
#  (b) two APPEARANCE lines: keep saturation, avoid washed-out transparency (E1: 5 colour/material losses each);
#  (c) contract rule 9: no unary minus/plus before ** (E1-medium: 2 grey-box fallbacks from `-(x) ** 2`).
_P3_POSE = "- Y is up. The object's front faces +Z (toward the default camera); its left-right axis is X. Model the object upright in its natural, canonical pose; do not rotate it to copy the photo's camera angle, because the judge already looks from the front-left and front-right."
_P4_POSE = (
    "- Y is up, +Z points toward the front cameras and X is left-right. Reproduce the object's pose and orientation as the photo shows it; do not \"canonicalise\" it (do not stand it upright, straighten it or turn it to a standard view unless the photo shows it that way). The front views should look like the photo: an object that lies, leans or runs diagonally in the photo keeps that lie, lean or diagonal in the X-Y plane, and the side the camera sees in the photo faces +Z.\n"
    "- Objects photographed lying flat or face-on (boards, trays, plates, pans, coins, medallions, rings, clocks, signs, cards, book covers, ceiling fans) must present that same face toward +Z, so the front views see the face and not a thin edge. Keep the natural upright pose only when the photo shows the object upright."
)
_P3_WHEELS = "- Wheels of a +Z-facing vehicle face along X: TorusGeometry tyres need rotation.y = Math.PI / 2 and CylinderGeometry hubs need rotation.z = Math.PI / 2."
_P4_WHEELS = "- Wheels turn with their axle. For a vehicle whose front faces +Z (axles along X), TorusGeometry tyres need rotation.y = Math.PI / 2 and CylinderGeometry hubs need rotation.z = Math.PI / 2; for a vehicle shown from the side (length along X, axles along Z), tyres keep the default orientation and hubs need rotation.x = Math.PI / 2."
_P3_COLOURS = "- Match the dominant colours and where they are first, then the secondary colours and the small details (eyes, buttons, labels, seams, handles, lights, screws)."
_P4_COLOURS = (
    _P3_COLOURS + "\n"
    "- Match the photo's saturation and hue: do not lighten, grey out or pastelise colours; vivid parts stay vivid and dark parts stay dark.\n"
    "- Avoid washed-out transparency: keep parts opaque unless the photo clearly shows them transparent. Transparent or translucent parts (glass, clear plastic, liquids, bubbles, ice) must stay clearly visible on the white and grey backgrounds: opacity at least 0.35 (transparent: true), a visible tint matching the photo, and visible edges, rims or outlines; coloured contents inside a clear container stay visible."
)
_P3_RULE8 = "8. LatheGeometry, Shape, ShapeGeometry and ExtrudeGeometry take THREE.Vector2 points; TubeGeometry and CatmullRomCurve3 take THREE.Vector3 points; never pass plain arrays. Keep surfaces closed and visible from every side; use side: THREE.DoubleSide for thin open surfaces (cloth, leaves, paper, blades, petals)."
_P4_RULE9 = "9. Never put a unary minus or plus directly before `**`: `-x ** 2` is a syntax error in JavaScript. Write `-(x ** 2)` or `-(x * x)`."


def _replace_once(text: str, old: str, new: str) -> str:
    assert text.count(old) == 1, old[:60]
    return text.replace(old, new)


INSTRUCTIONS_P4 = INSTRUCTIONS_P3
for _old, _new in ((_P3_POSE, _P4_POSE), (_P3_WHEELS, _P4_WHEELS), (_P3_COLOURS, _P4_COLOURS),
                   (_P3_RULE8, _P3_RULE8 + "\n" + _P4_RULE9)):
    INSTRUCTIONS_P4 = _replace_once(INSTRUCTIONS_P4, _old, _new)
del _old, _new
assert INSTRUCTIONS_P4 != INSTRUCTIONS_P3

USER_TEXT_GEN_P1 = "The reference image of the object to recreate is attached. Write the complete module now."

INSTRUCTIONS = {"p1": INSTRUCTIONS_P1, "p2": INSTRUCTIONS_P2, "p3": INSTRUCTIONS_P3, "p4": INSTRUCTIONS_P4}
USER_TEXT = {("p1", "gen"): USER_TEXT_GEN_P1, ("p2", "gen"): USER_TEXT_GEN_P1, ("p3", "gen"): USER_TEXT_GEN_P1,
             ("p4", "gen"): USER_TEXT_GEN_P1}

# Retry policy rp1 (C1-P5): fixed line put after the tag of the attempt that follows a RECORDED
# `provider_incomplete` whose reason is max_output_tokens. Frozen like the prompt versions (tests/test_contract.py).
RETRY_COMPACT_TEXT = {
    "rp1": "A previous attempt at this module ran out of output tokens before it finished. Keep the reasoning brief and the module compact: build repeated parts with loops and small helper functions, skip hidden or tiny detail, and finish the complete module well within the limit.",
}

# Refine f1 (pipeline v2, R1_refine 2026-10-20). One user turn, content parts in this fixed order:
#   [tag + REFINE_TEXT] [REFINE_LABELS reference] [prompt image URL] [REFINE_LABELS renders + 4 data-URL PNGs]
#   [REFINE_LABELS code + the model's own step-1 module in a fence]
# The instructions are the gen prompt version's (same contract). The renders are render spec rv1 (render.py): the
# judge's four Stage-1 front views (front_left, front_right, front_below, front_above) at 512 px on white with
# neutral light, i.e. exactly the angles and background Stage 1 compares with the photo (Stage 1 decides ~80 % of
# duels, E2 §3). Frozen like the prompt versions (tests/test_v2.py).
REFINE_TEXT = {
    "f1": (
        "REFINEMENT STEP: one chance to improve your module before it is judged. Below are the reference photo, renders of your current module and its code. The renders were made by the judge's own renderer from the four front angles the judge compares with the photo first, so every visible difference costs points.\n"
        "1. Compare the renders with the photo and list the concrete discrepancies, most damaging first: pose and orientation (which face points toward the front cameras; lying, standing, leaning or diagonal; tilt and turn), overall proportions (width : height : depth) and the sizes and positions of the parts, missing or extra parts (count every repeated part: legs, wheels, buttons, windows, slats, petals, spokes), colours and materials (hue, saturation, brightness, transparency, metal or gloss), and framing (the object alone fills the frame as in the photo: no floor, base, stand or backdrop). Ignore differences caused only by the plain white background and neutral light of the renders, and by the fit step that re-centres and rescales the object.\n"
        "2. Then write the COMPLETE corrected module that fixes these discrepancies, under the same module contract and rules. Keep every part that already matches the photo; do not simplify, drop correct detail or start over unless the object is fundamentally wrong. If nothing needs fixing, return the module unchanged.\n"
        "Reply format for this step: first the discrepancy list (plain text, at most 12 short bullets, no code), then exactly one ```javascript fenced code block that contains the complete module."
    ),
    # Refine f2 (pipeline v2.1, F2_build 2026-10-20) = f1 + a keep-pose guard (E3: most of f1's losses were pose
    # changes after seeing its own renders: three-quarter car turned to profile, bowl tilted toward the camera, fan
    # made face-on) and the fix priority colours -> proportions -> parts -> framing. Same parts, labels, renders.
    "f2": (
        "REFINEMENT STEP: one chance to improve your module before it is judged. Below are the reference photo, renders of your current module and its code. The renders were made by the judge's own renderer from the four front angles the judge compares with the photo first, so every visible difference costs points.\n"
        "1. Compare the renders with the photo and list the concrete discrepancies, most damaging first, and fix them in this priority: colours and materials (hue, saturation, brightness, transparency, metal or gloss), overall proportions (width : height : depth) and the sizes and positions of the parts, missing or extra parts (count every repeated part: legs, wheels, buttons, windows, slats, petals, spokes), and framing (the object alone fills the frame as in the photo: no floor, base, stand or backdrop). Ignore differences caused only by the plain white background and neutral light of the renders, and by the fit step that re-centres and rescales the object.\n"
        "2. Keep the pose. Keep your current module's pose and orientation (lying, standing, leaning or diagonal; tilt and turn) and keep the side that faces the front cameras, unless the renders clearly contradict the photo (for example the object stands upright in the renders but lies flat in the photo, or the renders show its back, side or underside where the photo shows its front). The photo was taken from one arbitrary viewpoint and the renders from four fixed front angles, so a different camera angle alone is not a pose error: never turn or tilt the object only to make one render look more like the photo.\n"
        "3. Then write the COMPLETE corrected module that fixes these discrepancies, under the same module contract and rules. Keep every part that already matches the photo; do not simplify, drop correct detail or start over unless the object is fundamentally wrong. If nothing needs fixing, return the module unchanged.\n"
        "Reply format for this step: first the discrepancy list (plain text, at most 12 short bullets, no code), then exactly one ```javascript fenced code block that contains the complete module."
    ),
}
_REFINE_LABELS_F1 = {
    "reference": "Reference photo of the object:",
    "renders": "Renders of your current module by the judge's renderer (512 x 512 pixels, white background, neutral light), in this order: front-left 30 degrees, front-right 30 degrees, front from 15 degrees below, front from 30 degrees above:",
    "code": "Your current module (step 1):",
}
REFINE_LABELS = {"f1": dict(_REFINE_LABELS_F1), "f2": dict(_REFINE_LABELS_F1)}
# which render spec (render.RENDER_SPECS) and image detail a refine version sends
REFINE_RENDER_SPEC = {"f1": "rv1", "f2": "rv1"}
REFINE_IMAGE_DETAIL = {"f1": "high", "f2": "high"}
# refine calls per prompt: f1 = one call (a recorded failure keeps the gen module); f2 = attempt 0 + ONE retry
# (attempt 1, same request apart from the tag) only after a RECORDED retryable provider failure
# (backends.refine_retryable) and only if recorded spend + the retry's worst case fits the per-model cap
REFINE_MAX_ATTEMPTS = {"f1": 1, "f2": 2}

# ------------------------------------------------------------------------------------------------------------------
# Submission port (PORT_submission, 2026-10-20): refine VIEW MODES under 404's renderer_client API.
#   "front4": rv1 = the judge's four Stage-1 front views, each its own render() call
#             (RenderOptions layout "view", 512 px, neutral light, white ffffff); texts/labels = REFINE_TEXT /
#             REFINE_LABELS above (unchanged, so the f1/f2 texts measured in E3/E4 are sent byte for byte).
#   "grid":   the runtime's default preview, renderer.render(source) with no options = ONE PNG, a 2 x 2 grid of
#             four views from 15 degrees above (azimuths 24/120/216/312, studio light, grey background). The f1
#             text names white front views, so the grid mode has its own frozen variant of f1 (four sentences
#             changed to describe one grid image) and its own renders label. Only f1 has a grid variant.
REFINE_VIEW_MODES = ("front4", "grid")
_F1 = REFINE_TEXT["f1"]
_GRID_EDITS = (
    ("Below are the reference photo, renders of your current module and its code.",
     "Below are the reference photo, a render of your current module and its code."),
    ("The renders were made by the judge's own renderer from the four front angles the judge compares with the photo first, so every visible difference costs points.",
     "The render was made by the judge's own renderer: a 2 x 2 grid of four views of the object from 15 degrees above, spaced 96 degrees apart around it, so every visible difference costs points."),
    ("1. Compare the renders with the photo and list",
     "1. Compare the render with the photo and list"),
    ("Ignore differences caused only by the plain white background and neutral light of the renders,",
     "Ignore differences caused only by the grey background and studio light of the render,"),
)
_F1_GRID = _F1
for _old, _new in _GRID_EDITS:
    _F1_GRID = _replace_once(_F1_GRID, _old, _new)
del _old, _new
REFINE_TEXT_BY_VIEWS = {
    ("f1", "front4"): REFINE_TEXT["f1"],
    ("f2", "front4"): REFINE_TEXT["f2"],
    ("f1", "grid"): _F1_GRID,
}
REFINE_LABELS_BY_VIEWS = {
    ("f1", "front4"): dict(REFINE_LABELS["f1"]),
    ("f2", "front4"): dict(REFINE_LABELS["f2"]),
    ("f1", "grid"): {
        "reference": _REFINE_LABELS_F1["reference"],
        "renders": "Render of your current module by the judge's renderer (one 1041 x 1041 pixel image: a 2 x 2 grid of four views from 15 degrees above, at 24, 120, 216 and 312 degrees around the vertical axis from the front, top-left to bottom-right, studio light, grey background):",
        "code": _REFINE_LABELS_F1["code"],
    },
}
# Gen user text after the tag (p4 = p1's text; the prompt image follows as a PNG data URL, detail high).
USER_TEXT_GEN = USER_TEXT[("p4", "gen")]

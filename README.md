# Gouge

Closed-model generator for the 404-GEN subnet (SN17) competition, round 1.
One high-reasoning draft from the reference photo, a render-and-refine pass on the judge's four
front views, then a detail pass guarded by a cheap A/B judgement; the module that wins is returned.

## Layout

- `main.py` — entry point: `generate(image: bytes, seed: int, witness, renderer) -> str | None`.
- `sn17gen/` — the pipeline:
  - `settings.py` — the committed mode and flags (the ONE place that selects behaviour);
  - `contract.py` — instruction texts (prompt versions, refine and detail-pass texts) and the output contract;
  - `pipeline.py` — gen → refine → detail pass → guard; budget ledger; fallbacks;
  - `reqbuild.py` — request bodies (deterministic tags, images, code);
  - `calib.py` — numeric colour check of the photo against the judge-view renders (renders only);
  - `repair.py`, `jsscan.py` — structural repair and static checks of the model's module;
  - `views.py` — judge front views and the default preview through the renderer client;
  - `credits.py` — credit accounting against the per-prompt limits.
- `miner_reference/` — local batch runner and render checkpoints used for dry runs; not used by `main.py`.
- `tests/` — `tests/test_gouge.py` covers the flags below (`pytest tests/test_gouge.py -q`); the remaining
  files are the inherited request-byte tests of earlier modes and are not kept green.

## Flags (`sn17gen/settings.py`)

| Flag | Committed | Effect |
|---|---|---|
| `MODE` / `REFINE_VIEWS` | `v2` / `front4` | one draft, one refine on the judge's four front views |
| `PROMPT_VERSION` | `p6` | real budgets, detail demand, numeric material table, 150k vertex target |
| `COLOUR_CALIB` | on | measured lightness note in the refine prompt when the render is clearly off |
| `SECOND_REFINE` | on | detail pass: inventory of parts and surface detail, applied as an edit |
| `REFINE2_GUARD` | on | A/B judgement refine vs detail pass; the detail pass wins only when clearly better |
| `TWO_DRAFTS` | off | second draft + judge (measured: no gain) |

## Limits

Per prompt: 4 model calls, ≈1.3 M credits (max measured 1.8 M of 5 M), final source ≤ 30 KiB.
Every branch is a function of the recorded bytes, so replay reproduces the same module.

## Run

```
python -m pytest tests/test_gouge.py -q
```

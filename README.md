# sn17gen: 404-GEN closed-source miner

Python submission for the 404-GEN closed-source model competition (SN17, the miner interface in
`404-gen-subnet/miner-reference` @ 72324c6). The runtime imports `main.py` and calls

```python
generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None
```

once per prompt image. The return value is one Three.js ES module.

## Pipeline

The mode is set by the constants at the top of `sn17gen/settings.py`. Nothing else selects it.

| Constant | Values | Committed |
|---|---|---|
| `MODE` | `v1` (generation only) / `v2` (generation + one render-and-refine call) | `v2` |
| `REFINE_VERSION` | `f1` / `f2` (keep-pose text + one retry after a recorded provider failure) | `f1` |
| `REFINE_VIEWS` | `front4` (four 512 px white front views) / `grid` (the runtime's default preview) | `front4` |
| `REFINE_CREDIT_CAP` | worst-case credit cap of the refine call(s), 3,000,000-5,000,000 (gen calls always use 3,000,000) | `4_000_000` |
| `TICKET` | label in every request tag; each own hotkey's commit sets its own label (the hotkey name) | `t0` (base) |
| `SERVICE_TIER_FRACTION` | share of prompts sent on `FAST_TIER`, chosen by the prompt image's sha256, 0.0-1.0 (0.0 = no `service_tier` key anywhere) | `0.0` |
| `FAST_TIER` | OpenAI `service_tier` of the selected prompts: `fast` (or `ultrafast`) | `fast` |
| `REFINE_RETRY` | v2: one refine retry (attempt 1, same request) after a recorded retryable failure or an HTTP-200 stream cut; `False` = 0.2.1 behaviour | `True` |

The committed configuration is sn17miner **v2.2**: v2 (gen + refine f1 on the four front views) with the refine
call allowed up to 4,000,000 credits per prompt. Round 7 measured the alternatives: refine texts f3 (−2.7 pts
pooled) and f4 (0.0) missed the pre-registered +7 bar, so f1 stays; the 4M refine cap rescued the refines that the
3M rule skipped (8 of 381 prompt-draws; 5 judge wins, 2 draws, 0 losses against the unrefined module).

Each prompt runs these steps:

1. **Generation.** One `gpt-6-astra` request (effort high) with prompt p4 and the prompt PNG as a data URL.
   - The reply is repaired with fixed pure-Python rules (`repair.py` r3) and structurally checked.
   - The repaired module is then rendered. A `RenderRejected` is the production validator verdict.
   - A recorded provider failure, a rejected reply or a render rejection moves on to attempt 1.
   - After an output-cap stop, attempt 1 carries a frozen "be compact" line and asks for 32k tokens if that fits
     the budget.
2. **Refine (v2 only).** One request with the photo, the gen module's previews (PNG bytes exactly as rendered)
   and the model's own module.
   - The refined module is used only if it passes the structural check and a render.
   - With `REFINE_RETRY` (0.2.2): after a recorded retryable failure of that call, one more call with the same
     request (only the tag's attempt digit differs). See "Refine retry" below.
3. **Fallback.** If no candidate passes, a neutral box is returned (it is render-checked too). `None` is returned
   only if even that render is rejected.

The budget is counted in competition credits from verified usage. Before every call, recorded spend plus the
call's worst case must stay within that step's cap:

- every gen call (and the 32k-or-24k choice of the output-cap retry): 3,000,000 credits;
- every refine call (and the retry): `REFINE_CREDIT_CAP`, committed 4,000,000.

So the gen requests and the refine request bodies are exactly those of the 3M rule; only the refine call runs on
more prompts (those with gen spend + refine worst case between 3M and 4M, typically long modules after an
output-cap retry). The per-prompt worst case is 4,000,000 credits (the README's nominal limit is 5,000,000). At
most 4 calls are made per prompt (3 with f1 and `REFINE_RETRY = False`).

## Refine retry (0.2.2)

In the VPS smoke test of 10-23 (VPS_HOST.md F3) one refine stream ended with HTTP 200 after the reasoning items and
no `response.completed`; f1 has no retry, so that prompt shipped its unrefined gen module (v1 quality).
`REFINE_RETRY = True` (committed in 0.2.2, launch variant `v2r`) adds exactly one more refine call in that case.

- **Retried** (once): HTTP 5xx; a stream `error` event on HTTP 200 (e.g. `server_is_overloaded`) unless its code is
  a credit/quota code; `response.failed`; `response.incomplete`; and an HTTP-200 stream that decodes event by event
  but has no terminal event (the pinned decoder's "Missing completed OpenAI response", which the witness client
  reports as `invalid_provider_response`; counted as `provider_incomplete`). This is refine f2's rule plus the
  stream cut (`pipeline.refine_retry_reason`).
- **Never retried:** provider rejections (HTTP 400/401/403/404/429), any other undecodable record (a broken JSON
  frame, bad UTF-8, inconsistent or duplicate events), credit/quota codes, non-provider failures (transport,
  gateway, client: the runtime recovers those or leaves them unresolved), a reply that fails the structural check
  and a refined module the renderer rejects. A failed retry keeps the gen module; there is never a third call.
- **Same request:** the retry body is attempt 0's body with `step=refine attempt=0]` → `attempt=1]` (one byte):
  the same f1 text, photo, gen PNGs (not re-rendered) and code, and the same `service_tier` key if any.
- **Budget:** the retry runs only if recorded spend + its worst case ≤ `REFINE_CREDIT_CAP` (4,000,000); otherwise it
  is skipped (`budget_stop`). With `REFINE_RETRY`, a recorded failure whose usage the decoder cannot read
  (`invalid_provider_response`, gen or refine) is charged its worst case, so a cut call's unknown billing counts.
  A 5xx or stream `error` event without a response stays at 0 credits (0.2.1's rule).
- **Replay:** every decision reads only the recorded outcome (code, source, status, the pinned decoder's error),
  so a replay serving the same records takes the same branch.
- **Normal path:** without a refine failure, every request equals 0.2.0's bytes (`FROZEN_BODY`), and with
  `REFINE_RETRY = False` every path is 0.2.1's. `tests/test_refine_retry.py` checks both.

## Service tier (0.2.1)

`SERVICE_TIER_FRACTION` exists so that a round with a short window can still finish (round-8 tier probe U1). The
committed value is 0.0, which sends exactly the request bytes of 0.2.0.

- A prompt goes on the fast tier iff `int(sha256(image).hexdigest()[:8], 16) / 2**32 < SERVICE_TIER_FRACTION`.
  `image` is the byte string the runtime passes to `generate()`.
- Every request of a selected prompt ends with `"service_tier": FAST_TIER`. That covers both gen attempts, the
  output-cap retry, the refine call and f2's retry. The key goes last, which makes the body 22 bytes longer for `fast`.
- Requests of the other prompts carry no `service_tier` key, so their bytes are identical to 0.2.0's.
  `tests/test_tier.py` checks this against 0.2.0's frozen body table, and checks that the tier version is an exact
  insert.
- The choice is a pure function of the image bytes. It never depends on the seed, the ticket, the clock, the queue,
  the time left in the window or any response. The echoed `service_tier` in a response is never read.
- Credits do not depend on the tier: the competition counts Standard rates, so the caps and every budget decision
  stay the same. Real cash does change. Fast costs about 2× Standard (U1: 1.99-2.03×) and is about 1.7-1.9× faster
  in tokens per second. Ultrafast costs about 6× and is about 4-5× faster.

### Contingency variants

Each variant is one committed set of constants. `mining/launch/make_commits.py --variant <name>` builds the
per-hotkey commits from it. Only `TICKET` differs between the hotkeys of a variant.

| Variant | Base | Committed change | Fits 85 % of a 24 h / 48 h window up to N/W | Real cash per hotkey per 128 | Use when |
|---|---|---|---|---|---|
| `v2` (default) | 0.2.0 `57119947` | none | 1.13-1.32 / 2.3-2.7 | ≈ $168 | front4 can be expressed (gate G1) and the window fits Standard |
| `v2fast50` | 0.2.1 | `SERVICE_TIER_FRACTION = 0.5` | 1.44-1.68 / 3.0-3.5 | ≈ $252 | G1 GO, Standard v2 does not fit, half Fast does |
| `v2fast100` | 0.2.1 | `SERVICE_TIER_FRACTION = 1.0` | 2.0-2.3 / 4.1-4.8 | ≈ $336 | G1 GO, half Fast does not fit, all Fast does |
| `v1` | 0.2.0 `57119947` | `MODE = "v1"` | 2.0-2.5 / 4.2-5.1 | ≈ $80-87 | G1 NO-GO (never grid refine), or not even all Fast fits |
| `v2r` (launch default from 0.2.2) | 0.2.2 | none (`REFINE_RETRY = True` is committed) | as `v2` | ≈ $168 (+ ≈ $0.7 per retried prompt) | wherever `v2` fits: `v2` + one refine retry after a recorded provider failure / stream cut |

The ranges come from U1's window model (serial hours per 128 prompts per hotkey at a witness load of N/W; low end =
today's absolute rates, high end = E5's times divided by the measured speed-ups). They assume the other miners hold
the witness workers as briefly as we do. If they hold them at Standard speed, Fast helps less. The rules, the cost
at 5 hotkeys and the caveats are in `mining/launch/LAUNCH_RUNBOOK.md` (gates G1, G8 and G9).

`v2` and `v1` are built from 0.2.0 itself, so `--variant v2` gives the same hashes as before (the 10-21 dry run
with the same `--date`). Their requests are byte-identical to 0.2.1's with the fraction at 0.0. `v2r` is built from
0.2.2; on every prompt without a refine failure it sends `v2`'s bytes.

## Determinism

- Requests are dicts built in one fixed key order, serialized by the bundled witness client.
- The only per-call text is the tag `[pid=<sha256(image)[:16]> seed=<seed> ticket=<t> step=<s> attempt=<k>]`.
- No clock, randomness, set order, host state, environment or network is used outside the supplied clients.
  `tests/test_requests.py` checks the import surface.
- Every branch depends only on recorded responses, recorded usage and recorded render outcomes.
- One JSON diagnostics line per prompt goes to stderr. It is never used as an input.

## Layout

| Path | What |
|---|---|
| `main.py` | entry point |
| `sn17gen/settings.py` | committed mode and all request-shaping settings |
| `sn17gen/contract.py` | frozen instruction, retry and refine texts |
| `sn17gen/reqbuild.py` | deterministic request builder |
| `sn17gen/credits.py` | rate table copy, credits, worst-case bound, ledger |
| `sn17gen/repair.py`, `jsscan.py` | pure-Python JS extraction, repair rules, structural check, fallback |
| `sn17gen/views.py` | the only module that calls `renderer_client` (preview options) |
| `sn17gen/pipeline.py` | the per-prompt procedure |
| `tests/` | unittest suite with fake clients (needs the runtime's bundled `witness_client` and `renderer_client`) |

## 0.3.0 (port to 404's published clients, 44d2a64; branch port-0.3.0)

- **C3 front4 views:** `RenderOptions(thetas=(t,), phis=(p,), img_size=512, cam_radius=2.0, cam_fov_deg=49.1,
  bg_color="ffffff", lighting="neutral")`: one angle pair = a single-view PNG via `/render/grid`. Byte-identical to
  0.2.2's emulator "view" renders for an opaque framebuffer (12/12 PNGs on render-service-js 0.4.10; a material that
  writes alpha < 1, e.g. NoBlending on a transparent material, can differ between /render and /render/grid: refine
  image quality only, never replay); the default grid query also matches. 5/5 live-smoke renders are byte-identical on
  404's pinned renderer image (witness-render-bench@sha256:75f8a00c).
- **C2 rejections:** 404's `RenderRejected(detail_json)`; `views.rejection_info` reads (stage, rule) from the 422
  report for diagnostics only.
- **C1 infrastructure errors:** a `WitnessError` whose source is not "provider" (client / gateway: transport,
  capture_failed, ExchangePending, budget refusal) propagates to the runtime, as in 404's reference. 0.2.2 treated it
  as a recorded outcome and paid for more calls.
- **C4 fast tiers dormant by choice:** `service_tier_fraction` must be 0.0. (404's Pod admits fast / ultrafast at
  Standard rates and refuses only flex; the Pod's concurrency removes the need, and a non-zero fraction would need a
  re-gate through the real GenerationWitness / ReplayWitness.)
- **C5 budget bound:** + the 25 bytes of the Pod's injected `"service_tier":"default"`.
- **Request content unchanged:** the Pod body = 0.2.x's body + `"service_tier":"default"` (stream last).
  `tests/test_requests.py` freezes both (FROZEN_BODY, FROZEN_POD_BODY).
- Only the service's 422 (`RenderRejected`) is a recorded outcome; `RenderUnavailable` (transport, 503, a render
  crash or timeout) and any other renderer exception propagate to the runtime (tests/test_pipeline.py
  TestRenderInfraPropagates).
- Tests: 91 (run with 404's witness-client 0.2.13rc6 + renderer-client src on PYTHONPATH, inside sn17-r5-runtime:0.1).
  The record → real `ReplayWitness` → `finish()` probe passes 3/3 (normal, HTTP-500 refine retry, rp1 + stream-cut
  retry).

## 0.3.1 (both witness-client generations: rc6 at 44d2a64 / 92bedb6, and 29a0d82; branch port-0.3.1)

404's `feat/witness-public-release` 29a0d82 (10-25) changed what `witness.responses.create()` returns, under the same
version string 0.2.13rc6 (pin the client by tree hash, never by version):

- `create(stream=True)` returns a `ModelStream` (an SSE event iterator with no `output_text`). 0.3.0 passed
  `stream=True`, so on 29a0d82 every gen reply read as empty text and every prompt fell back after two paid calls.
- `create()` (stream False, the new default) returns an OpenAI `Response` with its `status`: `failed` and `incomplete`
  are returned, not raised. HTTP != 200 raises SDK `APIStatusError` subclasses, a stream `error` event raises
  `openai.APIError` chained from the provider `WitnessError`, and an undecodable HTTP-200 record (a stream cut, a broken
  frame, bad UTF-8, ...) raises the decoder's bare `ValueError`.

0.3.1 routes every call through `pipeline.witness_call`, which works with both generations:

- `stream` is never passed to `create()`. The request dict keeps its `"stream": True` (so `FROZEN_BODY` and the
  worst-case bound are unchanged), and both generations write `"stream":true` at the end of the body themselves, so
  the recorded Pod bodies are unchanged too (`FROZEN_POD_BODY`).
- 29a0d82's objects become exactly the provider `WitnessError` that rc6 raised for the same recorded bytes:
  - a `Response` whose status is not `completed` → `WitnessError(provider_<status>, response=…)`, with the
    provider code and message from `response.error`;
  - an `openai.APIStatusError` → `witness_client.response.provider_error(status, recorded body)`, i.e. rc6's own
    decoding (`provider_rejected` for 400/401/403/404/429, else `provider_error`, `provider_code` from the
    `{"error": {...}}` body);
  - an `openai.APIError` whose cause is a provider `WitnessError` → that `WitnessError`;
  - a `ValueError` raised inside the client's decoder (`witness_client.response.completed_response`, including its
    SSE, JSON and UTF-8 decoding) → `WitnessError(invalid_provider_response)` chained from it, so `stream_cut()`
    matches a cut again.
- So `stream_cut`, `hit_output_cap` (rp1), `refine_retryable` (f2 / REFINE_RETRY), `usage_from_error` and the ledger
  see the same objects. These are pure functions of the recorded bytes, so replay takes the same branch.
- The reply text is read with `pipeline.reply_text` on both generations (review F2, 10-26): a completed response
  recorded without an `output` key reads as `""` (a rejected output, then the next attempt), as on rc6. Before, the
  SDK's `Response.output_text` raised `TypeError` out of `generate()` on 29a0d82 and the prompt stayed pending.
- Every recorded outcome takes the same branch on both generations, with two documented exceptions (pinned by
  `TestKnownDivergences`; both are pure functions of the recorded bytes):
  - the reply text: rc6 reads the text streamed in `response.output_text.done` events, 29a0d82 reads the message
    items of the terminal `output` (it rebuilds them from the streamed text only when `output` is empty). They agree
    whenever the terminal `output` carries the streamed message, which every OpenAI API-mode completion does. If
    `output` is non-empty but holds no output_text (only a reasoning item, a refusal, an empty message), 29a0d82
    reads `""` where rc6 reads the streamed text: at gen that costs one more gen attempt, at refine the gen module is
    kept (review F1). `witness_call` cannot repair this: 29a0d82 hands it only the `Response`;
  - a `response.failed` whose `error` is not an object: rc6's decoder raises `AttributeError` (its runtime reports
    `recording_unresolved` and the prompt stays pending); on 29a0d82 it is a recorded `provider_failed` without a
    provider code, so it is retryable (gen: the next attempt; refine: the f2 / REFINE_RETRY retry). Accepted as the
    safer outcome; no real OpenAI record has this shape (review F3).
- Everything else propagates to the runtime, as in 0.3.0 (C1): client / gateway `WitnessError`s
  (`ExchangePending`, `ReplayMismatch`, capture or transport failures), a `ValueError` raised outside the decoder
  (request validation, gateway parsing), and an `APIError` without a provider cause. This is narrower than the scratch
  prototype, which caught every `ValueError` (ACTIONS N3).
- rc6's objects pass through unchanged. New imports: `openai` (exception classes only; a fixed runtime package,
  `openai==3.29.0`, and a dependency of witness-client) and `witness_client.response`.
- Unchanged: request content, credits, caps, the plan, and every decision on the normal path. Under 29a0d82's replay
  budget rule (`BudgetRejected("Consumed call has unknown usage")`), a prompt with a cut, an HTTP error or a stream
  error is rejected at replay whatever we do. `REFINE_RETRY` keeps its rc6 behaviour (decision D2).
- Render quota (A3): at most 9 renders per prompt in v2 front4 (gen attempt 0 front4 4 + attempt 1 front4 4 + the
  refined module's check 1, or 8 + the leftover / fallback check 1); v1 and grid at most 3. The final is always a
  source this prompt rendered and the renderer accepted, so the Pod's final save adds no render. Renders run one at a
  time. `tests/test_render_quota.py` checks all of this.
- Tests: 168 (0.3.0's 91 + 77; the 10-26 review fixes added 4). They are green on 44d2a64 (site44), 92bedb6
  (site92) and 29a0d82 (site29), with hash seeds 0 and random and 0 skipped.
  - `tests/fakes.py` models both surfaces from each scripted outcome's recorded bytes: the real decoder of the client
    on the path, plus a port of the other generation's decoder. `DEFAULT_SURFACE` is 29a0d82.
  - `tests/test_client_surfaces.py` checks that `create()` never receives `stream`. For every recorded outcome type
    at every call site under v2r / v2 / f2 / v1, it checks that both surfaces give the same requests, renders, events,
    ledger and output. It also checks that the mapping gives rc6's exact objects and that infrastructure failures
    propagate, shows that the 0.3.0 call shape falls back on 29a0d82, and re-runs the flow tests of
    `test_pipeline` / `test_refine_retry` on rc6. The outcome table includes a completed response without `output`
    (F2) and an HTTP-503 quota body that is invalid UTF-8 (F4: `witness_call` must decode the raw body, never
    `.text`); `TestKnownDivergences` pins the two documented differences.
  - The `scenario.py` digest is equal on both surfaces.
  - 13 mutants (incl. 0.3.0's own `create(**params)`: 48 failures) are all caught on both clients; so are the
    review mutants (the body read from `.text`, the reply read without `reply_text`).
  - The real-client replay probes (`probe_replay29.py` on 29a0d82, `probe_replay44.py` on 44d2a64) give reports
    identical to the prototype's PASS runs.

## Tests

Run them inside the runtime image (Python 3.13, openai 3.29.0) with 404's published clients on the path (0.3.1:
witness-client 0.2.13rc6 at 44d2a64 / 92bedb6 or at 29a0d82, with the matching renderer-client src), the submission
at /sub and 404's repo at /repo (for the validator allowlist test):

```sh
python -m unittest discover -s tests -t tests      # PYTHONPATH=<witness-client>:<renderer-client>:/sub:/sub/tests
```

The suite has 168 tests in 0.3.1. 0.3.0 had 91; 0.3.1 adds `test_client_surfaces.py` (75, incl. the flow tests
re-run on rc6) and `test_render_quota.py` (2). (0.2.2 had 97; 0.3.0 replaced the fast-tier selection tests with three
retired-tier tests and added the Pod-body, rejection-format, infrastructure-propagation and renderer-failure tests.)
Fixtures pin ticket `t0` and `REFINE_RETRY = False` (`tests/fakes.py`), so the suite passes with any committed `TICKET`
and `REFINE_RETRY`; `SERVICE_TIER_FRACTION` must be 0.0 (0.3.0).

## Versions

| Version | Commit | Change |
|---|---|---|
| 0.1.0 | `e3609908` | port of sn17miner v1 / v2 (f1, f2; front4, grid) to `generate(image, seed, witness, renderer)` |
| 0.2.0 | `57119947` | sn17miner v2.2: refine credit cap 3,000,000 → 4,000,000 (`REFINE_CREDIT_CAP`); gen calls, texts and request bytes unchanged |
| 0.2.1 | `40e53d34` | committed `SERVICE_TIER_FRACTION` (0.0) + `FAST_TIER` (`fast`); fixtures pin ticket t0 + fraction 0.0 (launch patch 0001 folded in); at 0.0 every request byte equals 0.2.0's |
| 0.2.2 | `54d1cd10` | committed `REFINE_RETRY` (`True`): one refine retry after a recorded retryable failure or an HTTP-200 stream cut, within the 4M refine cap; unknown-usage failures charged at worst case; normal-path bytes = 0.2.0's; `False` = 0.2.1 |
| 0.3.0 | `674fc903` | port to 404's published witness / renderer clients (44d2a64): front4 via `RenderOptions(thetas, phis, ...)`, `RenderRejected(detail_json)`, client / gateway `WitnessError` and `RenderUnavailable` propagate to the runtime, fast tiers dormant, +25 B Pod bound; request content = 0.2.2's |
| 0.3.1 | this commit | both witness-client generations (rc6 at 44d2a64 / 92bedb6, and 29a0d82): `stream` is never passed to `create()`; 29a0d82's non-completed `Response`s, SDK status errors, stream-error `APIError`s and decoder `ValueError`s become rc6's provider `WitnessError`s; other errors propagate; reply text via `reply_text`; ≤ 9 renders per prompt (tested); request content and normal-path branches = 0.3.0's (two documented record-shape differences on 29a0d82) |

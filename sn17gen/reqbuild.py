"""Deterministic request builder (sn17miner reqbuild t1/rp1/f1/f2, ported to bytes-in prompts).

Every request is a dict built in ONE fixed key order
    stream, model, instructions, input, reasoning, max_output_tokens, store [, service_tier]
(the witness serializer prepends ``"tools": []``). ``service_tier`` is present only on the prompts that
``Settings.service_tier(pid)`` puts on the fast tier (a pure function of sha256(image); SERVICE_TIER_FRACTION 0.0 = never,
= 0.2.0's bytes), and then on every gen and refine request of that prompt. The only per-call text is the tag
    [pid=<sha256(image)[:16]> seed=<seed> ticket=<ticket> step=<gen|refine> attempt=<k>]
plus, after a RECORDED output-cap failure (retry policy rp1), the frozen compact line. The prompt image goes in as a
PNG data URL of the exact prompt bytes; rendered previews go in as data URLs of the exact PNG bytes the renderer
returned. Nothing comes from a clock, randomness, set order, the host or an SDK object.
"""

from __future__ import annotations

import base64
import hashlib
from typing import Any

from .contract import INSTRUCTIONS, REFINE_IMAGE_DETAIL, REFINE_LABELS_BY_VIEWS, REFINE_TEXT_BY_VIEWS, RETRY_COMPACT_TEXT, USER_TEXT_GEN
from .settings import Settings

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SEED_MAX = 2**64 - 1
VARIANT_OUT_CAP = "out_cap"   # the attempt after a recorded provider_incomplete(max_output_tokens), rp1


def prompt_id(image: bytes) -> str:
    """Stable prompt id (the runtime gives no stem): the first 16 hex digits of sha256(image bytes)."""
    return hashlib.sha256(bytes(image)).hexdigest()[:16]


def check_seed(seed: Any) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int) or not (0 <= int(seed) <= SEED_MAX):
        raise ValueError(f"seed must be an int in [0, 2**64 - 1], got {seed!r}")
    return int(seed)


def _mime(data: bytes) -> str:
    if data.startswith(PNG_SIGNATURE):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"   # the spec promises PNG prompt bytes


def data_url(data: bytes) -> str:
    """Data URL of the exact bytes (standard base64; the payload is transport only)."""
    data = bytes(data)
    return f"data:{_mime(data)};base64," + base64.b64encode(data).decode("ascii")


def call_tag(s: Settings, pid: str, seed: int, step: str, attempt: int) -> str:
    if type(attempt) is not int or attempt < 0:
        raise ValueError("attempt must be a non-negative int")
    return f"[pid={pid} seed={seed} ticket={s.ticket} step={step} attempt={attempt}]"


def _add_service_tier(params: dict[str, Any], s: Settings, pid: str) -> None:
    """Append ``"service_tier"`` as the LAST key iff this prompt is on the fast tier (gen and refine alike);
    otherwise add nothing, so the body is byte-identical to 0.2.0's."""
    tier = s.service_tier(pid)
    if tier is not None:
        params["service_tier"] = tier


def build_gen_params(s: Settings, pid: str, seed: int, image_url: str, attempt: int, *,
                     variant: str | None = None, max_output_tokens: int | None = None) -> dict[str, Any]:
    """Gen request of one attempt. ``variant``/``max_output_tokens`` are set only by the rp1 retry plan."""
    head = call_tag(s, pid, seed, "gen", attempt)
    if variant is None:
        text = f"{head} {USER_TEXT_GEN}"
    elif variant == VARIANT_OUT_CAP:
        text = f"{head} {RETRY_COMPACT_TEXT[s.retry_policy]} {USER_TEXT_GEN}"
    else:
        raise ValueError(f"unknown request variant {variant!r}")
    mot = s.max_output_tokens if max_output_tokens is None else max_output_tokens
    if type(mot) is not int or mot <= 0:
        raise ValueError("max_output_tokens must be a positive int")
    params: dict[str, Any] = {}
    params["stream"] = True
    params["model"] = s.model
    params["instructions"] = INSTRUCTIONS[s.prompt_version]
    params["input"] = [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": text},
                {"type": "input_image", "image_url": image_url, "detail": s.detail},
            ],
        }
    ]
    params["reasoning"] = {"effort": s.effort}
    params["max_output_tokens"] = mot
    params["store"] = False
    _add_service_tier(params, s, pid)
    return params


def build_refine_params(s: Settings, pid: str, seed: int, image_url: str, code: str, pngs: list[bytes],
                        attempt: int = 0) -> dict[str, Any]:
    """The refine request: same instructions as gen; one user turn with
    [tag + frozen refine text] [label] [prompt image] [label] [preview PNG data URLs] [label + the model's own
    step-1 module in a javascript fence]. ``attempt`` 1 only for refine f2's one retry (same request, new tag)."""
    if not s.refine:
        raise ValueError("mode v1 has no refine step")
    if type(attempt) is not int or not (0 <= attempt < s.refine_max_attempts):
        raise ValueError(f"refine {s.refine_version} has attempts 0..{s.refine_max_attempts - 1}, got {attempt!r}")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("refine needs the step-1 module text")
    want = 4 if s.refine_views == "front4" else 1
    if len(pngs) != want or any(not bytes(p).startswith(PNG_SIGNATURE) for p in pngs):
        raise ValueError(f"refine views {s.refine_views} need {want} PNG(s), got {len(pngs)}")
    key = (s.refine_version, s.refine_views)
    labels = REFINE_LABELS_BY_VIEWS[key]
    detail = REFINE_IMAGE_DETAIL[s.refine_version]
    content: list[dict[str, Any]] = [
        {"type": "input_text", "text": f"{call_tag(s, pid, seed, 'refine', attempt)} {REFINE_TEXT_BY_VIEWS[key]}"},
        {"type": "input_text", "text": labels["reference"]},
        {"type": "input_image", "image_url": image_url, "detail": s.detail},
        {"type": "input_text", "text": labels["renders"]},
    ]
    for png in pngs:
        content.append({"type": "input_image", "image_url": data_url(bytes(png)), "detail": detail})
    fence = "```"
    content.append({"type": "input_text", "text": f"{labels['code']}\n{fence}javascript\n{code}{fence}"})
    params: dict[str, Any] = {}
    params["stream"] = True
    params["model"] = s.model
    params["instructions"] = INSTRUCTIONS[s.prompt_version]
    params["input"] = [{"role": "user", "content": content}]
    params["reasoning"] = {"effort": s.refine_effort}
    params["max_output_tokens"] = s.refine_max_output_tokens
    params["store"] = False
    _add_service_tier(params, s, pid)
    return params

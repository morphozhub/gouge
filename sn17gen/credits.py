"""Per-prompt budget in competition credits (README "Budget"), from verified provider usage only.

    call_credits = input_tokens * input_weight + output_tokens * output_weight      (exact Fractions, no rounding)

All input tokens count once at the full input rate (cached tokens included), output includes reasoning; a request
with more than 272,000 input tokens uses the long-context weights for the whole request. The weights are a frozen
copy of ``miner-reference/budgets/pricing.json`` @ 72324c6 (checked 2026-10-19), as decimal strings.

The cap is a WORST-CASE cap: before every call, recorded spend + the call's worst case must stay within the step's
cap: ``Settings.credit_cap`` for gen calls (3,000,000 = the $3 per model of sn17miner), ``Settings.refine_credit_cap``
for refine calls (v2.2: 4,000,000); the README's nominal limit is 5,000,000.
The worst case of a request = its text bytes (body bytes minus base64 image payloads; a token is at least one byte)
+ an image-token bound per image at its detail + its own ``max_output_tokens``. Recorded usage never comes from a
clock or the host, so every decision taken from this ledger replays identically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

PRICING_CHECKED_ON = "2026-10-19"
CREDITS_PER_PROMPT = 5_000_000
CALLS_PER_PROMPT = 60
LONG_CONTEXT_ABOVE_INPUT_TOKENS = 272_000
# model -> (input, output, long input, long output) credits per token
RATES: dict[str, tuple[str, str, str, str]] = {
    "gpt-6-astra": ("10", "50", "20", "75"),
    "gpt-6.1-sol": ("2", "10", "4", "15"),
    "gpt-6-sol": ("2", "10", "4", "15"),
    "gpt-6-luna": ("0.10", "0.50", "0.20", "0.75"),
    "gpt-5.6-sol": ("4", "20", "8", "30"),
    "gpt-5.6-terra": ("2", "12", "4", "18"),
    "gpt-5.6-luna": ("0.20", "1.20", "0.40", "1.80"),
}
# Pre-call bound on one image's input tokens (never used for accounting). Astra 1024^2 at detail high = 1,229
# tokens (fact base G1); the bound leaves 2x headroom.
IMAGE_TOKEN_BOUND = {"low": 400, "high": 2_600, "auto": 2_600, "original": 12_000}


def call_credits(model: str, input_tokens: int, output_tokens: int) -> Fraction:
    if model not in RATES:
        raise ValueError(f"model {model!r} has no frozen rate")
    w_in, w_out, l_in, l_out = (Fraction(x) for x in RATES[model])
    if input_tokens > LONG_CONTEXT_ABOVE_INPUT_TOKENS:
        w_in, w_out = l_in, l_out
    return input_tokens * w_in + output_tokens * w_out


def credits_str(c: Fraction) -> str:
    """Exact decimal text for diagnostics (the weights have at most 2 decimals)."""
    if c.denominator == 1:
        return str(c.numerator)
    whole, rest = divmod(c.numerator * 100, c.denominator)
    return f"{whole // 100}.{whole % 100:02d}" + ("" if rest == 0 else "+")


def body_bytes(params: dict[str, Any]) -> int:
    """Byte length of the request body as the witness serializer writes it ({"tools": [], **params}, compact JSON,
    UTF-8). Used only for the worst-case bound."""
    return len(json.dumps({"tools": [], **params}, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False).encode("utf-8"))


def image_parts(params: dict[str, Any]) -> list[tuple[str, int]]:
    """(detail, base64 payload bytes) per input image; payload 0 for a URL image. Base64 has no JSON escapes, so
    body bytes minus the payloads = the text bytes."""
    out = []
    for item in params.get("input") or []:
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "input_image":
                u = part.get("image_url") or ""
                payload = len(u) - (u.index(",") + 1) if u.startswith("data:") and "," in u else 0
                out.append((str(part.get("detail") or "auto"), payload))
    return out


POD_BODY_EXTRA_BYTES = len(',"service_tier":"default"')   # the Pod witness adds it to every body (C5)


def worst_credits(params: dict[str, Any]) -> Fraction:
    parts = image_parts(params)
    text_bytes = body_bytes(params) + POD_BODY_EXTRA_BYTES - sum(p for _, p in parts)
    input_bound = text_bytes + sum(IMAGE_TOKEN_BOUND[d] for d, _ in parts)
    return call_credits(str(params["model"]), input_bound, int(params["max_output_tokens"]))


@dataclass
class CallUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    known: bool = True

    @classmethod
    def from_usage(cls, usage: Any) -> "CallUsage":
        if usage is not None and hasattr(usage, "model_dump"):
            usage = usage.model_dump(exclude_unset=True)
        if not isinstance(usage, dict) or "input_tokens" not in usage or "output_tokens" not in usage:
            return cls(known=False)
        def _int(v: Any) -> int | None:
            return int(v) if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
        it, ot = _int(usage.get("input_tokens")), _int(usage.get("output_tokens"))
        if it is None or ot is None:
            return cls(known=False)
        return cls(it, ot, True)


def usage_from_error(err: BaseException) -> CallUsage:
    """Usage of a recorded provider failure. A failure that carries a response (response.failed / incomplete) is
    charged its usage, or its worst case when the usage is missing; one without a response (HTTP 4xx/5xx, stream
    error event) has no billed tokens (sn17miner F2 rule)."""
    resp = getattr(err, "response", None)
    if resp is None:
        return CallUsage()
    return CallUsage.from_usage(getattr(resp, "usage", None))


@dataclass
class Ledger:
    credit_cap: int
    call_cap: int
    spent: Fraction = Fraction(0)
    calls: int = 0
    unknown_usage_calls: int = 0

    def allows(self, worst: Fraction, cap: int | None = None) -> bool:
        """spent + worst <= cap and one more call within the call cap. ``cap`` None = ``credit_cap`` (gen calls);
        the refine step passes ``Settings.refine_credit_cap``."""
        limit = self.credit_cap if cap is None else cap
        return self.calls + 1 <= self.call_cap and self.spent + worst <= limit

    def add(self, model: str, usage: CallUsage, worst: Fraction) -> Fraction:
        """Account one dispatched call: verified usage, or its worst case when the usage is unknown."""
        self.calls += 1
        if usage.known:
            cost = call_credits(model, usage.input_tokens, usage.output_tokens)
        else:
            cost = worst
            self.unknown_usage_calls += 1
        self.spent += cost
        return cost

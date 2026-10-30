"""Run a fixed multi-branch scenario (v2 front4 / grid / f2 / v1) and print one digest of every request body,
render call and returned module per witness-client surface (rc6, then 29a0d82: fakes.SURFACES). Used by
test_determinism under several PYTHONHASHSEED values; the two digests must also be equal (0.3.1: same branches)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json

from fakes import FIXTURE_REFINE_RETRY, FIXTURE_TICKET, FIXTURE_TIER_FRACTION, IMAGE, REJECT, SEED, SURFACES, FakeRenderer, FakeWitness, err_http, err_incomplete, err_stream, err_stream_cut, fenced, module, ok, script_from

from sn17gen.pipeline import run
from sn17gen.settings import make_settings


def digest(surface: str | None = None) -> str:
    h = hashlib.sha256()
    gen, ref, bad = fenced(module("336699")), fenced(module("884422")), fenced(module(marker=REJECT))
    cases = [
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), {("gen", 0): ok("no code"), ("gen", 1): ok(gen), ("refine", 0): ok(ref)}),
        (make_settings("v2", "f1", "grid", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), {("gen", 0): ok(bad), ("gen", 1): ok(gen), ("refine", 0): ok(ref)}),
        (make_settings("v2", "f2", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), {("gen", 0): err_incomplete(), ("gen", 1): ok(gen),
                                               ("refine", 0): err_stream("overloaded"), ("refine", 1): ok(ref)}),
        (make_settings("v1", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), {("gen", 0): ok(bad), ("gen", 1): ok(bad)}),
        # v2.2: gen spend + refine worst case in (3M, 4M]: refined under the 4M refine cap, skipped under 3M
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY), {("gen", 0): err_incomplete(), ("gen", 1): ok(gen, 3_800, 20_000),
                                               ("refine", 0): ok(ref)}),
        (make_settings("v2", "f1", "front4", refine_credit_cap=3_000_000, ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY),
         {("gen", 0): err_incomplete(), ("gen", 1): ok(gen, 3_800, 20_000)}),
        # the 0.2.1 service-tier paths, now at fraction 0.0 (fast tiers dormant by choice since 0.3.0)
        (make_settings("v2", "f2", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY),
         {("gen", 0): err_incomplete(), ("gen", 1): ok(gen), ("refine", 0): err_stream("overloaded"), ("refine", 1): ok(ref)}),
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=FIXTURE_REFINE_RETRY),
         {("gen", 0): ok(bad), ("gen", 1): ok(gen), ("refine", 0): ok(ref)}),
        # 0.2.2 REFINE_RETRY (v2r): stream-cut retry, 503 retry, a retry the refine cap skips, no retry after a 400
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=True),
         {("gen", 0): ok(gen), ("refine", 0): err_stream_cut(), ("refine", 1): ok(ref)}),
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=True),
         {("gen", 0): ok(bad), ("gen", 1): ok(gen), ("refine", 0): err_http(503, "server_error"), ("refine", 1): ok(ref)}),
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=True),
         {("gen", 0): err_incomplete(), ("gen", 1): ok(gen, 3_800, 20_000), ("refine", 0): err_stream_cut()}),
        (make_settings("v2", "f1", "front4", ticket=FIXTURE_TICKET, service_tier_fraction=FIXTURE_TIER_FRACTION, refine_retry=True),
         {("gen", 0): ok(gen), ("refine", 0): err_http(400)}),
    ]
    for s, table in cases:
        w, r = FakeWitness(script_from(table), surface), FakeRenderer()
        with contextlib.redirect_stderr(io.StringIO()):
            out = run(IMAGE, SEED, w, r, s)
        for b in w.bodies:
            h.update(hashlib.sha256(b).digest())
        for src, opts in r.calls:
            h.update(hashlib.sha256(src.encode() + json.dumps(opts, sort_keys=True).encode()).digest())
        h.update(hashlib.sha256((out or "<none>").encode()).digest())
    return h.hexdigest()


def digests() -> str:
    return " ".join(digest(s) for s in SURFACES)


if __name__ == "__main__":
    print(digests())

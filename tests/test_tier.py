"""Service tier: dormant by choice since 0.3.0 (C4).

404's Pod witness adds ``"service_tier":"default"`` to every body itself; it admits fast / ultrafast at Standard rates
(only flex is refused), but the Pod's concurrency removes the need and a non-zero fraction would need a re-gate. So
SERVICE_TIER_FRACTION must be 0.0: our params never carry a service_tier key, and every Pod body carries exactly the
injected default. (0.2.1-0.2.2 tested the fast-tier selection rule here; that code stays dormant at 0.0.)"""

from __future__ import annotations

import json
import unittest

from fakes import FIXTURE_REFINE_RETRY, FIXTURE_TICKET, IMAGE, SEED, pod_body
from test_requests import CODE, FIXED_PNGS

from sn17gen import settings
from sn17gen.reqbuild import build_gen_params, build_refine_params, data_url, prompt_id
from sn17gen.settings import make_settings

IMAGES = [IMAGE + bytes([k]) for k in range(64)]


def fx(*args, **kw):
    return make_settings(*args, ticket=FIXTURE_TICKET, refine_retry=FIXTURE_REFINE_RETRY, **kw)


class TestTierRetired(unittest.TestCase):
    def test_committed_fraction_is_zero(self):
        self.assertEqual(settings.SERVICE_TIER_FRACTION, 0.0)
        self.assertEqual(settings.ACTIVE.service_tier_fraction, 0.0)
        self.assertTrue(all(settings.ACTIVE.service_tier(prompt_id(i)) is None for i in IMAGES))

    def test_nonzero_fraction_is_refused(self):
        for f in (0.25, 0.5, 1.0, 1):
            with self.assertRaises(ValueError, msg=f):
                make_settings("v2", service_tier_fraction=f)
        for kw in ({"service_tier_fraction": -0.01}, {"service_tier_fraction": float("nan")},
                   {"service_tier_fraction": True}, {"service_tier_fraction": None}, {"fast_tier": "flex"}):
            with self.assertRaises(ValueError, msg=kw):
                make_settings("v2", **kw)

    def test_every_pod_body_carries_only_the_injected_default(self):
        for mode in ("v1", "v2"):
            s = fx(mode)
            for img in IMAGES[:8]:
                pid, url = prompt_id(img), data_url(img)
                params = [build_gen_params(s, pid, SEED, url, 0)]
                if mode == "v2":
                    params.append(build_refine_params(s, pid, SEED, url, CODE, FIXED_PNGS))
                for p in params:
                    self.assertNotIn("service_tier", p)
                    doc = json.loads(pod_body(p))
                    self.assertEqual(list(doc)[:2], ["tools", "service_tier"])
                    self.assertEqual((doc["service_tier"], list(doc)[-1], doc["stream"]), ("default", "stream", True))


if __name__ == "__main__":
    unittest.main()

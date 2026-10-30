"""The whole pipeline is a pure function of (image, seed, recorded responses, recorded render outcomes):
same digest of every request body, render call and output under any PYTHONHASHSEED, and (0.3.1) the same digest on
both witness-client surfaces (rc6 and 29a0d82)."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class TestHashSeed(unittest.TestCase):
    def test_scenario_digest_is_hashseed_independent(self):
        env0 = {k: v for k, v in os.environ.items() if k not in ("PYTHONHASHSEED",)}
        outs = {}
        for seed in ("0", "1", "4242", "random"):
            env = dict(env0, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, str(HERE / "scenario.py")], env=env, capture_output=True, text=True,
                               cwd=str(HERE))
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            outs[seed] = r.stdout.strip()
        self.assertEqual(len(set(outs.values())), 1, outs)
        from scenario import digests
        self.assertEqual(digests(), outs["0"])
        rc6, at29 = outs["0"].split(" ")
        self.assertEqual(rc6, at29)                   # both client surfaces: the same bodies, renders and outputs


if __name__ == "__main__":
    unittest.main()

"""Miner entry point: the sandbox supplies one prompt image, the round seed and the runtime clients.

The committed pipeline mode (v1 / v2, refine views) is set in sn17gen/settings.py.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sn17gen.pipeline import run

if TYPE_CHECKING:
    from renderer_client import Renderer
    from witness_client import WitnessClient


def generate(image: bytes, seed: int, witness: WitnessClient, renderer: Renderer) -> str | None:
    return run(image, seed, witness, renderer)

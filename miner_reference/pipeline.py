"""Adapter so the shared batch runner and dry stand can drive the sn17gen generator.

The generator never passes ``request_id`` (it relies on unique request tags); the runner's
call wrapper keys its checkpoints by request_id, so this shim numbers the calls in order
(the pipeline is sequential, hence deterministic).
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, Unpack

from openai.types.responses import Response
from openai.types.responses.response_create_params import ResponseCreateParamsBase
from renderer_client import Renderer

from sn17gen import pipeline as _generator

_ROOT = Path(__file__).resolve().parents[1]
INSTRUCTIONS = "sn17gen:" + hashlib.sha256(
    b"".join(p.read_bytes() for p in sorted((_ROOT / "sn17gen").glob("*.py")))
).hexdigest()


class ModelCalls(Protocol):
    def create(self, *, request_id: int = 0, **params: Unpack[ResponseCreateParamsBase]) -> Response: ...


class _Numbered:
    def __init__(self, calls: ModelCalls) -> None:
        self._calls, self._n, self._lock = calls, 0, threading.Lock()

    def create(self, *, request_id: int | None = None, **params):
        with self._lock:
            self._n += 1
            n = self._n
        return self._calls.create(request_id=n if request_id is None else request_id, **params)


def run(image: bytes, seed: int, calls: ModelCalls, renderer: Renderer) -> str | None:
    witness = SimpleNamespace(responses=_Numbered(calls))
    return _generator.run(image, seed, witness, renderer)

"""Test doubles for the runtime clients (no network, no renderer).

FakeWitness models BOTH published witness-client surfaces (0.3.1):
  "rc6"     = 44d2a64 / 92bedb6: create(stream: Literal[True] = True) returns a WitnessResult; every recorded provider
              failure is raised as a provider WitnessError (client.recorded_result);
  "29a0d82" = create(stream=False) returns response.model_result(status, body): an OpenAI Response (status completed /
              failed / incomplete), a ModelStream when stream=True is passed, SDK APIStatusError subclasses for HTTP
              != 200, openai.APIError ``from`` the provider WitnessError for a stream ``error`` event, and the
              decoder's bare ValueError for an undecodable HTTP-200 record (a stream cut, a broken frame, ...).
Every scripted provider outcome (ok / err_* below) carries its RECORDED exchange (HTTP status + response bytes) as
``fake_exchange``; FakeWitness decodes those bytes with the chosen surface's own decoder on every call:
  - the real one where the witness_client on sys.path is that generation (rc6 sites: client.recorded_result; the
    29a0d82 site: response.model_result);
  - otherwise a line-for-line port of it onto the bundled decoder pieces (``rc6_outcome`` / ``model_result_29a0d82``).
The returned objects of ok() / err_*() are the rc6 decode of the same bytes (built by the bundled decoder, as before),
so classifier tests can inspect them directly. Outcomes without recorded bytes (client / gateway failures, a
hand-built WitnessError) are raised as they are on both surfaces. FakeWitness keeps every create() keyword argument as
received (``received``), the request as the client normalizes and serializes it (``params``, ``bodies``: both
generations write {"service_tier": "default", **params, "stream": true}), and the type of what create() returned or
raised (``delivered``). DEFAULT_SURFACE is 29a0d82 (404's current head); tests/test_client_surfaces.py re-runs the
flow tests on rc6 and checks that both surfaces take the same branches.
FakeRenderer: deterministic PNG-signed bytes per (source, RenderOptions.query()), a real-format RenderRejected (the
service's 422 JSON) for sources that carry a marker.
Run inside the runtime image (or any env with the bundled clients on sys.path).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

import httpx
import openai
from renderer_client import RenderRejected
from witness_client import WitnessError
from witness_client import client as _wc_client
from witness_client import response as _wc_response
from witness_client.request import serialize_body
from witness_client.response import completed_response, provider_error, sse_events

PNG = b"\x89PNG\r\n\x1a\n"
IMAGE = PNG + b"\x00\x00\x00\rIHDR" + bytes(range(256)) * 40          # fixed fake prompt bytes
SEED = 3560810081
FIXTURE_TICKET = "t0"   # frozen fixtures use ticket t0; the committed settings.TICKET is the hotkey label
FIXTURE_TIER_FRACTION = 0.0   # frozen fixtures send no service_tier; the committed SERVICE_TIER_FRACTION may differ
FIXTURE_REFINE_RETRY = False   # frozen fixtures = 0.2.1 refine behaviour; the committed REFINE_RETRY may differ
REJECT = "REJECT_ME"

SURFACE_RC6 = "rc6"            # witness-client 0.2.13rc6 at 44d2a64 / 92bedb6
SURFACE_29 = "29a0d82"         # witness-client at 404 29a0d82 (same version string, new create() surface)
SURFACES = (SURFACE_RC6, SURFACE_29)
DEFAULT_SURFACE = SURFACE_29
# the real decoders, where the witness_client on sys.path is that generation
REAL_RC6_DECODER = getattr(_wc_client, "recorded_result", None)        # rc6 sites only
REAL_29_DECODER = getattr(_wc_response, "model_result", None)          # 29a0d82 site only
EXCHANGE_ID = "00000000-0000-4000-8000-000000000000"
_PROVIDER_REQUEST = httpx.Request("POST", "https://api.openai.com/v1/responses")


def module(colour: str = "336699", marker: str = "") -> str:
    extra = f'  const tag = "{marker}";\n' if marker else ""
    return (
        "export default function generate(THREE) {\n"
        f"{extra}"
        "  const g = new THREE.Group();\n"
        f"  g.add(new THREE.Mesh(new THREE.BoxGeometry(1, 2, 1), new THREE.MeshStandardMaterial({{ color: 0x{colour} }})));\n"
        "  return g;\n"
        "}\n"
    )


def fenced(js: str, prefix: str = "") -> str:
    return f"{prefix}```javascript\n{js}```\n"


@dataclass
class Result:
    """rc6's WitnessResult shape (output_text, usage dict, response) plus the recorded exchange it decodes from."""
    output_text: str
    usage: dict[str, Any]
    response: Any = None
    hotkey: str = "hk"
    _exchange_id: str = "x"
    fake_exchange: tuple[int, bytes] | None = field(default=None, repr=False)


def usage(it: int, ot: int) -> dict[str, Any]:
    return {"input_tokens": it, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": ot,
            "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": it + ot}


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _response(status: str, it: int, ot: int, reason: str | None = None) -> dict[str, Any]:
    return {"id": "resp_x", "object": "response", "created_at": 1, "status": status, "model": "gpt-6-astra",
            "output": [], "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            "incomplete_details": {"reason": reason} if reason else None,
            "error": {"code": "server_error", "message": "x"} if status == "failed" else None,
            "usage": usage(it, ot)}


def _message(text: str, status: str) -> dict[str, Any]:
    return {"type": "message", "id": "msg_x", "role": "assistant", "status": status,
            "content": [{"type": "output_text", "text": text, "annotations": []}]}


def _with_exchange(obj: Any, status: int, raw: bytes) -> Any:
    obj.fake_exchange = (status, raw)
    return obj


def exchange_of(act: Any) -> tuple[int, bytes] | None:
    """The recorded (HTTP status, response bytes) a scripted outcome decodes from (None: not a recorded exchange)."""
    return getattr(act, "fake_exchange", None)


def completed_sse(text: str, it: int = 3_800, ot: int = 5_000) -> bytes:
    resp = _response("completed", it, ot)
    resp["output"] = [_message(text, "completed")]
    return _sse([{"type": "response.completed", "response": resp}])


def decoded_error(raw: bytes) -> BaseException:
    try:
        completed_response(raw)
    except Exception as e:  # noqa: BLE001
        return _with_exchange(e, 200, raw)
    raise AssertionError("stream decoded without an error")


def err_incomplete(it: int = 3_800, ot: int = 24_000, reason: str = "max_output_tokens",
                   partial: str | None = None) -> BaseException:
    """response.incomplete; ``partial`` = the text the model wrote before the stop (an incomplete message item)."""
    resp = _response("incomplete", it, ot, reason)
    if partial is not None:
        resp["output"] = [_message(partial, "incomplete")]
    return decoded_error(_sse([{"type": "response.incomplete", "response": resp}]))


def err_failed(it: int = 9_000, ot: int = 3_000) -> BaseException:
    return decoded_error(_sse([{"type": "response.failed", "response": _response("failed", it, ot)}]))


def err_stream(kind: str) -> BaseException:
    inner = {"overloaded": {"type": "service_unavailable_error", "code": "server_is_overloaded", "message": "busy"},
             "quota": {"type": "insufficient_quota", "code": "credit_balance_exhausted", "message": "no credit"}}[kind]
    return decoded_error(_sse([{"type": "error", "sequence_number": 1, "error": inner}]))


def err_http(status: int, code: str = "invalid_request_error") -> BaseException:
    return err_http_body(status, json.dumps({"error": {"message": "x", "type": code, "code": code}}).encode())


def err_http_body(status: int, body: bytes) -> BaseException:
    """HTTP ``status`` with any recorded body (rc6: provider_error(status, body))."""
    return _with_exchange(provider_error(status, body), status, body)


def _invalid(raw: bytes) -> BaseException:
    """What the rc6 witness client raises for an HTTP-200 record its pinned decoder cannot read: WitnessError
    invalid_provider_response chained ``from`` the decoder's ValueError (rc4-rc6 create/resume)."""
    try:
        completed_response(raw)
    except ValueError as error:
        try:
            raise WitnessError("Invalid recorded OpenAI response", "x", source="provider",
                               code="invalid_provider_response") from error
        except WitnessError as wrapped:
            return _with_exchange(wrapped, 200, raw)
    raise AssertionError("record decoded without a ValueError")


def f3_stream() -> bytes:
    """VPS_HOST.md F3's shape (sven_252 fe7fb23acf, exchange 94a33dc2, 10-23): created, in_progress, reasoning items,
    then the stream ends (no response.completed / .failed / .incomplete)."""
    resp = _response("in_progress", 0, 0)
    resp["usage"] = None
    item = {"id": "rs_x", "type": "reasoning", "summary": [], "encrypted_content": "gAAAA"}
    return _sse([{"type": "response.created", "sequence_number": 0, "response": resp},
                 {"type": "response.in_progress", "sequence_number": 1, "response": resp},
                 {"type": "response.output_item.added", "sequence_number": 2, "output_index": 0, "item": item},
                 {"type": "response.output_item.done", "sequence_number": 3, "output_index": 0, "item": item},
                 {"type": "response.output_item.added", "sequence_number": 4, "output_index": 1, "item": item}])


def err_stream_cut() -> BaseException:
    """F3: an HTTP-200 stream cut without a terminal event (pinned decoder: "Missing completed OpenAI response")."""
    return _invalid(f3_stream())


def err_stream_cut_mid_text(text: str = "```javascript\nexport default") -> BaseException:
    """A stream cut after some output text deltas (still no terminal event): the same decoder outcome as F3."""
    return _invalid(_sse([{"type": "response.created", "response": _response("in_progress", 0, 0)},
                          {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": text}]))


def err_invalid(kind: str) -> BaseException:
    """Other undecodable HTTP-200 records (NOT stream cuts): a truncated JSON frame, inconsistent text, a duplicate
    terminal event, bad UTF-8."""
    done = _sse([{"type": "response.completed", "response": _response("completed", 10, 10)}])
    raw = {"partial_frame": f3_stream() + b'event: response.output_item.done\ndata: {"type": "response.output_item.do',
           "inconsistent": _sse([{"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "a"},
                                 {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "b"},
                                 {"type": "response.completed", "response": _response("completed", 10, 10)}]),
           "two_terminals": done + done,
           "bad_utf8": f3_stream() + b"data: \xff\xfe\n\n"}[kind]
    return _invalid(raw)


def err_transport() -> BaseException:
    """A client-side failure with nothing decoded (the runtime owns these: recovery / unresolved)."""
    return WitnessError("Exchange x failed; inference was not retried: ReadError", "x")


def err_gateway(status: int = 503) -> BaseException:
    return WitnessError("gateway busy", None, source="gateway", status_code=status, code="busy")


Action = Any  # Result | BaseException


# ------------------------------------------------------------------------------------------- the two decoders
def rc6_outcome(status: int, raw: bytes) -> Any:
    """rc6's client.recorded_result for one recorded exchange: a WitnessResult, or the provider WitnessError it raises
    (returned, not raised). The real function on rc6 sites, else this port of it (same decoder pieces)."""
    try:
        if REAL_RC6_DECODER is not None:
            return REAL_RC6_DECODER("hk", EXCHANGE_ID, status, raw)
        if status != 200:
            raise provider_error(status, raw)
        try:
            output, completed = completed_response(raw)
        except ValueError as error:
            raise WitnessError("Invalid recorded OpenAI response", EXCHANGE_ID, source="provider",
                               code="invalid_provider_response") from error
        return Result(output, completed.usage.model_dump(exclude_unset=True) if completed.usage else {},
                      response=completed, _exchange_id=EXCHANGE_ID)
    except WitnessError as error:
        error.exchange_id = EXCHANGE_ID
        return error


_STATUS_ERRORS: dict[int, type[openai.APIStatusError]] = {
    400: openai.BadRequestError, 401: openai.AuthenticationError, 403: openai.PermissionDeniedError,
    404: openai.NotFoundError, 409: openai.ConflictError, 422: openai.UnprocessableEntityError,
    429: openai.RateLimitError}


class ModelStream(Iterator[Any]):
    """Stand-in for 29a0d82's response.ModelStream on rc6 sites: the decoded SSE events, no output_text, no usage."""

    def __init__(self, raw: bytes) -> None:
        self.events = iter(sse_events(raw))

    def __iter__(self) -> "ModelStream":
        return self

    def __next__(self) -> Any:
        return next(self.events)


def model_result_29a0d82(status: int, raw: bytes, stream: bool = False) -> Any:
    """What 29a0d82's create() returns for one recorded exchange, or raises (it raises, like the client). The real
    response.model_result on the 29a0d82 site; on rc6 sites a port of it onto the rc6 decoder, which raises for failed
    / incomplete where 29a0d82 passes raise_failed=False (the port returns that response). Not ported: 29a0d82's
    rebuild of message items for a terminal event with an empty ``output`` (codex subscription completions)."""
    if REAL_29_DECODER is not None:
        return REAL_29_DECODER(status, raw, "api", stream=stream)
    if status != 200:
        try:
            value = json.loads(raw)
        except ValueError:
            value = None
        details = value.get("error", value) if isinstance(value, dict) else value
        error_type = _STATUS_ERRORS.get(status, openai.InternalServerError if status >= 500 else openai.APIStatusError)
        raise error_type(f"OpenAI returned HTTP {status}",
                         response=httpx.Response(status, content=raw, request=_PROVIDER_REQUEST), body=details)
    if stream:
        return ModelStream(raw)
    try:
        return completed_response(raw)[1]
    except WitnessError as error:
        if error.response is not None:
            return error.response
        raise openai.APIError(error.provider_message or str(error), _PROVIDER_REQUEST,
                              body={"code": error.provider_code, "param": error.param}) from error


def pod_body(params: dict[str, Any]) -> bytes:
    """The request body 404's Pod witness records for ``witness.responses.create(**params)`` (rc6 client.py:374-376,
    replay.py:80; 29a0d82 client.py _request): stream moves to the end (rc6: create()'s own argument, default True;
    29a0d82: always true); service_tier "default" goes first."""
    p = dict(params)
    stream = p.pop("stream", True)
    return serialize_body({"service_tier": "default", **p, "stream": stream}, "api")


class FakeWitness:
    """``script(step, attempt, params) -> Result | BaseException``, answered on one client ``surface`` (None =
    DEFAULT_SURFACE at construction)."""

    def __init__(self, script: Callable[[str, int, dict[str, Any]], Action], surface: str | None = None) -> None:
        self.script = script
        self.surface = DEFAULT_SURFACE if surface is None else surface
        if self.surface not in SURFACES:
            raise ValueError(f"unknown surface {self.surface!r}")
        self.responses = self
        self.bodies: list[bytes] = []
        self.params: list[dict[str, Any]] = []
        self.steps: list[tuple[str, int]] = []
        self.received: list[dict[str, Any]] = []       # create()'s keyword arguments, exactly as received
        self.delivered: list[str] = []                 # type name of what create() returned or raised

    def create(self, **kwargs: Any) -> Any:
        self.received.append(dict(kwargs))
        stream = kwargs.pop("stream", None)
        kwargs.pop("request_id", None)
        kwargs.pop("session_id", None)
        written = stream if (self.surface == SURFACE_RC6 and stream is not None) else True
        params = {**kwargs, "stream": written}
        self.bodies.append(pod_body(params))
        self.params.append(params)
        text0 = params["input"][0]["content"][0]["text"]
        step = text0.split(" step=", 1)[1].split(" ", 1)[0]
        attempt = int(text0.split(" attempt=", 1)[1].split("]", 1)[0])
        self.steps.append((step, attempt))
        act = self.script(step, attempt, params)
        try:
            out = self._deliver(act, stream is True)
        except BaseException as e:
            self.delivered.append(type(e).__name__)
            raise
        self.delivered.append(type(out).__name__)
        return out

    def _deliver(self, act: Action, stream: bool) -> Any:
        exchange = exchange_of(act)
        if exchange is None:
            if isinstance(act, BaseException):
                raise act
            if self.surface == SURFACE_29:
                raise AssertionError("a reply on the 29a0d82 surface needs its recorded exchange (use ok())")
            return act
        if self.surface == SURFACE_29:
            return model_result_29a0d82(*exchange, stream=stream)
        out = rc6_outcome(*exchange)
        if isinstance(out, BaseException):
            raise out
        return out


class FakeRenderer:
    def __init__(self, reject: Callable[[str, dict[str, Any] | None], str | None] | None = None) -> None:
        self.reject = reject or (lambda src, opts: "BOUNDING_BOX_OUT_OF_RANGE" if REJECT in src else None)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def render(self, source: str, options: Any = None) -> bytes:
        norm = None if options is None else options.query()
        self.calls.append((source, norm))
        rule = self.reject(source, norm)
        if rule:
            raise RenderRejected(json.dumps({"error": "fake rejection", "failures": [
                {"stage": "post_validation", "rule": rule, "detail": "fake"}], "truncated": False}))
        h = hashlib.sha256(source.encode() + json.dumps(norm, sort_keys=True).encode()).digest()
        return PNG + h * 8


def ok(text: str, it: int = 3_800, ot: int = 5_000) -> Result:
    return Result(text, usage(it, ot), fake_exchange=(200, completed_sse(text, it, ot)))


def ok_raw(raw: bytes) -> Result:
    """A reply recorded as the HTTP-200 bytes ``raw`` (for records ok() cannot build), as rc6 decodes them."""
    got = rc6_outcome(200, raw)
    if isinstance(got, BaseException):
        raise AssertionError(f"rc6 does not decode this record as a reply: {got!r}")
    return Result(got.output_text, dict(got.usage), response=got.response, fake_exchange=(200, raw))


def completed_without_output() -> Result:
    """A response.completed terminal event whose response has no ``output`` key (review F2, 10-26): rc6 reads ""; on
    29a0d82 the SDK's Response.output_text raises TypeError (pipeline.reply_text reads "")."""
    return ok_raw(_sse([{"type": "response.completed", "response": {"status": "completed"}}]))


def script_from(table: dict[tuple[str, int], Action]) -> Callable[[str, int, dict[str, Any]], Action]:
    def f(step: str, attempt: int, params: dict[str, Any]) -> Action:
        return table[(step, attempt)]
    return f

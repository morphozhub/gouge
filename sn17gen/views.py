"""The only module that talks to the runtime's ``renderer_client`` (404's published client, 44d2a64).

Calls made (each one recorded by the runtime and reproduced + verified in replay):
  check(js)            ``renderer.render(js)``: the runtime's default four-view preview grid. Used as the validity
                       check of a candidate in mode v1, of every module in grid mode, and of the fallback.
  front4(js)           four ``renderer.render(js, RenderOptions(thetas=(t,), phis=(p,), ...))`` calls: one angle pair
                       = a single-view PNG. The judge's Stage-1 front views in this fixed order, 512 px, neutral light,
                       white background (sn17miner rv1). Byte-identical to 0.2.2's emulator "view" renders for an
                       opaque framebuffer (10-25: 12/12 PNGs, render-service-js 0.4.10; alpha < 1 can differ).
  check_front(js)      the first front4 view only: validity check of a refined module in front4 mode.
A structured ``RenderRejected`` (the service's HTTP 422: production validation failure) propagates to the caller,
which decides on it; it is a recorded outcome that replay reproduces. Anything else the renderer raises
(``RenderUnavailable``: transport, 503, protocol) is an infrastructure failure owned by the runtime and is not caught
here.
"""

from __future__ import annotations

from typing import Any

from renderer_client import RenderOptions, RenderRejected  # noqa: F401  (re-exported for the pipeline)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# (name, theta, phi) in degrees: judge Stage-1 order (shared/subnet_common/render.py, multi_stage.S1_ANGLES)
FRONT4_VIEWS: tuple[tuple[str, int, int], ...] = (
    ("front_left", 30, 0),
    ("front_right", 330, 0),
    ("front_below", 0, 15),
    ("front_above", 0, -30),
)
FRONT4_SIZE = 512
FRONT4_LIGHTING = "neutral"
FRONT4_BACKGROUND = "ffffff"
FRONT4_CAM_RADIUS = 2.0         # the judge's camera (= RenderOptions' defaults, spelled out so a default change in
FRONT4_CAM_FOV_DEG = 49.1       # renderer_client cannot move our views)


def front4_options() -> list[Any]:
    return [RenderOptions(thetas=(float(theta),), phis=(float(phi),), img_size=FRONT4_SIZE,
                          cam_radius=FRONT4_CAM_RADIUS, cam_fov_deg=FRONT4_CAM_FOV_DEG, bg_color=FRONT4_BACKGROUND,
                          lighting=FRONT4_LIGHTING)
            for _, theta, phi in FRONT4_VIEWS]


def rejection_info(r: Any) -> tuple[str | None, str | None]:
    """(stage, rule) of a RenderRejected, for diagnostics only (never an input to a request or a branch). 404's
    client carries the service's 422 JSON ({"error", "failures": [{"stage", "rule", "detail"}], ...}) as
    ``details``; the first failure names it. Never raises."""
    try:
        d = r.details
        f = d.get("failures") if isinstance(d, dict) else None
        first = f[0] if isinstance(f, list) and f and isinstance(f[0], dict) else {}
        stage, rule = first.get("stage"), first.get("rule")
        if not isinstance(rule, str) and isinstance(d, dict) and isinstance(d.get("error"), str):
            rule = d["error"][:80]
        return (stage if isinstance(stage, str) else None), (rule if isinstance(rule, str) else None)
    except Exception:  # noqa: BLE001
        return None, None


def _png(data: Any) -> bytes:
    data = bytes(data)
    if not data.startswith(PNG_SIGNATURE):
        raise RuntimeError("renderer returned bytes that are not a PNG")
    return data


def check(renderer: Any, js: str) -> bytes:
    return _png(renderer.render(js))


def front4(renderer: Any, js: str) -> list[bytes]:
    return [_png(renderer.render(js, opt)) for opt in front4_options()]


def check_front(renderer: Any, js: str) -> bytes:
    return _png(renderer.render(js, front4_options()[0]))


def previews(renderer: Any, js: str, views_mode: str) -> list[bytes]:
    """The refine images of the gen module (also its validity check)."""
    if views_mode == "front4":
        return front4(renderer, js)
    if views_mode == "grid":
        return [check(renderer, js)]
    raise ValueError(f"unknown refine view mode {views_mode!r}")


def check_refined(renderer: Any, js: str, views_mode: str) -> bytes:
    if views_mode == "front4":
        return check_front(renderer, js)
    if views_mode == "grid":
        return check(renderer, js)
    raise ValueError(f"unknown refine view mode {views_mode!r}")

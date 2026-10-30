"""Colour check for the refine step: a numeric comparison of the photo's object colours with the colours of the
judge-view renders of the current module (foreground only), turned into a short measured note for the refine prompt
when the render is clearly darker, lighter or of a different dominant colour than the photo.

Pure function of the bytes (PIL decode + integer numpy statistics, fixed order, no randomness, no clock), so the
same photo and the same renders give the same text at replay. No network, no calls: zero extra credits.
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image

__all__ = ["ColourStats", "measure", "colour_check", "DARK_GAP", "LIGHT_GAP", "HUE_GAP"]

DARK_GAP = 0.12     # render mean lightness below the photo's by more than this -> "too dark"
LIGHT_GAP = 0.15    # render mean lightness above the photo's by more than this -> "too light"
HUE_GAP = 100.0     # RGB distance between the dominant colours above this (and no lightness flag) -> "wrong colour"
_BORDER = 6         # photo: background colour = median of this frame (pixels)
_PHOTO_DIFF = 30    # photo: a pixel is foreground when any channel differs from the background by more than this
_RENDER_WHITE = 248 # render: a pixel is background when every channel is at least this (white bg + antialias)
_MIN_PIXELS = 400   # fewer foreground pixels -> no measurement (no note)
_TOP = 3


class ColourStats:
    __slots__ = ("lightness", "dominant", "pixels")

    def __init__(self, lightness: float, dominant: list[tuple[str, float]], pixels: int) -> None:
        self.lightness = lightness      # mean perceptual lightness of the foreground, 0..1
        self.dominant = dominant        # [(hex, share), ...] top bins, share of the foreground
        self.pixels = pixels


def _rgb(data: bytes, max_side: int = 384) -> np.ndarray:
    im = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = im.size
    scale = max(w, h) / float(max_side)
    if scale > 1.0:
        im = im.resize((max(1, int(w / scale)), max(1, int(h / scale))), Image.BILINEAR)
    return np.asarray(im, dtype=np.int32)


def _foreground_photo(a: np.ndarray) -> np.ndarray:
    b = _BORDER
    frame = np.concatenate([a[:b].reshape(-1, 3), a[-b:].reshape(-1, 3), a[:, :b].reshape(-1, 3), a[:, -b:].reshape(-1, 3)])
    bg = np.median(frame, axis=0)
    return (np.abs(a - bg).max(axis=2) > _PHOTO_DIFF)


def _foreground_render(a: np.ndarray) -> np.ndarray:
    return a.min(axis=2) < _RENDER_WHITE


def _stats(pixels: np.ndarray) -> ColourStats:
    n = int(pixels.shape[0])
    lum = (pixels[:, 0] * 2126 + pixels[:, 1] * 7152 + pixels[:, 2] * 722) / (10000.0 * 255.0)
    light = float(lum.mean())
    q = pixels // 32                                     # 8 levels per channel = 512 bins
    key = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    counts = np.bincount(key, minlength=512)
    order = np.argsort(-counts, kind="stable")
    dom: list[tuple[str, float]] = []
    for k in order[:_TOP]:
        c = int(counts[k])
        if c == 0:
            break
        sel = pixels[key == k]
        mean = sel.mean(axis=0)
        dom.append(("#%02x%02x%02x" % (int(mean[0]), int(mean[1]), int(mean[2])), c / n))
    return ColourStats(light, dom, n)


def measure(data: bytes, kind: str) -> ColourStats | None:
    """``kind`` "photo" (studio background estimated from the frame) or "render" (white background)."""
    a = _rgb(data)
    mask = _foreground_photo(a) if kind == "photo" else _foreground_render(a)
    px = a[mask]
    if px.shape[0] < _MIN_PIXELS:
        return None
    return _stats(px)


def _measure_renders(renders: list[bytes]) -> ColourStats | None:
    parts = []
    for r in renders:
        a = _rgb(r)
        px = a[_foreground_render(a)]
        if px.shape[0]:
            parts.append(px)
    if not parts:
        return None
    px = np.concatenate(parts)
    if px.shape[0] < _MIN_PIXELS:
        return None
    return _stats(px)


def _hex_rgb(h: str) -> np.ndarray:
    return np.array([int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)], dtype=np.float64)


def _fmt(dom: list[tuple[str, float]]) -> str:
    return ", ".join(f"{h} {int(round(s * 100))}%" for h, s in dom)


def colour_check(photo: bytes, renders: list[bytes]) -> tuple[str | None, dict]:
    """(note or None, measurements). The note is inserted into the refine prompt only when a flag fires. Any decode
    or measurement failure gives no note (recorded in the measurements), never an exception."""
    info: dict = {}
    try:
        p = measure(photo, "photo")
        r = _measure_renders(renders)
    except Exception as e:  # noqa: BLE001  (undecodable bytes: no note, same at replay)
        info["skipped"] = f"error:{type(e).__name__}"
        return None, info
    if p is None or r is None:
        info["skipped"] = "no foreground"
        return None, info
    gap = r.lightness - p.lightness
    hue = float(np.sqrt(((_hex_rgb(p.dominant[0][0]) - _hex_rgb(r.dominant[0][0])) ** 2).sum())) if p.dominant and r.dominant else 0.0
    info.update(photo_l=round(p.lightness, 3), render_l=round(r.lightness, 3), gap=round(gap, 3), hue=round(hue, 1),
                photo_dom=_fmt(p.dominant), render_dom=_fmt(r.dominant))
    if gap < -DARK_GAP:
        verdict = f"your model renders much DARKER than the photo (mean lightness {r.lightness:.2f} against {p.lightness:.2f})"
        fix = ("Treat this as the first discrepancy. Brightness must come from the colour, not from metalness: under this "
               "neutral light metalness darkens a surface. Polished steel, chrome, silver or aluminium: color #c8c8c8 to "
               "#dcdcdc, metalness 0.35-0.5, roughness 0.2-0.35. Dark fabric, rubber or dark plastic: #2a2a2a to #4a4a4a with "
               "roughness 0.8-0.95 and a subtle DataTexture, never #000000 to #141414 unless the photo is truly black. "
               "Set every base colour to the photo's hex of that part.")
        flag = "dark"
    elif gap > LIGHT_GAP:
        verdict = f"your model renders much LIGHTER than the photo (mean lightness {r.lightness:.2f} against {p.lightness:.2f})"
        fix = ("Treat this as the first discrepancy: darken the base colours to the photo's hexes, keep white parts at most "
               "#e6e6e6 with visible darker seams or trim, and avoid washed-out transparency.")
        flag = "light"
    else:   # dominant-bin hue distance is too noisy on multi-colour objects to act on (checked on 40 round-0 models)
        info["flag"] = None
        return None, info
    info["flag"] = flag
    note = (f"COLOUR CHECK (measured on the object pixels of the renders above against the photo; the white background "
            f"and the neutral light are excluded): {verdict}. Dominant photo colours: {_fmt(p.dominant)}. Dominant colours "
            f"of your renders: {_fmt(r.dominant)}. {fix}")
    return note, info

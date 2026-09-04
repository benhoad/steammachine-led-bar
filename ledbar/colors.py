"""Small colour helpers.

Colours inside the renderer are tuples of three floats in the range 0..1 so
that blending, dimming and gamma correction stay simple.  They are only
converted to 0..255 integers at the very end, right before they are handed
to an output backend.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

RGB = tuple[float, float, float]
RGB8 = tuple[int, int, int]

BLACK: RGB = (0.0, 0.0, 0.0)
WHITE: RGB = (1.0, 1.0, 1.0)

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
_SHORT_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{3})$")

# Every permutation of the three channels, used by the ``color_order``
# setting to work around strips/controllers that expect a different order.
COLOR_ORDERS = {
    "RGB": (0, 1, 2),
    "RBG": (0, 2, 1),
    "GRB": (1, 0, 2),
    "GBR": (1, 2, 0),
    "BRG": (2, 0, 1),
    "BGR": (2, 1, 0),
}


def parse_hex(value: str) -> RGB:
    """Parse ``"#1a9fff"`` (or ``"1a9fff"``, or ``"#19f"``) into floats."""
    if not isinstance(value, str):
        raise ValueError(f"colour must be a string like '#1a9fff', got {value!r}")
    text = value.strip()
    match = _HEX_RE.match(text)
    if match:
        digits = match.group(1)
        return tuple(int(digits[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    match = _SHORT_HEX_RE.match(text)
    if match:
        digits = match.group(1)
        return tuple(int(ch * 2, 16) / 255.0 for ch in digits)  # type: ignore[return-value]
    raise ValueError(f"invalid colour {value!r}; expected a hex colour like '#1a9fff'")


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def scale(color: RGB, factor: float) -> RGB:
    """Multiply a colour by ``factor`` (used for dimming and breathing)."""
    factor = clamp01(factor)
    return (color[0] * factor, color[1] * factor, color[2] * factor)


def lerp(a: RGB, b: RGB, t: float) -> RGB:
    """Linear interpolation between two colours; ``t`` is clamped to 0..1."""
    t = clamp01(t)
    return (
        a[0] + (b[0] - a[0]) * t,
        a[1] + (b[1] - a[1]) * t,
        a[2] + (b[2] - a[2]) * t,
    )


def blend_frames(a: Sequence[RGB], b: Sequence[RGB], t: float) -> list[RGB]:
    """Interpolate two frames LED by LED (both must have the same length)."""
    return [lerp(x, y, t) for x, y in zip(a, b)]


def to_rgb8(color: RGB, gamma: float = 1.0, brightness: float = 1.0) -> RGB8:
    """Convert floats to 0..255 ints applying brightness then gamma.

    Brightness is applied *before* gamma so that ``brightness = 0.5`` really
    looks half as bright to the eye rather than being crushed to a flicker.
    """
    out = []
    for channel in color:
        value = clamp01(channel) * clamp01(brightness)
        if gamma and gamma != 1.0:
            value = value ** gamma
        out.append(int(round(value * 255.0)))
    return (out[0], out[1], out[2])


def reorder(color: RGB8, order: str) -> RGB8:
    """Swap channels according to a colour order string such as ``"GRB"``."""
    perm = COLOR_ORDERS[order.upper()]
    return (color[perm[0]], color[perm[1]], color[perm[2]])


def is_dark(frame: Iterable[RGB8]) -> bool:
    return all(c == (0, 0, 0) for c in frame)

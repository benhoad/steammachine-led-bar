"""Turns an abstract *scene* into per-LED colours.

A scene describes *what* the bar should show ("breathe blue", "fill 43 %",
"second quadrant breathing red"); this module works out the colour of every
LED for a given point in time, smooths transitions between scenes, and
finally maps the logical left-to-right bar onto the physical LED chain
(direction, offset, brightness, gamma, colour order).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional, Sequence

from .colors import BLACK, RGB, RGB8, blend_frames, clamp01, lerp, reorder, scale, to_rgb8

# Named segments of the bar, as fractions of its length.  These mirror the
# Steam Machine error patterns (quadrants and halves).
SEGMENTS: dict[str, tuple[float, float]] = {
    "full": (0.0, 1.0),
    "left_half": (0.0, 0.5),
    "right_half": (0.5, 1.0),
    "q1": (0.0, 0.25),
    "q2": (0.25, 0.5),
    "q3": (0.5, 0.75),
    "q4": (0.75, 1.0),
}


@dataclass(frozen=True)
class Scene:
    """A description of what the bar should display right now."""

    kind: str = "off"                 # off | solid | breathe | blink | fill | segment_breathe | gauge | pulse
    name: str = "off"                 # stable identifier; a change triggers a crossfade
    color: RGB = BLACK
    level: float = 1.0                # extra brightness multiplier 0..1
    fraction: Optional[float] = None  # fill/gauge amount 0..1
    segment: str = "full"             # for segment_breathe
    period: float = 3.0               # seconds per breathe/blink cycle
    floor: float = 0.06               # minimum brightness of a breathe cycle
    head_glow: bool = True
    color2: Optional[RGB] = None      # gauge "hot" colour
    track: RGB = BLACK                # unfilled part of a fill
    started: float = 0.0              # monotonic time the scene became active (set by compositor)
    duration: float = 0.0             # for one-shot scenes such as pulse

    def with_fraction(self, fraction: Optional[float]) -> "Scene":
        return replace(self, fraction=fraction)


OFF = Scene()


# --------------------------------------------------------------------------
# Envelope helpers
# --------------------------------------------------------------------------


def breathe_level(t: float, period: float, floor: float) -> float:
    """Smooth 0..1 breathing curve (cosine) with a minimum of ``floor``."""
    if period <= 0:
        return 1.0
    phase = (t % period) / period
    raw = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
    # ease the curve so it lingers a little at the bottom, like a real breath
    raw = raw * raw * (3.0 - 2.0 * raw)
    return floor + (1.0 - floor) * raw


def blink_level(t: float, period: float) -> float:
    if period <= 0:
        return 1.0
    return 1.0 if (t % period) < period / 2.0 else 0.0


def pulse_level(elapsed: float, duration: float) -> float:
    """One-shot flash: rises quickly, then decays back to 1.0 by ``duration``."""
    if duration <= 0 or elapsed >= duration:
        return 1.0
    x = elapsed / duration
    if x < 0.15:
        boost = x / 0.15
    else:
        boost = 1.0 - (x - 0.15) / 0.85
    return 1.0 + 0.8 * boost   # the shaper clamps overshoot per channel


# --------------------------------------------------------------------------
# Pattern rendering
# --------------------------------------------------------------------------


def segment_mask(n: int, start: float, end: float) -> list[bool]:
    return [start <= (i + 0.5) / n < end for i in range(n)]


def fill_frame(n: int, fraction: float, color: RGB, track: RGB = BLACK, head_glow: bool = True) -> list[RGB]:
    """Progress bar: the first ``fraction`` of the bar lit from the left."""
    fraction = clamp01(fraction)
    lit = fraction * n
    if fraction > 0.0 and lit < 0.15:
        lit = 0.15   # always show a hint of progress once something has started
    frame: list[RGB] = []
    for i in range(n):
        if i + 1 <= lit:
            frame.append(color)
        elif i < lit:
            partial = lit - i
            if not head_glow:
                frame.append(color if partial >= 0.5 else track)
            else:
                frame.append(lerp(track, color, partial))
        else:
            frame.append(track)
    return frame


def render_scene(scene: Scene, n: int, now: float) -> list[RGB]:
    """Compute the logical (left to right) frame for ``scene`` at time ``now``."""
    kind = scene.kind
    level = clamp01(scene.level)
    if kind == "off" or level <= 0.0:
        return [BLACK] * n
    if kind == "solid":
        return [scale(scene.color, level)] * n
    if kind == "breathe":
        amount = breathe_level(now, scene.period, scene.floor) * level
        return [scale(scene.color, amount)] * n
    if kind == "blink":
        amount = blink_level(now, scene.period) * level
        return [scale(scene.color, amount)] * n
    if kind == "pulse":
        amount = pulse_level(now - scene.started, scene.duration) * level
        return [scale_over(scene.color, amount)] * n
    if kind == "fill":
        fraction = scene.fraction if scene.fraction is not None else 0.0
        frame = fill_frame(n, fraction, scene.color, scene.track, scene.head_glow)
        return [scale(c, level) for c in frame]
    if kind == "gauge":
        fraction = clamp01(scene.fraction if scene.fraction is not None else 0.0)
        hot = scene.color2 if scene.color2 is not None else scene.color
        frame = fill_frame(n, fraction, lerp(scene.color, hot, fraction), scene.track, scene.head_glow)
        return [scale(c, level) for c in frame]
    if kind == "segment_breathe":
        start, end = SEGMENTS.get(scene.segment, (0.0, 1.0))
        amount = breathe_level(now, scene.period, scene.floor) * level
        lit = scale(scene.color, amount)
        return [lit if on else BLACK for on in segment_mask(n, start, end)]
    raise ValueError(f"unknown scene kind {kind!r}")


def scale_over(color: RGB, factor: float) -> RGB:
    """Like ``scale`` but allows factors above 1 (pushes towards white)."""
    if factor <= 1.0:
        return scale(color, factor)
    extra = min(factor - 1.0, 1.0)
    return lerp(color, (1.0, 1.0, 1.0), extra * 0.6)


# --------------------------------------------------------------------------
# Compositor: crossfades between scenes, smooths the fill fraction
# --------------------------------------------------------------------------


class Smoother:
    """Exponential smoothing of a scalar with a time constant in seconds."""

    def __init__(self, tau: float) -> None:
        self.tau = max(0.0, tau)
        self.value: Optional[float] = None
        self._last = None

    def reset(self, value: Optional[float] = None) -> None:
        self.value = value
        self._last = None

    def update(self, target: Optional[float], now: float) -> Optional[float]:
        if target is None:
            self.value = None
            self._last = now
            return None
        if self.value is None or self.tau <= 0.0 or self._last is None:
            self.value = target
        else:
            dt = max(0.0, now - self._last)
            alpha = 1.0 - math.exp(-dt / self.tau) if self.tau > 0 else 1.0
            self.value += (target - self.value) * alpha
            if abs(target - self.value) < 0.0005:
                self.value = target
        self._last = now
        return self.value


class Compositor:
    """Renders scenes and crossfades whenever the scene *name* changes."""

    def __init__(self, n: int, fade_seconds: float = 0.35, fill_tau: float = 0.8) -> None:
        self.n = n
        self.fade_seconds = max(0.0, fade_seconds)
        self.scene: Scene = OFF
        self._fade_from: Optional[list[RGB]] = None
        self._fade_start = 0.0
        self._last_frame: list[RGB] = [BLACK] * n
        self.fill = Smoother(fill_tau)
        self._fill_key: Optional[str] = None

    def set_scene(self, scene: Scene, now: float, fill_key: Optional[str] = None, instant: bool = False) -> None:
        if scene.name != self.scene.name:
            self._fade_from = None if instant else list(self._last_frame)
            self._fade_start = now
            scene = replace(scene, started=now)
            if fill_key != self._fill_key:
                self.fill.reset()
        elif fill_key != self._fill_key:
            # same scene kind but a different download: jump instead of sweeping back
            self.fill.reset()
        else:
            scene = replace(scene, started=self.scene.started)
        self._fill_key = fill_key
        self.scene = scene

    def render(self, now: float) -> list[RGB]:
        scene = self.scene
        if scene.kind in ("fill", "gauge"):
            smoothed = self.fill.update(scene.fraction, now)
            scene = scene.with_fraction(smoothed)
        frame = render_scene(scene, self.n, now)
        if self._fade_from is not None:
            elapsed = now - self._fade_start
            if self.fade_seconds <= 0.0 or elapsed >= self.fade_seconds:
                self._fade_from = None
            else:
                frame = blend_frames(self._fade_from, frame, elapsed / self.fade_seconds)
        self._last_frame = frame
        return frame

    @property
    def last_frame(self) -> list[RGB]:
        return self._last_frame


# --------------------------------------------------------------------------
# Physical mapping
# --------------------------------------------------------------------------


class FrameShaper:
    """Maps a logical frame onto the physical chain and quantises it."""

    def __init__(self, count: int, reverse: bool = False, offset: int = 0, brightness: float = 1.0,
                 gamma: float = 2.2, color_order: str = "RGB") -> None:
        self.count = count
        self.reverse = reverse
        self.offset = offset
        self.brightness = clamp01(brightness)
        self.gamma = gamma
        self.color_order = color_order.upper()

    @property
    def total(self) -> int:
        return self.count + self.offset

    def shape(self, frame: Sequence[RGB], indicator: Optional[RGB] = None) -> list[RGB8]:
        """Map the logical bar frame onto the physical chain.

        ``indicator`` (a 0..1 colour, or None) fills the ``offset`` LEDs at the
        start of the chain; None leaves them off.  It goes through the same
        brightness/gamma/colour-order pipeline as the bar.
        """
        if len(frame) != self.count:
            raise ValueError(f"frame has {len(frame)} LEDs, expected {self.count}")
        logical = list(frame)
        if self.reverse:
            logical.reverse()
        if indicator is None:
            physical: list[RGB8] = [(0, 0, 0)] * self.offset
        else:
            physical = [self._quantise(indicator)] * self.offset
        for color in logical:
            physical.append(self._quantise(color))
        return physical

    def _quantise(self, color: RGB) -> RGB8:
        rgb8 = to_rgb8(color, self.gamma, self.brightness)
        return reorder(rgb8, self.color_order) if self.color_order != "RGB" else rgb8

    def fade_out(self, last: Sequence[RGB8], steps: int) -> list[list[RGB8]]:
        """Frames that dim ``last`` to black over ``steps`` frames."""
        frames = []
        for step in range(1, steps + 1):
            k = 1.0 - step / steps
            frames.append([(int(r * k), int(g * k), int(b * k)) for r, g, b in last])
        return frames

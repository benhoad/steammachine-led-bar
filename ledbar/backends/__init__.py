"""Output backends: where the rendered frames go."""

from __future__ import annotations

import logging

from ..config import Config
from .base import Backend, BackendError


def create_backend(config: Config, name: str | None = None) -> Backend:
    name = (name or config.output.backend).lower()
    total = config.leds.total
    if name in ("openrgb", "openrgb-basic"):
        from .openrgb import OpenRGBBackend, OpenRGBModeBackend

        if name == "openrgb-basic" or config.openrgb.whole_strip:
            return OpenRGBModeBackend(
                config.openrgb, total,
                brightness=config.leds.brightness / 100.0,
                color_order=config.leds.color_order,
            )
        return OpenRGBBackend(config.openrgb, total)
    if name == "terminal":
        from .terminal import TerminalBackend

        return TerminalBackend(total, offset=config.leds.offset)
    if name == "null":
        from .null import NullBackend

        return NullBackend(total)
    raise BackendError(f"unknown backend {name!r}")


__all__ = ["Backend", "BackendError", "create_backend"]

from __future__ import annotations

from typing import Optional

from ..colors import RGB8
from .base import Backend


class NullBackend(Backend):
    """Discards frames; handy for testing the daemon logic without hardware."""

    name = "null"

    def __init__(self, total: int) -> None:
        super().__init__(total)
        self.frames = 0
        self.last: Optional[list[RGB8]] = None

    def write(self, frame: list[RGB8]) -> bool:
        self.frames += 1
        self.last = list(frame)
        return True

    def describe(self) -> str:
        return f"null backend ({self.total} LEDs)"

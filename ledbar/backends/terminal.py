"""Draws the LED chain in the terminal with 24-bit ANSI colours.

This is the simulator: it lets you try every pattern on any machine (even
one without an LED strip or OpenRGB) and see exactly what the hardware
would receive, in physical chain order.
"""

from __future__ import annotations

import sys
from typing import Optional, TextIO

from ..colors import RGB8
from .base import Backend

RESET = "\x1b[0m"


class TerminalBackend(Backend):
    name = "terminal"

    def __init__(self, total: int, offset: int = 0, stream: Optional[TextIO] = None, glyph: str = "  ") -> None:
        super().__init__(total)
        self.offset = offset
        self.stream = stream or sys.stdout
        self.glyph = glyph
        self.status = ""
        self._tty = hasattr(self.stream, "isatty") and self.stream.isatty()
        self._last_line = ""
        self._last_status_printed = ""

    def open(self) -> None:
        if self._tty:
            self.stream.write("\x1b[?25l")  # hide cursor
            self.stream.flush()

    def set_status(self, text: str) -> None:
        self.status = text

    def render_line(self, frame: list[RGB8]) -> str:
        parts = []
        for index, (r, g, b) in enumerate(frame):
            if index < self.offset:
                parts.append(f"\x1b[48;2;25;25;25m{self.glyph}{RESET}")
            else:
                parts.append(f"\x1b[48;2;{r};{g};{b}m{self.glyph}{RESET}")
        return "".join(parts)

    def write(self, frame: list[RGB8]) -> bool:
        line = self.render_line(frame)
        if self._tty:
            self.stream.write(f"\r\x1b[K[{line}] {self.status}")
            self.stream.flush()
        else:
            # not a terminal (journal, pipe): only log status changes
            if self.status != self._last_status_printed:
                self.stream.write(f"{self.status}\n")
                self.stream.flush()
                self._last_status_printed = self.status
        self._last_line = line
        return True

    def close(self, final: Optional[list[RGB8]], on_exit: str = "off") -> None:
        if final is not None:
            self.write(final)
        if self._tty:
            self.stream.write("\x1b[?25h\n")
            self.stream.flush()

    def describe(self) -> str:
        return f"terminal simulator ({self.total} LEDs, first LED on the left)"

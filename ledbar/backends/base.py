from __future__ import annotations

from typing import Any, Optional

from ..colors import RGB8


class BackendError(Exception):
    """Raised for problems the user may have to fix.

    ``fatal`` marks the ones that can never resolve themselves (a missing Python
    package, say).  Everything else is retried: when ledbar and OpenRGB start
    together, OpenRGB accepts connections *before* it has finished enumerating
    hardware, so "no devices" and "device not found" are normal for the first
    few seconds and must not kill the service.
    """

    def __init__(self, message: str, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal


class Backend:
    """Base class for LED outputs.

    ``write`` receives the frame in *physical* order (index 0 is the first
    LED on the chain) and returns ``True`` when the frame reached the
    hardware.  Backends are expected to recover from transient errors on
    their own (reconnecting, for example) and never raise from ``write``.
    """

    name = "base"

    # When True the daemon calls ``apply_decision`` with the current state
    # instead of streaming per-LED frames through ``write``.  Used by backends
    # that can only drive the whole strip via hardware effects.
    mode_based = False

    def __init__(self, total: int) -> None:
        self.total = total

    def open(self) -> None:
        pass

    def write(self, frame: list[RGB8]) -> bool:
        raise NotImplementedError

    def apply_decision(self, decision: Any, now: float) -> bool:
        """Whole-strip backends: apply the current state (a state.Decision).

        Only called when ``mode_based`` is True.  Returns True when the state
        reached the hardware.
        """
        raise NotImplementedError

    def resync(self) -> None:
        """Called after a resume from suspend: re-apply modes, resend state."""

    def set_status(self, text: str) -> None:
        """Optional human readable status (used by the terminal backend)."""

    def close(self, final: Optional[list[RGB8]], on_exit: str = "off") -> None:
        pass

    def describe(self) -> str:
        return self.name

    @property
    def healthy(self) -> bool:
        return True

    @property
    def last_error(self) -> str:
        """Why the backend is unhealthy, for `ledbar status`."""
        return ""

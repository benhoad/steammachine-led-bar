"""Detects OS updates running in the background.

On a Steam Machine an OS update is just another Steam download, because Valve
owns both ends.  On Bazzite the updater (uupd / rpm-ostree / bootc) is entirely
separate from Steam, so ledbar has to watch it directly.

The robust signal is simply "is the updater's systemd unit running".  A real
percentage would be nicer, but rpm-ostree only publishes transaction progress
over D-Bus and that surface is shifting as Universal Blue migrates to bootc, so
this reports a fraction only when something hands us one and otherwise leaves it
to the renderer to show an indeterminate animation.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

log = logging.getLogger("ledbar.updates")

# Units that mean "the OS is updating itself", newest mechanism first.
DEFAULT_UNITS = (
    "uupd.service",                        # Universal Blue / Bazzite updater
    "bootc-fetch-apply-updates.service",   # bootc
    "rpm-ostreed-automatic.service",       # Fedora atomic automatic updates
    "packagekit-offline-update.service",   # generic offline updates
)

ACTIVE_STATES = ("active", "activating", "reloading")


@dataclass
class SystemUpdate:
    """What the OS updater is doing."""

    active: bool = False
    unit: str = ""
    fraction: Optional[float] = None       # None = running, progress unknown

    def __bool__(self) -> bool:
        return self.active


def systemctl_states(units: Iterable[str]) -> dict[str, str]:
    """Ask systemd for each unit's ActiveState in one call."""
    units = list(units)
    if not units or not shutil.which("systemctl"):
        return {}
    try:
        result = subprocess.run(
            ["systemctl", "show", "--property=ActiveState", "--value", *units],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("systemctl failed: %s", exc)
        return {}
    lines = [line.strip() for line in result.stdout.splitlines()]
    # `systemctl show` emits one value per unit, in order
    return {unit: (lines[index] if index < len(lines) else "") for index, unit in enumerate(units)}


class SystemUpdateMonitor:
    """Polls the updater units at a modest interval."""

    def __init__(self, enabled: bool = False, units: Iterable[str] = (),
                 poll_interval: float = 5.0,
                 states: Optional[Callable[[Iterable[str]], dict[str, str]]] = None) -> None:
        self.enabled = enabled
        self.units = list(units) or list(DEFAULT_UNITS)
        self.poll_interval = poll_interval
        self._states = states or systemctl_states
        self.state = SystemUpdate()
        self._last = -1e9

    def poll(self, now: Optional[float] = None, force: bool = False) -> SystemUpdate:
        now = time.monotonic() if now is None else now
        if not self.enabled:
            self.state = SystemUpdate()
            return self.state
        if not force and now - self._last < self.poll_interval:
            return self.state
        self._last = now

        states = self._states(self.units)
        for unit in self.units:
            if states.get(unit, "") in ACTIVE_STATES:
                if not self.state.active:
                    log.info("system update running (%s)", unit)
                self.state = SystemUpdate(active=True, unit=unit)
                return self.state
        if self.state.active:
            log.info("system update finished (%s)", self.state.unit)
        self.state = SystemUpdate()
        return self.state

"""Watches whether the LED controller is actually receiving our frames.

Adalight is a one-way protocol: the controller never acknowledges anything, so
``backend.write()`` returning True only means OpenRGB accepted the bytes.  If the
controller stops consuming them - an ESP32's native USB endpoint can wedge when
the host reboots while the board stays powered - everything upstream looks
perfectly healthy while the strip sits frozen.

WLED does expose liveness over HTTP: ``/json/info`` reports ``live: true`` while
it is receiving realtime data.  This polls that, off the render thread, and can
optionally recover by rebooting the controller and restarting the OpenRGB server
(which otherwise keeps a stale file descriptor for the re-enumerated port).

Recovery deliberately goes over the network rather than the serial link, because
in the failure being detected the controller is not reading serial at all.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("ledbar.health")


@dataclass
class HealthState:
    host: str = ""
    checked: bool = False          # did we manage to reach the controller at all
    healthy: bool = True
    reason: str = ""
    last_live: float = 0.0
    resets: int = 0
    reset_times: list[float] = field(default_factory=list)


def fetch_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - user-configured host
        return json.loads(response.read().decode("utf-8", errors="replace"))


def discover_wled(timeout: float = 3.0) -> str:
    """Best-effort mDNS lookup of a WLED controller; "" when not found."""
    if not shutil.which("avahi-browse"):
        return ""
    try:
        result = subprocess.run(
            ["avahi-browse", "-rtp", "--no-db-lookup", "_wled._tcp"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.splitlines():
        # =;iface;IPv4;name;_wled._tcp;local;host;ADDRESS;port;txt
        parts = line.split(";")
        if parts and parts[0] == "=" and len(parts) > 7 and parts[2] == "IPv4" and parts[7]:
            return parts[7]
    return ""


class LinkHealthMonitor:
    """Polls the controller's liveness in a background thread."""

    def __init__(self, enabled: bool = True, host: str = "", check_interval: float = 30.0,
                 grace_seconds: float = 60.0, action: str = "warn", max_resets_per_hour: int = 3,
                 restart_command: str = "systemctl --user restart ledbar-openrgb",
                 timeout: float = 3.0,
                 fetch: Optional[Callable[[str, float], dict]] = None,
                 runner: Optional[Callable[[list[str]], None]] = None,
                 discover: Optional[Callable[[], str]] = None) -> None:
        self.enabled = enabled
        self.configured_host = host
        self.check_interval = check_interval
        self.grace_seconds = grace_seconds
        self.action = action
        self.max_resets_per_hour = max_resets_per_hour
        self.restart_command = restart_command
        self.timeout = timeout
        self._fetch = fetch or fetch_json
        self._runner = runner or self._run
        self._discover = discover or discover_wled

        self.state = HealthState(host=host)
        # reentrant: check() calls snapshot() from inside its own locked section
        self._lock = threading.RLock()
        self._streaming = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._warned = False

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _run(command: list[str]) -> None:
        subprocess.run(command, check=False, timeout=30)

    def set_streaming(self, streaming: bool) -> None:
        """The render loop tells us whether it believes it is sending frames."""
        self._streaming = streaming

    def snapshot(self) -> HealthState:
        with self._lock:
            return HealthState(**{**self.state.__dict__, "reset_times": list(self.state.reset_times)})

    def resolve_host(self) -> str:
        if self.configured_host:
            return self.configured_host
        if not self.state.host:
            found = self._discover()
            if found:
                log.info("health: discovered controller at %s", found)
            with self._lock:
                self.state.host = found
        return self.state.host

    # -- one check ---------------------------------------------------------

    def check(self, now: Optional[float] = None) -> HealthState:
        """Run a single liveness check (also the unit of work for the thread)."""
        now = time.monotonic() if now is None else now
        if not self.enabled:
            return self.snapshot()
        host = self.resolve_host()
        if not host:
            return self.snapshot()

        try:
            info = self._fetch(f"http://{host}/json/info", self.timeout)
            reachable = True
        except Exception as exc:
            info, reachable = {}, False
            with self._lock:
                self.state.checked = False
                self.state.reason = f"controller unreachable: {exc}"

        if not reachable:
            # Can't tell - don't declare a fault on a Wi-Fi blip.
            return self.snapshot()

        live = bool(info.get("live"))
        with self._lock:
            self.state.checked = True
            self.state.host = host
            if live or not self._streaming:
                # streaming and seen, or not streaming so nothing is expected
                self.state.last_live = now
                if not self.state.healthy:
                    log.info("health: controller is receiving frames again")
                self.state.healthy = True
                self.state.reason = ""
                self._warned = False
                return self.snapshot()
            if self.state.last_live == 0.0:
                self.state.last_live = now      # start the clock on first sight
                return self.snapshot()
            stale_for = now - self.state.last_live
            if stale_for < self.grace_seconds:
                return self.snapshot()
            self.state.healthy = False
            self.state.reason = (
                f"streaming for {stale_for:.0f}s but {host} reports no realtime source "
                "(the controller is not consuming the data)"
            )

        if not self._warned:
            log.warning("health: %s", self.state.reason)
            self._warned = True
        if self.action == "reset":
            self._attempt_reset(host, now)
        return self.snapshot()

    # -- recovery ----------------------------------------------------------

    def _attempt_reset(self, host: str, now: float) -> None:
        with self._lock:
            recent = [t for t in self.state.reset_times if now - t < 3600.0]
            self.state.reset_times = recent
            if len(recent) >= self.max_resets_per_hour:
                log.error("health: giving up, already reset %d times this hour", len(recent))
                return
            self.state.reset_times.append(now)
            self.state.resets += 1

        log.warning("health: rebooting the controller at %s", host)
        try:
            self._fetch(f"http://{host}/reset", self.timeout)
        except Exception as exc:
            # /reset closes the connection as it reboots, so errors are expected
            log.debug("health: reset request returned %s", exc)

        if self.restart_command:
            log.warning("health: restarting the OpenRGB server so it reopens the port")
            try:
                self._runner(shlex.split(self.restart_command))
            except Exception as exc:
                log.error("health: restart command failed: %s", exc)

        with self._lock:
            self.state.last_live = now + self.grace_seconds   # let it come back before judging again

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ledbar-health", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.check_interval):
            try:
                self.check()
            except Exception as exc:                       # never kill the thread
                log.debug("health: check failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()

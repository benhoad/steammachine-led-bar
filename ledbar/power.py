"""Listens to logind for suspend/resume and shutdown notifications.

Uses ``gdbus monitor`` (part of glib2, present on Bazzite) as a subprocess
so that no D-Bus Python bindings are needed.  Falls back to ``busctl``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Optional

log = logging.getLogger("ledbar.power")

GDBUS_CMD = ["gdbus", "monitor", "--system", "--dest", "org.freedesktop.login1",
             "--object-path", "/org/freedesktop/login1"]
BUSCTL_CMD = ["busctl", "monitor", "--system",
              "--match", "type='signal',interface='org.freedesktop.login1.Manager'"]


class PowerMonitor:
    """Tracks ``sleeping`` / ``shutting_down`` flags from logind signals."""

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self.sleeping = False
        self.shutting_down = False
        self.available = False
        self.tool = "none"
        self._resume_pending = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen[str]] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if not self.enabled:
            return
        if shutil.which("gdbus"):
            self.tool = "gdbus"
        elif shutil.which("busctl"):
            self.tool = "busctl"
        else:
            log.info("neither gdbus nor busctl found; sleep/shutdown detection disabled")
            return
        self.available = True
        self._thread = threading.Thread(target=self._run, name="ledbar-power", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def take_resume_event(self) -> bool:
        """True once after every resume (consumed by the caller)."""
        with self._lock:
            pending = self._resume_pending
            self._resume_pending = False
        return pending

    # -- monitor loop ------------------------------------------------------

    def _run(self) -> None:
        cmd = GDBUS_CMD if self.tool == "gdbus" else BUSCTL_CMD
        while not self._stop.is_set():
            try:
                self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            except OSError as exc:
                log.warning("cannot start %s: %s", cmd[0], exc)
                self.available = False
                return
            member: Optional[str] = None
            assert self._proc.stdout is not None
            for line in self._proc.stdout:
                if self._stop.is_set():
                    break
                line = line.strip()
                if self.tool == "gdbus":
                    self._handle_gdbus_line(line)
                else:
                    member = self._handle_busctl_line(line, member)
            try:
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                pass
            if not self._stop.is_set():
                time.sleep(5.0)

    def _handle_gdbus_line(self, line: str) -> None:
        # e.g. "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)"
        if "PrepareForSleep" in line:
            self._set_sleeping("true" in line.split("(", 1)[-1].lower())
        elif "PrepareForShutdown" in line:
            if "true" in line.split("(", 1)[-1].lower():
                self._set_shutdown()

    def _handle_busctl_line(self, line: str, member: Optional[str]) -> Optional[str]:
        if "Member=" in line:
            for part in line.split():
                if part.startswith("Member="):
                    return part.split("=", 1)[1]
            return member
        if "BOOLEAN" in line and member:
            value = "true" in line.lower()
            if member == "PrepareForSleep":
                self._set_sleeping(value)
            elif member == "PrepareForShutdown" and value:
                self._set_shutdown()
            return None
        return member

    def _set_sleeping(self, value: bool) -> None:
        with self._lock:
            if self.sleeping and not value:
                self._resume_pending = True
            self.sleeping = value
        log.info("system is %s", "going to sleep" if value else "waking up")

    def _set_shutdown(self) -> None:
        with self._lock:
            self.shutting_down = True
        log.info("system is shutting down")

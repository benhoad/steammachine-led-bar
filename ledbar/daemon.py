"""The main loop: gather inputs, decide, render, send.

``Daemon`` is deliberately small; all the interesting behaviour lives in
``state.py`` (what to show) and ``render.py`` (how it looks).  Inputs come
from an ``InputSource`` so the same loop can run against the real system
or a scripted timeline (``ledbar demo``).
"""

from __future__ import annotations

import logging
import signal
import time
from pathlib import Path
from typing import Optional

from .backends.base import Backend, BackendError
from .colors import RGB8
from .config import Config
from .power import PowerMonitor
from .render import Compositor, FrameShaper
from .sensors import FaultMonitor, ThermalMonitor
from .state import Inputs, StateMachine, indicator_color
from .steam import DownloadState, SteamMonitor

log = logging.getLogger("ledbar.daemon")


class InputSource:
    """Something that can answer "what is the system doing right now?"."""

    def start(self) -> None:
        pass

    def poll(self, now: float) -> Inputs:
        return Inputs(now=now)

    def take_resume_event(self) -> bool:
        return False

    def stop(self) -> None:
        pass


class LiveInputs(InputSource):
    """Real monitors: Steam manifests, hwmon temperatures, logind, faults."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.steam = SteamMonitor(
            extra_roots=config.steam.paths,
            stale_seconds=config.progress.stale_seconds,
            show_paused=config.progress.show_paused,
            require_steam_process=config.steam.require_process,
        )
        self.thermal = ThermalMonitor(config.thermal)
        self.faults = FaultMonitor(config.faults, lambda: list(self.steam.libraries))
        self.power = PowerMonitor(config.power.monitor)
        self._last_steam = -1e9
        self._steam_state = DownloadState()

    def start(self) -> None:
        self.steam.rescan_libraries()
        if self.steam.libraries:
            log.info("Steam libraries: %s", ", ".join(str(p) for p in self.steam.libraries))
        else:
            log.warning("no Steam libraries found (looked in %s)", ", ".join(self.config.steam.paths) or "the default locations")
        self.thermal.poll(force=True)
        log.info("temperatures: %s", self.thermal.summary())
        self.power.start()
        if self.power.available:
            log.info("sleep/shutdown detection via %s", self.power.tool)

    def poll(self, now: float) -> Inputs:
        if now - self._last_steam >= self.config.steam.poll_interval:
            try:
                self._steam_state = self.steam.poll()
            except Exception as exc:  # never let a Steam hiccup kill the bar
                log.warning("steam poll failed: %s", exc)
            self._last_steam = now
        self.thermal.poll(now)
        faults = self.faults.poll(now)
        return Inputs(
            now=now,
            steam=self._steam_state,
            cpu_temp=self.thermal.cpu_temp,
            gpu_temp=self.thermal.gpu_temp,
            faults=faults,
            sleeping=self.power.sleeping,
            shutting_down=self.power.shutting_down,
        )

    def take_resume_event(self) -> bool:
        return self.power.take_resume_event()

    def stop(self) -> None:
        self.power.stop()


class Daemon:
    def __init__(self, config: Config, backend: Backend, inputs: InputSource) -> None:
        self.config = config
        self.backend = backend
        self.inputs = inputs
        self._stop = False
        self.shaper = FrameShaper(
            count=config.leds.count,
            reverse=config.leds.reverse,
            offset=config.leds.offset,
            brightness=config.leds.brightness / 100.0,
            gamma=config.leds.gamma,
            color_order=config.leds.color_order,
        )
        self.compositor = Compositor(config.leds.count, fade_seconds=0.35, fill_tau=config.progress.smoothing_seconds)
        self.machine: Optional[StateMachine] = None
        self.last_sent: Optional[list[RGB8]] = None
        self.frames_sent = 0

    # -- signals -----------------------------------------------------------

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def _install_signals(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, self.request_stop)
            except (ValueError, OSError):  # not on the main thread
                pass

    # -- main loop ---------------------------------------------------------

    def run(self, max_seconds: Optional[float] = None) -> int:
        cfg = self.config
        self._install_signals()
        self.backend.open()
        log.info("output: %s", self.backend.describe())
        self.inputs.start()

        start = time.monotonic()
        self.machine = StateMachine(cfg, start)
        period = 1.0 / cfg.leds.fps
        self._power_led = cfg.leds.offset_mode == "power_led" and cfg.leds.offset > 0
        if self._power_led and self.backend.mode_based:
            log.warning("[leds] offset_mode = 'power_led' needs per-LED control; ignored in whole-strip mode")
            self._power_led = False
        last_send_time = -1e9
        last_status = None
        last_scene = None
        # The boot animation only makes sense once we can actually show it, so
        # it (re)starts the first time the output becomes usable.
        seen_output = self.backend.healthy

        while not self._stop:
            now = time.monotonic()
            if not seen_output and self.backend.healthy:
                seen_output = True
                if cfg.boot.enabled:
                    self.machine.restart_boot(now)
            if self.inputs.take_resume_event():
                log.info("resumed from suspend; re-initialising output")
                self.backend.resync()
                if cfg.power.resume_boot:
                    self.machine.restart_boot(now)

            inputs = self.inputs.poll(now)
            decision = self.machine.update(inputs)
            scene = decision.scene

            if decision.status != last_status:
                log.info("state: %s", decision.status)
                self.backend.set_status(decision.status)
                last_status = decision.status
            last_scene = scene.name

            if self.backend.mode_based:
                # whole-strip backends map the state onto a hardware effect and
                # only write when it changes, so there is no per-frame rendering.
                if self.backend.apply_decision(decision, now):
                    self.frames_sent += 1
            else:
                instant = scene.name in ("sleep", "shutdown") and last_scene not in ("sleep", "shutdown")
                self.compositor.set_scene(scene, now, decision.fill_key, instant=instant)
                frame = self.compositor.render(now)
                indicator = indicator_color(decision, cfg) if self._power_led else None
                physical = self.shaper.shape(frame, indicator)
                if physical != self.last_sent or now - last_send_time >= cfg.leds.keepalive_seconds:
                    if self.backend.write(physical):
                        self.last_sent = physical
                        last_send_time = now
                        self.frames_sent += 1

            if max_seconds is not None and now - start >= max_seconds:
                break
            elapsed = time.monotonic() - now
            if elapsed < period:
                time.sleep(period - elapsed)

        self._shutdown()
        return 0

    def _shutdown(self) -> None:
        cfg = self.config
        log.info("stopping (%d frames sent)", self.frames_sent)
        try:
            on_exit = cfg.power.on_exit
            fade_steps = int(cfg.power.fade_seconds * cfg.leds.fps)
            if self.last_sent is not None and fade_steps > 0 and self.backend.healthy and on_exit == "off":
                for frame in self.shaper.fade_out(self.last_sent, fade_steps):
                    self.backend.write(frame)
                    time.sleep(1.0 / cfg.leds.fps)
            final: Optional[list[RGB8]]
            if on_exit == "hold":
                final = None
            elif on_exit.startswith("mode:") and self.machine is not None:
                final = self.shaper.shape([self.machine.c_bar] * cfg.leds.count)
            else:
                final = [(0, 0, 0)] * self.shaper.total
            self.backend.close(final, on_exit)
        finally:
            self.inputs.stop()

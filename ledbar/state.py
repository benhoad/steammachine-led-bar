"""The state machine: decides what the bar shows, in priority order.

Priority (highest first):

1. shutdown / suspend            -> off (configurable for suspend)
2. overheating                   -> solid red            (Steam Machine)
3. hardware / system faults      -> red breathing segment (Steam Machine style)
4. thermal warning (optional)    -> amber breathing
5. booting                       -> blue breathing        (Steam Machine)
6. Steam download / install      -> blue fill, left to right (Steam Machine)
7. download just finished        -> short pulse (optional)
8. game running (optional)       -> off / dim / colour
9. idle                          -> solid blue            (Steam Machine)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .colors import RGB, parse_hex, scale
from .config import Config
from .render import Scene
from .sensors import Fault
from .steam import DownloadState
from .updates import SystemUpdate


@dataclass
class Inputs:
    now: float                           # monotonic seconds
    steam: DownloadState = field(default_factory=DownloadState)
    cpu_temp: Optional[float] = None
    gpu_temp: Optional[float] = None
    faults: list[Fault] = field(default_factory=list)
    sleeping: bool = False
    shutting_down: bool = False
    system_update: SystemUpdate = field(default_factory=SystemUpdate)


@dataclass
class Decision:
    scene: Scene
    fill_key: Optional[str] = None       # identifies the download for fill smoothing
    status: str = ""                     # human readable, for logs / terminal backend


class Threshold:
    """A level trigger with hysteresis (on at ``level``, off below ``level - hysteresis``)."""

    def __init__(self, level: float, hysteresis: float) -> None:
        self.level = level
        self.hysteresis = max(0.0, hysteresis)
        self.active = False

    def update(self, value: Optional[float]) -> bool:
        if value is None:
            self.active = False
        elif self.active:
            if value <= self.level - self.hysteresis:
                self.active = False
        elif value >= self.level:
            self.active = True
        return self.active


class StateMachine:
    def __init__(self, config: Config, now: float) -> None:
        self.config = config
        self.critical = Threshold(config.thermal.critical_c, config.thermal.hysteresis_c)
        self.warning = Threshold(config.thermal.warning_c, config.thermal.hysteresis_c)
        self.boot_active = config.boot.enabled
        # Whether a status indicator exists to carry the "this is the OS, not a
        # game" signal.  The daemon overrides this once it knows the backend can
        # actually address the indicator LEDs.
        self.has_indicator = config.leds.offset_mode == "power_led" and config.leds.offset > 0
        self.boot_started = now
        self.progress_key: Optional[str] = None
        self.last_fraction: Optional[float] = None
        self.last_phase = "idle"
        self.complete_until = -1.0
        self.last_status = ""
        # colours resolved once
        self.c_bar: RGB = config.rgb("bar")
        self.c_boot: RGB = config.rgb("boot")
        self.c_complete: RGB = config.rgb("complete")
        self.c_error: RGB = config.rgb("error")
        self.c_warning: RGB = config.rgb("warning")
        self.c_track: RGB = config.rgb("track")
        self.c_game: RGB = config.rgb("game")
        self.c_gauge_cool: RGB = config.rgb("gauge_cool")
        self.c_gauge_hot: RGB = config.rgb("gauge_hot")

    # -- public ------------------------------------------------------------

    def restart_boot(self, now: float) -> None:
        self.boot_active = self.config.boot.enabled
        self.boot_started = now

    def update(self, inputs: Inputs) -> Decision:
        cfg = self.config
        now = inputs.now
        steam = inputs.steam

        # thermal -----------------------------------------------------------
        temps = [t for t in (inputs.cpu_temp, inputs.gpu_temp) if t is not None]
        hottest = max(temps) if temps else None
        critical = cfg.thermal.enabled and self.critical.update(hottest)
        warning = cfg.thermal.enabled and cfg.thermal.warning_c > 0 and self.warning.update(hottest)

        # progress bookkeeping (runs even when something else is displayed) -
        active = cfg.progress.enabled and steam.active and (not steam.paused or cfg.progress.show_paused)
        if active and steam.app is not None:
            self.progress_key = f"app-{steam.app.appid}"
            self.last_fraction = steam.fraction
            self.last_phase = steam.phase
        elif self.progress_key is not None:
            finished = (self.last_fraction is not None and self.last_fraction >= 0.97) or self.last_phase == "committing"
            if cfg.progress.complete_flash and finished:
                self.complete_until = now + cfg.progress.complete_seconds
            self.progress_key = None
            self.last_fraction = None

        # boot bookkeeping --------------------------------------------------
        if self.boot_active:
            elapsed = now - self.boot_started
            steam_ready = (steam.steam_running or steam.active or not cfg.boot.wait_for_steam
                           or not cfg.steam.require_process)
            if not cfg.boot.enabled or elapsed >= cfg.boot.timeout_seconds or (elapsed >= cfg.boot.min_seconds and steam_ready):
                self.boot_active = False

        # choose the scene, highest priority first --------------------------
        decision: Decision
        if inputs.shutting_down:
            decision = Decision(Scene(kind="off", name="shutdown"), status="shutting down")
        elif inputs.sleeping and cfg.power.sleep != "hold":
            if cfg.power.sleep == "dim":
                scene = Scene(kind="solid", name="sleep", color=self.c_bar, level=cfg.power.sleep_brightness / 100.0)
            else:
                scene = Scene(kind="off", name="sleep")
            decision = Decision(scene, status="sleeping")
        elif critical:
            decision = Decision(
                Scene(kind="solid", name="thermal-critical", color=self.c_error),
                status=f"OVERHEATING {hottest:.0f}C" if hottest is not None else "OVERHEATING",
            )
        elif inputs.faults:
            fault = inputs.faults[0]
            scene = Scene(
                kind="solid" if fault.solid else "segment_breathe",
                name=f"fault-{fault.key}",
                color=self.c_error,
                segment=fault.segment,
                period=2.0,
                floor=0.05,
            )
            decision = Decision(scene, status=f"fault: {fault.message}")
        elif warning:
            decision = Decision(
                Scene(kind="breathe", name="thermal-warning", color=self.c_warning, period=2.0, floor=0.15),
                status=f"hot {hottest:.0f}C" if hottest is not None else "hot",
            )
        elif self.boot_active:
            waiting = " (waiting for Steam)" if cfg.boot.wait_for_steam and not steam.steam_running else ""
            decision = Decision(
                Scene(kind="breathe", name="boot", color=self.c_boot, period=cfg.boot.period_seconds),
                status=f"booting{waiting}",
            )
        elif inputs.system_update.active:
            decision = self._system_update_decision(inputs.system_update)
        elif active and steam.app is not None:
            decision = self._progress_decision(steam)
        elif now < self.complete_until:
            decision = Decision(
                Scene(kind="pulse", name="complete", color=self.c_complete, duration=cfg.progress.complete_seconds),
                status="download complete",
            )
        elif steam.running_appid is not None and cfg.game.mode != "same":
            decision = self._game_decision(steam.running_appid)
        else:
            decision = self._idle_decision(inputs)

        if decision.status != self.last_status:
            self.last_status = decision.status
        return decision

    # -- helpers -----------------------------------------------------------

    def _progress_decision(self, steam: DownloadState) -> Decision:
        cfg = self.config
        app = steam.app
        assert app is not None
        level = cfg.progress.paused_brightness / 100.0 if steam.paused else 1.0
        label = f"{steam.phase} {app.name}"
        if steam.fraction is None:
            if cfg.progress.indeterminate == "off":
                return self._idle_decision(Inputs(now=0.0, steam=steam))
            scene = Scene(kind="breathe", name="progress-indeterminate", color=self.c_bar, period=2.0, floor=0.2, level=level)
            return Decision(scene, fill_key=self.progress_key, status=f"{label} (no progress info)")
        scene = Scene(
            kind="fill",
            name="progress",
            color=self.c_bar,
            fraction=steam.fraction,
            track=self.c_track,
            head_glow=cfg.progress.head_glow,
            level=level,
        )
        return Decision(scene, fill_key=self.progress_key, status=f"{label} {steam.fraction * 100:.0f}%")

    def _system_update_decision(self, update: SystemUpdate) -> Decision:
        """How an OS update looks depends on whether there is an indicator LED.

        With one, this mirrors the Steam Machine: the bar is identical to a
        content download and the small indicator recolours to say it is the OS.
        Without one there is no second light to carry that meaning, so the bar
        blinks instead - otherwise an OS update would be indistinguishable from
        a game download.
        """
        cfg = self.config
        unit = f" ({update.unit})" if update.unit else ""

        if not self.has_indicator:
            scene = Scene(kind="blink", name="system-update", color=self.c_bar, period=1.5)
            return Decision(scene, fill_key="system-update", status=f"system update{unit}")

        if update.fraction is None:
            scene = Scene(kind="breathe", name="system-update", color=self.c_bar, period=2.0, floor=0.2)
            return Decision(scene, fill_key="system-update", status=f"system update{unit}")
        scene = Scene(
            kind="fill", name="system-update", color=self.c_bar, fraction=update.fraction,
            track=self.c_track, head_glow=cfg.progress.head_glow,
        )
        return Decision(scene, fill_key="system-update",
                        status=f"system update{unit} {update.fraction * 100:.0f}%")

    def _game_decision(self, appid: int) -> Decision:
        cfg = self.config
        mode = cfg.game.mode
        if mode == "off":
            return Decision(Scene(kind="off", name="game-off"), status=f"game {appid} running (bar off)")
        if mode == "dim":
            scene = Scene(kind="solid", name="game-dim", color=self.c_bar, level=cfg.game.brightness / 100.0)
            return Decision(scene, status=f"game {appid} running (dimmed)")
        scene = Scene(kind="solid", name="game-solid", color=self.c_game)
        return Decision(scene, status=f"game {appid} running")

    def _idle_decision(self, inputs: Inputs) -> Decision:
        cfg = self.config
        level = cfg.idle.brightness / 100.0
        mode = cfg.idle.mode
        if mode == "off":
            return Decision(Scene(kind="off", name="idle-off"), status="idle (off)")
        if mode == "breathe":
            scene = Scene(kind="breathe", name="idle-breathe", color=self.c_bar, period=cfg.idle.breathe_period, level=level, floor=0.15)
            return Decision(scene, status="idle")
        if mode == "temperature":
            temp = inputs.cpu_temp if inputs.cpu_temp is not None else inputs.gpu_temp
            fraction = 0.0 if temp is None else max(0.0, min(1.0, temp / max(1.0, cfg.idle.temperature_max_c)))
            scene = Scene(kind="gauge", name="idle-temperature", color=self.c_gauge_cool, color2=self.c_gauge_hot,
                          fraction=fraction, level=level, track=self.c_track)
            status = f"idle (cpu {temp:.0f}C)" if temp is not None else "idle (no temperature sensor)"
            return Decision(scene, fill_key="temperature", status=status)
        return Decision(Scene(kind="solid", name="idle", color=self.c_bar, level=level), status="idle")


def indicator_color(decision: "Decision", config: Config) -> Optional[RGB]:
    """Colour for the power-LED indicator (the ``offset`` region), or None = off.

    Mirrors the Steam Machine indicator: white normally, red on overheat/fault,
    and a configurable state going into sleep.  Returns a 0..1 colour already
    scaled by ``[indicator] brightness``; the shaper applies the global
    brightness/gamma/colour-order.
    """
    ind = config.indicator
    name = decision.scene.name
    level = max(0.0, min(1.0, ind.brightness / 100.0))
    if name == "shutdown":
        return None
    if name == "sleep":
        if ind.sleep == "off":
            return None
        return scale(parse_hex(ind.color), level)
    if name == "thermal-critical" or name.startswith("fault-"):
        fault = ind.fault_color or config.colors.error
        return scale(parse_hex(fault), level)
    if name == "thermal-warning":
        return scale(parse_hex(config.colors.warning), level)
    if name == "system-update":
        return scale(parse_hex(ind.update_color or config.colors.bar), level)
    return scale(parse_hex(ind.color), level)

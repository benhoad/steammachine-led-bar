"""Scripted inputs that walk the bar through every state (``ledbar demo``)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .daemon import InputSource
from .sensors import Fault
from .state import Inputs
from .steam import STATE_COMMITTING, STATE_DOWNLOADING, STATE_STAGING, STATE_UPDATE_STARTED, AppStatus, DownloadState
from .updates import SystemUpdate

DEMO_FAULTS = {
    "fault-memtrain": Fault("memtrain", "left_half", "(demo) memory training failed"),
    "fault-ssd": Fault("ssd", "q2", "(demo) SSD not detected"),
    "fault-gpu": Fault("gpu", "right_half", "(demo) GPU failure"),
    "fault-ram": Fault("ram", "q4", "(demo) RAM not detected"),
}

STATE_NAMES = ["boot", "idle", "download", "complete", "install", "system-update", "warning", "critical",
               "fault-memtrain", "fault-ssd", "fault-gpu", "fault-ram", "game", "sleep"]


@dataclass
class Step:
    name: str
    seconds: float


def default_script(config: Config) -> list[Step]:
    steps = [
        Step("boot", 4.0),
        Step("idle", 2.5),
        Step("download", 12.0),
        Step("complete", 3.0),
        Step("install", 5.0),
        Step("complete", 2.5),
        Step("system-update", 5.0),
    ]
    if config.thermal.warning_c > 0:
        steps.append(Step("warning", 4.0))
    steps += [
        Step("critical", 4.0),
        Step("fault-memtrain", 4.0),
        Step("fault-ssd", 4.0),
        Step("fault-gpu", 4.0),
        Step("fault-ram", 4.0),
    ]
    if config.game.mode != "same":
        steps.append(Step("game", 4.0))
    steps += [Step("sleep", 3.0), Step("idle", 3.0)]
    return steps


def _fake_app(appid: int, name: str, flags: int, done: int, total: int, staging: bool = False) -> AppStatus:
    return AppStatus(
        appid=appid, name=name, flags=flags,
        bytes_to_download=0 if staging else total, bytes_downloaded=0 if staging else done,
        bytes_to_stage=total if staging else 0, bytes_staged=done if staging else 0,
        mtime=0.0, path=Path(f"/demo/appmanifest_{appid}.acf"), downloading_dir=True,
    )


class ScriptedInputs(InputSource):
    """Plays a list of (state, seconds) steps, optionally looping."""

    def __init__(self, config: Config, steps: list[Step], loop: bool = False) -> None:
        self.config = config
        self.steps = steps
        self.loop = loop
        self.total = sum(s.seconds for s in steps)
        self._start: Optional[float] = None
        self.finished = False
        self.current = ""

    def start(self) -> None:
        self._start = None

    def _locate(self, now: float) -> tuple[Optional[Step], float]:
        if self._start is None:
            self._start = now
        elapsed = now - self._start
        if elapsed >= self.total:
            if not self.loop:
                self.finished = True
                return None, 0.0
            elapsed = elapsed % self.total
        for step in self.steps:
            if elapsed < step.seconds:
                return step, elapsed / step.seconds
            elapsed -= step.seconds
        return self.steps[-1], 1.0

    def poll(self, now: float) -> Inputs:
        step, progress = self._locate(now)
        if step is None:
            return Inputs(now=now, steam=DownloadState(steam_running=True))
        self.current = step.name
        cfg = self.config
        name = step.name
        steam = DownloadState(steam_running=name != "boot")
        inputs = Inputs(now=now, steam=steam, cpu_temp=45.0, gpu_temp=40.0)
        total = 10 * 1024 ** 3
        if name == "download":
            done = int(total * progress)
            app = _fake_app(440, "Demo Game", STATE_DOWNLOADING | STATE_UPDATE_STARTED | 6, done, total)
            steam.active, steam.app, steam.fraction, steam.phase = True, app, progress, "downloading"
        elif name == "install":
            done = int(total * progress)
            app = _fake_app(620, "Demo Update", STATE_STAGING | STATE_UPDATE_STARTED | 6, done, total, staging=True)
            steam.active, steam.app, steam.fraction, steam.phase = True, app, progress, "installing"
        elif name == "system-update":
            inputs.system_update = SystemUpdate(active=True, unit="uupd.service", fraction=progress)
        elif name == "warning":
            inputs.cpu_temp = max(cfg.thermal.warning_c, 1.0) + 2.0
        elif name == "critical":
            inputs.cpu_temp = cfg.thermal.critical_c + 5.0
        elif name in DEMO_FAULTS:
            inputs.faults = [DEMO_FAULTS[name]]
        elif name == "game":
            steam.running_appid = 440
        elif name == "sleep":
            inputs.sleeping = True
        return inputs


def make_single_state(config: Config, name: str, seconds: float) -> list[Step]:
    if name not in STATE_NAMES:
        raise ValueError(f"unknown demo state {name!r}; choose from {', '.join(STATE_NAMES)}")
    if name == "complete":
        return [Step("download", max(1.0, seconds * 0.4)), Step("complete", seconds)]
    return [Step(name, seconds)]

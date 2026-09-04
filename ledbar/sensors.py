"""Hardware monitoring: temperatures (sysfs hwmon), disk space, failed units.

Everything here is read-only and best effort.  Anything that is missing on
the current machine (no hwmon on a dev laptop, no ``nvidia-smi``, ...) just
yields ``None`` and the LED bar carries on without that signal.
"""

from __future__ import annotations

import glob
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .config import FaultsConfig, ThermalConfig


@dataclass
class Sensor:
    chip: str
    label: str
    path: str
    temp_c: float

    def __str__(self) -> str:
        label = f" {self.label}" if self.label else ""
        return f"{self.chip}{label}: {self.temp_c:.0f}C"


@dataclass
class Fault:
    """A detected hardware/system problem and how to show it on the bar."""

    key: str
    segment: str        # one of render.SEGMENTS
    message: str
    solid: bool = False # solid instead of breathing


def read_hwmon(root: str = "/sys/class/hwmon") -> list[Sensor]:
    """Read every ``temp*_input`` under ``/sys/class/hwmon``."""
    sensors: list[Sensor] = []
    for chip_dir in sorted(glob.glob(os.path.join(root, "hwmon*"))):
        chip = _read_text(os.path.join(chip_dir, "name")) or os.path.basename(chip_dir)
        for input_path in sorted(glob.glob(os.path.join(chip_dir, "temp*_input"))):
            raw = _read_text(input_path)
            if raw is None:
                continue
            try:
                temp = int(raw) / 1000.0
            except ValueError:
                continue
            if temp < -50 or temp > 250:
                continue
            label = _read_text(input_path.replace("_input", "_label")) or ""
            sensors.append(Sensor(chip=chip, label=label, path=input_path, temp_c=temp))
    return sensors


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().strip()
    except OSError:
        return None


# Chips (and preferred labels) that report the CPU / GPU temperature.
CPU_CHIPS: list[tuple[str, list[str]]] = [
    ("k10temp", ["Tdie", "Tctl"]),
    ("zenpower", ["Tdie", "Tctl"]),
    ("coretemp", ["Package id 0"]),
    ("cpu_thermal", []),
    ("x86_pkg_temp", []),
    ("acpitz", []),
]
GPU_CHIPS: list[tuple[str, list[str]]] = [
    ("amdgpu", ["junction", "edge"]),
    ("i915", []),
    ("xe", []),
    ("nouveau", []),
    ("nvidia", []),
]


def _chip_temp(sensors: Iterable[Sensor], chip: str, labels: list[str]) -> Optional[float]:
    matching = [s for s in sensors if s.chip.lower() == chip.lower()]
    if not matching:
        return None
    for label in labels:
        picked = [s.temp_c for s in matching if s.label.lower() == label.lower()]
        if picked:
            return max(picked)
    return max(s.temp_c for s in matching)


def pick_temperature(sensors: Iterable[Sensor], spec: str, auto_chips: list[tuple[str, list[str]]]) -> tuple[Optional[float], str]:
    """Resolve ``spec`` ("auto", "chip" or "chip:label") to a temperature.

    Returns ``(temperature, source_description)``.
    """
    sensors = list(sensors)
    spec = (spec or "auto").strip()
    if spec.lower() in ("", "auto"):
        for chip, labels in auto_chips:
            temp = _chip_temp(sensors, chip, labels)
            if temp is not None:
                return temp, chip
        return None, "none"
    if spec.lower() in ("off", "none", "disabled"):
        return None, "disabled"
    chip, _, label = spec.partition(":")
    temp = _chip_temp(sensors, chip, [label] if label else [])
    return temp, spec


class ThermalMonitor:
    """Polls CPU and GPU temperatures at a fixed interval."""

    def __init__(self, cfg: ThermalConfig, hwmon_root: str = "/sys/class/hwmon") -> None:
        self.cfg = cfg
        self.hwmon_root = hwmon_root
        self.cpu_temp: Optional[float] = None
        self.gpu_temp: Optional[float] = None
        self.cpu_source = "none"
        self.gpu_source = "none"
        self.sensors: list[Sensor] = []
        self._last = -1e9
        self._nvidia_smi: Optional[str] = shutil.which("nvidia-smi") if cfg.nvidia_smi else None
        self._nvidia_failures = 0

    def poll(self, now: Optional[float] = None, force: bool = False) -> None:
        now = time.monotonic() if now is None else now
        if not force and now - self._last < self.cfg.poll_interval:
            return
        self._last = now
        if not self.cfg.enabled:
            self.cpu_temp = self.gpu_temp = None
            return
        self.sensors = read_hwmon(self.hwmon_root)
        self.cpu_temp, self.cpu_source = pick_temperature(self.sensors, self.cfg.cpu, CPU_CHIPS)
        self.gpu_temp, self.gpu_source = pick_temperature(self.sensors, self.cfg.gpu, GPU_CHIPS)
        if self.gpu_temp is None and self.cfg.gpu.lower() in ("auto", "nvidia") and self._nvidia_smi and self._nvidia_failures < 3:
            temp = self._read_nvidia_smi()
            if temp is not None:
                self.gpu_temp, self.gpu_source = temp, "nvidia-smi"

    def _read_nvidia_smi(self) -> Optional[float]:
        try:
            result = subprocess.run(
                [self._nvidia_smi or "nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    return float(line.split(",")[0])
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        self._nvidia_failures += 1
        return None

    @property
    def max_temp(self) -> Optional[float]:
        temps = [t for t in (self.cpu_temp, self.gpu_temp) if t is not None]
        return max(temps) if temps else None

    def summary(self) -> str:
        cpu = f"{self.cpu_temp:.0f}C ({self.cpu_source})" if self.cpu_temp is not None else "n/a"
        gpu = f"{self.gpu_temp:.0f}C ({self.gpu_source})" if self.gpu_temp is not None else "n/a"
        return f"cpu {cpu}, gpu {gpu}"


class FaultMonitor:
    """Optional "hardware fault" style checks (all off by default)."""

    def __init__(self, cfg: FaultsConfig, library_paths: Callable[[], list[Path]]) -> None:
        self.cfg = cfg
        self.library_paths = library_paths
        self.faults: list[Fault] = []
        self._last = -1e9

    def poll(self, now: Optional[float] = None, force: bool = False) -> list[Fault]:
        now = time.monotonic() if now is None else now
        if not force and now - self._last < self.cfg.poll_interval:
            return self.faults
        self._last = now
        faults: list[Fault] = []
        if self.cfg.disk_space:
            faults.extend(self._check_disks())
        if self.cfg.failed_units:
            faults.extend(self._check_units())
        self.faults = faults
        return faults

    def _check_disks(self) -> list[Fault]:
        paths = [Path(os.path.expanduser(p)) for p in self.cfg.disk_paths] or self.library_paths()
        seen: set[str] = set()
        out: list[Fault] = []
        for path in paths:
            try:
                key = str(path.resolve())
                if key in seen or not path.exists():
                    continue
                seen.add(key)
                usage = shutil.disk_usage(path)
            except OSError:
                continue
            if usage.total <= 0:
                continue
            free_pct = usage.free / usage.total * 100.0
            if free_pct < self.cfg.disk_min_free_percent:
                out.append(Fault("disk", "q2", f"{path} has only {free_pct:.1f}% free"))
                break
        return out

    def _check_units(self) -> list[Fault]:
        scopes = {"system": [[]], "user": [["--user"]], "both": [[], ["--user"]]}[self.cfg.failed_units_scope]
        names: list[str] = []
        for extra in scopes:
            try:
                result = subprocess.run(
                    ["systemctl", *extra, "--failed", "--plain", "--no-legend", "--no-pager"],
                    capture_output=True, text=True, timeout=5,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for line in result.stdout.splitlines():
                parts = line.split()
                if parts:
                    names.append(parts[0].lstrip("●* "))
        if names:
            shown = ", ".join(names[:4]) + (" ..." if len(names) > 4 else "")
            return [Fault("units", "left_half", f"{len(names)} failed systemd unit(s): {shown}")]
        return []

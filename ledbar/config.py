"""Configuration loading (TOML) with defaults and validation.

The whole configuration is a tree of small dataclasses so that every option
has a documented default and a type.  Unknown keys are reported as warnings
rather than errors, so a typo never stops the LED bar from starting.
"""

from __future__ import annotations

import dataclasses
import os

try:
    import tomllib
except ImportError:  # Python 3.10: pip install tomli
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from .colors import COLOR_ORDERS, RGB, parse_hex

DEFAULT_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "ledbar"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.toml"

STEAM_BLUE = "#1a9fff"


@dataclass
class LedsConfig:
    count: int = 24                 # LEDs that make up the bar
    reverse: bool = False           # True when LED #0 sits at the RIGHT end of the bar
    offset: int = 0                 # LEDs at the start of the chain that are not part of the bar
    offset_mode: str = "off"        # what those offset LEDs do: "off" (blank) or "power_led"
                                    # (drive them as a Steam-Machine-style status indicator)
    brightness: int = 60            # maximum brightness, percent
    gamma: float = 2.2              # perceptual correction; 1.0 disables
    color_order: str = "RGB"        # swap channels if colours come out wrong
    fps: int = 30                   # frames per second sent to the controller
    keepalive_seconds: float = 0.25 # resend an unchanged frame at least this often (some controllers,
                                    # ASRock's included, fall back to their built-in effect when frames stop)

    @property
    def total(self) -> int:
        return self.count + self.offset


@dataclass
class OutputConfig:
    backend: str = "openrgb"        # openrgb | terminal | null


@dataclass
class OpenRGBConfig:
    host: str = "127.0.0.1"
    port: int = 6742
    device: str = ""                # substring of the device name, or an index; "" = auto
    zone: str = ""                  # zone name substring or index; "" = first addressable zone
    set_zone_size: bool = True      # resize the zone to leds.count + leds.offset
    require_direct: bool = True     # refuse devices without a "Direct" mode (protects flash memory)
    whole_strip: bool = False       # "basic" mode: drive the whole bar with the controller's own
                                    # hardware effects (Static/Breathing) instead of per-LED. Use this
                                    # when Direct mode does not work on your board (no progress fill).
    client_name: str = "ledbar"
    reconnect_seconds: float = 1.0   # first retry delay; doubles up to 30 s
    command: str = ""               # OpenRGB binary/AppImage for the ledbar-openrgb service; "" = auto


@dataclass
class ColorsConfig:
    bar: str = STEAM_BLUE           # default colour: idle, boot and progress
    boot: str = ""                  # "" = same as bar
    complete: str = ""              # "" = same as bar
    error: str = "#ff1a1a"
    warning: str = "#ff8c00"
    white: str = "#ffffff"
    track: str = "#000000"          # the unfilled part of the progress bar
    game: str = ""                  # "" = same as bar
    gauge_cool: str = "#1a9fff"
    gauge_hot: str = "#ff1a1a"


@dataclass
class BootConfig:
    enabled: bool = True
    wait_for_steam: bool = True     # keep breathing until the Steam client is running
    timeout_seconds: float = 90.0   # ... but never longer than this
    min_seconds: float = 2.0
    period_seconds: float = 3.0


@dataclass
class IdleConfig:
    mode: str = "solid"             # solid | off | breathe | temperature
    brightness: int = 100           # percent of the maximum brightness
    breathe_period: float = 4.0
    temperature_max_c: float = 95.0 # "temperature" mode: bar is full at this CPU temperature


@dataclass
class GameConfig:
    mode: str = "same"              # same | off | dim | solid   (while a Steam game runs)
    brightness: int = 30            # for "dim"
    color: str = ""                 # for "solid"; "" = colors.game / colors.bar


@dataclass
class ProgressConfig:
    enabled: bool = True
    smoothing_seconds: float = 0.8  # how quickly the fill eases to the new value
    head_glow: bool = True          # soft leading edge instead of a hard one
    show_paused: bool = False       # keep the (dimmed) bar while a download is paused
    paused_brightness: int = 35
    complete_flash: bool = True     # short pulse when a download finishes
    complete_seconds: float = 1.5
    stale_seconds: float = 120.0    # ignore manifests untouched for this long (unless a downloading dir exists)
    indeterminate: str = "breathe"  # breathe | off   (when Steam gives no byte counts)


@dataclass
class SteamConfig:
    paths: list[str] = field(default_factory=list)  # extra Steam roots to scan
    poll_interval: float = 1.0
    require_process: bool = True    # only report downloads while a Steam process exists


@dataclass
class ThermalConfig:
    enabled: bool = True
    critical_c: float = 100.0       # solid red at/above this (Steam Machine: CPU or GPU > 100 C)
    warning_c: float = 0.0          # amber breathing at/above this; 0 disables
    hysteresis_c: float = 5.0
    cpu: str = "auto"               # hwmon chip name, optionally "chip:label"
    gpu: str = "auto"
    poll_interval: float = 2.0
    nvidia_smi: bool = True         # allow calling nvidia-smi when no hwmon GPU sensor exists


@dataclass
class FaultsConfig:
    disk_space: bool = False        # Steam library nearly full -> second quadrant breathing red
    disk_min_free_percent: float = 2.0
    disk_paths: list[str] = field(default_factory=list)  # "" = Steam libraries
    failed_units: bool = False      # failed systemd units -> left half breathing red
    failed_units_scope: str = "system"  # system | user | both
    poll_interval: float = 30.0


@dataclass
class PowerConfig:
    monitor: bool = True            # watch logind for sleep/shutdown
    sleep: str = "off"              # off | dim | hold   (what the bar does while suspended)
    sleep_brightness: int = 10
    on_exit: str = "mode:Breathing" # off | hold | mode:<OpenRGB mode name>
    fade_seconds: float = 0.4
    resume_boot: bool = False       # replay the boot animation after resume


@dataclass
class IndicatorConfig:
    color: str = "#ffffff"          # normal indicator colour (Steam Machine indicator = white)
    fault_color: str = ""           # overheat/fault colour; "" = [colors] error
    brightness: int = 100           # percent of [leds] brightness for the indicator LEDs
    sleep: str = "solid"            # what the indicator shows going into sleep: "solid" | "off"
                                    # (a strip that holds its last frame keeps showing this during sleep)


@dataclass
class Config:
    leds: LedsConfig = field(default_factory=LedsConfig)
    indicator: IndicatorConfig = field(default_factory=IndicatorConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    openrgb: OpenRGBConfig = field(default_factory=OpenRGBConfig)
    colors: ColorsConfig = field(default_factory=ColorsConfig)
    boot: BootConfig = field(default_factory=BootConfig)
    idle: IdleConfig = field(default_factory=IdleConfig)
    game: GameConfig = field(default_factory=GameConfig)
    progress: ProgressConfig = field(default_factory=ProgressConfig)
    steam: SteamConfig = field(default_factory=SteamConfig)
    thermal: ThermalConfig = field(default_factory=ThermalConfig)
    faults: FaultsConfig = field(default_factory=FaultsConfig)
    power: PowerConfig = field(default_factory=PowerConfig)
    log_level: str = "info"
    path: Optional[Path] = None
    warnings: list[str] = field(default_factory=list)

    # -- colours -----------------------------------------------------------

    def rgb(self, name: str) -> RGB:
        """Resolve a named colour from ``[colors]`` honouring the fallbacks."""
        colors = self.colors
        value = getattr(colors, name, "")
        if not value:
            if name == "game" and self.game.color:
                value = self.game.color
            else:
                value = colors.bar
        return parse_hex(value)


_SECTIONS = {
    "leds": LedsConfig,
    "indicator": IndicatorConfig,
    "output": OutputConfig,
    "openrgb": OpenRGBConfig,
    "colors": ColorsConfig,
    "boot": BootConfig,
    "idle": IdleConfig,
    "game": GameConfig,
    "progress": ProgressConfig,
    "steam": SteamConfig,
    "thermal": ThermalConfig,
    "faults": FaultsConfig,
    "power": PowerConfig,
}


class ConfigError(ValueError):
    pass


def _coerce(name: str, value: Any, target_type: Any, section: str) -> Any:
    """Convert a TOML value to the dataclass field type, tolerantly."""
    origin = getattr(target_type, "__origin__", None)
    if target_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "yes", "no", "on", "off"):
            return value.lower() in ("true", "yes", "on")
        raise ConfigError(f"[{section}] {name} must be true or false")
    if target_type is int:
        if isinstance(value, bool):
            raise ConfigError(f"[{section}] {name} must be a number")
        if isinstance(value, (int, float)):
            return int(value)
        raise ConfigError(f"[{section}] {name} must be a number")
    if target_type is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"[{section}] {name} must be a number")
        return float(value)
    if target_type is str:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if not isinstance(value, str):
            raise ConfigError(f"[{section}] {name} must be a string")
        return value
    if origin is list or target_type is list:
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            raise ConfigError(f"[{section}] {name} must be a list")
        return [str(v) for v in value]
    return value


def _build_section(cls: type, table: dict[str, Any], section: str, warnings: list[str]) -> Any:
    kwargs: dict[str, Any] = {}
    known = {f.name: f for f in fields(cls)}
    for key, value in table.items():
        key_norm = key.replace("-", "_")
        if key_norm not in known:
            warnings.append(f"[{section}] unknown option '{key}' ignored")
            continue
        kwargs[key_norm] = _coerce(key, value, known[key_norm].type, section)
    return cls(**kwargs)


def _resolve_type_hints(cls: type) -> None:
    """dataclass field types are strings under ``from __future__ import annotations``."""
    hints = {}
    module_globals = vars(__import__(cls.__module__, fromlist=["*"]))
    for f in fields(cls):
        if isinstance(f.type, str):
            hints[f.name] = eval(f.type, module_globals)  # noqa: S307 - trusted source (this file)
        else:
            hints[f.name] = f.type
    for f in fields(cls):
        f.type = hints[f.name]


for _cls in list(_SECTIONS.values()) + [Config]:
    _resolve_type_hints(_cls)


def config_from_dict(data: dict[str, Any], path: Optional[Path] = None) -> Config:
    warnings: list[str] = []
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in _SECTIONS:
            if not isinstance(value, dict):
                raise ConfigError(f"[{key}] must be a table")
            kwargs[key] = _build_section(_SECTIONS[key], value, key, warnings)
        elif key == "log_level":
            kwargs["log_level"] = str(value)
        else:
            warnings.append(f"unknown top-level option '{key}' ignored")
    config = Config(**kwargs)
    config.path = path
    config.warnings = warnings
    validate(config)
    return config


def load_config(path: Optional[str | Path] = None) -> Config:
    """Load the TOML config; a missing file simply yields the defaults."""
    if path is None:
        env_path = os.environ.get("LEDBAR_CONFIG")
        path = Path(env_path).expanduser() if env_path else DEFAULT_CONFIG_PATH
    path = Path(path).expanduser()
    if not path.is_file():
        config = Config()
        config.path = path
        validate(config)
        return config
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    return config_from_dict(data, path)


def _check_choice(value: str, choices: tuple[str, ...], where: str) -> None:
    if value not in choices:
        raise ConfigError(f"{where} must be one of {', '.join(choices)} (got {value!r})")


def validate(config: Config) -> None:
    leds = config.leds
    if leds.count < 1 or leds.count > 1000:
        raise ConfigError("[leds] count must be between 1 and 1000")
    if leds.offset < 0 or leds.offset > 1000:
        raise ConfigError("[leds] offset must be between 0 and 1000")
    if not 0 <= leds.brightness <= 100:
        raise ConfigError("[leds] brightness must be between 0 and 100")
    if not 0.5 <= leds.gamma <= 4.0:
        raise ConfigError("[leds] gamma must be between 0.5 and 4.0")
    leds.color_order = leds.color_order.upper()
    if leds.color_order not in COLOR_ORDERS:
        raise ConfigError(f"[leds] color_order must be one of {', '.join(COLOR_ORDERS)}")
    _check_choice(leds.offset_mode, ("off", "power_led"), "[leds] offset_mode")
    if leds.offset_mode == "power_led" and leds.offset < 1:
        config.warnings.append("[leds] offset_mode = \"power_led\" but offset = 0, so there are no indicator LEDs")
    if not 1 <= leds.fps <= 120:
        raise ConfigError("[leds] fps must be between 1 and 120")
    _check_choice(config.output.backend, ("openrgb", "terminal", "null"), "[output] backend")
    _check_choice(config.idle.mode, ("solid", "off", "breathe", "temperature"), "[idle] mode")
    _check_choice(config.game.mode, ("same", "off", "dim", "solid"), "[game] mode")
    _check_choice(config.progress.indeterminate, ("breathe", "off"), "[progress] indeterminate")
    _check_choice(config.power.sleep, ("off", "dim", "hold"), "[power] sleep")
    _check_choice(config.faults.failed_units_scope, ("system", "user", "both"), "[faults] failed_units_scope")
    _check_choice(config.indicator.sleep, ("solid", "off"), "[indicator] sleep")
    if not 0 <= config.indicator.brightness <= 100:
        raise ConfigError("[indicator] brightness must be between 0 and 100")
    for cname in ("color", "fault_color"):
        value = getattr(config.indicator, cname)
        if value or cname == "color":
            try:
                parse_hex(value)
            except ValueError as exc:
                raise ConfigError(f"[indicator] {cname}: {exc}") from exc
    if not (config.power.on_exit in ("off", "hold") or config.power.on_exit.startswith("mode:")):
        raise ConfigError("[power] on_exit must be 'off', 'hold' or 'mode:<OpenRGB mode name>'")
    for pct_name, pct in (("[idle] brightness", config.idle.brightness),
                          ("[game] brightness", config.game.brightness),
                          ("[progress] paused_brightness", config.progress.paused_brightness),
                          ("[power] sleep_brightness", config.power.sleep_brightness)):
        if not 0 <= pct <= 100:
            raise ConfigError(f"{pct_name} must be between 0 and 100")
    if config.thermal.critical_c <= 0:
        raise ConfigError("[thermal] critical_c must be positive")
    for name in ("bar", "error", "warning", "white", "track", "gauge_cool", "gauge_hot", "boot", "complete", "game"):
        value = getattr(config.colors, name)
        if value or name in ("bar", "error", "warning", "white", "track", "gauge_cool", "gauge_hot"):
            try:
                parse_hex(value)
            except ValueError as exc:
                raise ConfigError(f"[colors] {name}: {exc}") from exc
    if config.game.color:
        try:
            parse_hex(config.game.color)
        except ValueError as exc:
            raise ConfigError(f"[game] color: {exc}") from exc
    if not 1 <= config.openrgb.port <= 65535:
        raise ConfigError("[openrgb] port must be between 1 and 65535")
    config.log_level = config.log_level.lower()
    _check_choice(config.log_level, ("debug", "info", "warning", "error"), "log_level")


def as_dict(config: Config) -> dict[str, Any]:
    """Plain dict view (used by ``ledbar config show``)."""
    out: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        out[name] = dataclasses.asdict(getattr(config, name))
    out["log_level"] = config.log_level
    return out

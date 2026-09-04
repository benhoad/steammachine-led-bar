"""Command line interface: ``ledbar run|status|identify|demo|config``."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError, as_dict, load_config

log = logging.getLogger("ledbar")

EXAMPLE_CONFIG = Path(__file__).resolve().parent / "config.example.toml"


def _setup_paths() -> None:
    """Allow a vendored copy of openrgb-python next to the package."""
    candidates = [os.environ.get("LEDBAR_VENDOR"), str(Path(__file__).resolve().parent.parent / "vendor")]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir() and candidate not in sys.path:
            sys.path.append(candidate)


def _setup_logging(level: str, quiet_tty: bool) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    if quiet_tty and numeric < logging.WARNING:
        numeric = logging.WARNING
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s" if not sys.stderr.isatty() else "%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _load(args: argparse.Namespace) -> Config:
    config = load_config(args.config)
    if args.backend:
        config.output.backend = args.backend
    _apply_env_overrides(config)
    return config


def _apply_env_overrides(config: Config) -> None:
    """A few environment overrides that make testing easier."""
    paths = os.environ.get("LEDBAR_STEAM_PATHS")
    if paths:
        config.steam.paths = [p for p in paths.split(":") if p] + list(config.steam.paths)
    require = os.environ.get("LEDBAR_REQUIRE_STEAM_PROCESS")
    if require is not None:
        config.steam.require_process = require.lower() in ("1", "true", "yes")
    backend = os.environ.get("LEDBAR_BACKEND")
    if backend:
        config.output.backend = backend


def _service_active(unit: str = "ledbar.service") -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@contextlib.contextmanager
def _service_paused(backend_name: str):
    """Stop the ledbar service while a manual command drives the strip, then start it again.

    Otherwise the service keeps resending its own frames and wipes the test
    pattern within a couple of seconds.
    """
    paused = False
    if backend_name == "openrgb" and _service_active():
        print("Pausing the ledbar service while this runs ...")
        subprocess.run(["systemctl", "--user", "stop", "ledbar.service"], check=False)
        paused = True
    try:
        yield
    finally:
        if paused:
            subprocess.run(["systemctl", "--user", "start", "ledbar.service"], check=False)
            print("ledbar service resumed.")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args)
    from .backends import create_backend
    from .daemon import Daemon, LiveInputs

    backend = create_backend(config)
    _setup_logging(args.log_level or config.log_level, quiet_tty=backend.name == "terminal" and sys.stdout.isatty() and not args.log_level)
    for warning in config.warnings:
        log.warning("config: %s", warning)
    log.info("ledbar %s starting (config %s)", __version__, config.path)
    daemon = Daemon(config, backend, LiveInputs(config))
    return daemon.run(max_seconds=args.seconds)


def cmd_demo(args: argparse.Namespace) -> int:
    config = _load(args)
    from .backends import create_backend
    from .daemon import Daemon
    from .demo import STATE_NAMES, ScriptedInputs, default_script, make_single_state

    if args.list:
        print("\n".join(STATE_NAMES))
        return 0
    backend = create_backend(config)
    _setup_logging(args.log_level or config.log_level, quiet_tty=backend.name == "terminal" and sys.stdout.isatty() and not args.log_level)
    if args.state:
        steps = make_single_state(config, args.state, args.seconds or 10.0)
    else:
        steps = default_script(config)
    inputs = ScriptedInputs(config, steps, loop=args.loop)
    print("Demo: " + " -> ".join(f"{s.name} {s.seconds:.0f}s" for s in steps) + ("  (looping)" if args.loop else ""))
    daemon = Daemon(config, backend, inputs)
    # stop automatically once the script has finished (unless looping)
    total = None if args.loop else inputs.total + 0.5
    with _service_paused(backend.name):
        return daemon.run(max_seconds=total)


def cmd_identify(args: argparse.Namespace) -> int:
    config = _load(args)
    from .backends import create_backend
    from .colors import to_rgb8

    backend = create_backend(config)
    _setup_logging(args.log_level or config.log_level, quiet_tty=False)
    total = config.leds.total
    brightness = config.leds.brightness / 100.0
    bar = to_rgb8(config.rgb("bar"), config.leds.gamma, brightness)
    white = to_rgb8((1.0, 1.0, 1.0), config.leds.gamma, brightness)
    red = to_rgb8((1, 0, 0), 1.0, brightness)
    green = to_rgb8((0, 1, 0), 1.0, brightness)
    blue = to_rgb8((0, 0, 1), 1.0, brightness)
    off = [(0, 0, 0)] * total

    with _service_paused(backend.name):
        if getattr(backend, "mode_based", False):
            return _identify_basic(backend, config)
        return _identify_sequence(backend, config, total, red, green, blue, white, bar, off)


def _identify_basic(backend, config) -> int:
    """Whole-strip mode has no per-LED control; just confirm colours and the strip."""
    import time as _time

    from .render import Scene
    from .state import Decision

    backend.open()
    if not backend.healthy:
        print("Could not connect to OpenRGB; see the messages above and `ledbar status`.")
        return 3
    print(f"Output: {backend.describe()}")
    print("Basic (whole-strip) mode: no per-LED control, so `reverse` and `offset` do not apply.")
    print("The whole bar turns RED, then GREEN, then BLUE. If a colour is wrong, fix [leds] color_order.")
    for name, rgb in (("red", (1.0, 0.0, 0.0)), ("green", (0.0, 1.0, 0.0)), ("blue", (0.0, 0.0, 1.0))):
        backend.apply_decision(Decision(Scene(kind="solid", name=f"id-{name}", color=rgb)), _time.monotonic())
        _sleep(2.5)
    print("Finally, the bar colour from your config.")
    backend.apply_decision(Decision(Scene(kind="solid", name="id-bar", color=config.rgb("bar"))), _time.monotonic())
    _sleep(3.0)
    backend.close(None, "off")
    return 0


def _identify_sequence(backend, config, total, red, green, blue, white, bar, off) -> int:
    backend.open()
    if not backend.healthy:
        print("Could not connect to OpenRGB; see the messages above and `ledbar status`.")
        return 3
    print(f"Output: {backend.describe()}")
    print(f"Chain length used: {total} LEDs (count {config.leds.count} + offset {config.leds.offset})")
    print()
    print("Step 1: the FIRST LED on the chain is RED, the second GREEN, the third BLUE.")
    print("  - If red and green are swapped, set   [leds] color_order = \"GRB\"   (and so on).")
    print("  - If the red LED is at the RIGHT-hand end of your bar, set   [leds] reverse = true")
    print("  - If LEDs before the bar light up, count them and set   [leds] offset = <n>")
    frame = list(off)
    for index, color in enumerate((red, green, blue)):
        if index < total:
            frame[index] = color
    backend.write(frame)
    _sleep(6.0)

    print("Step 2: a white dot runs from the first LED to the last (twice).")
    for _ in range(2):
        for index in range(total):
            frame = list(off)
            frame[index] = white
            backend.write(frame)
            _sleep(0.08)
    print("Step 3: every LED of the bar in the configured colour and brightness (3 seconds).")
    frame = [(0, 0, 0)] * config.leds.offset + [bar] * config.leds.count
    backend.write(frame)
    _sleep(3.0)
    backend.close(off, "off")
    print("Done. Edit your config, then run `ledbar identify` again to check.")
    return 0


def _sleep(seconds: float) -> None:
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        raise SystemExit(130)


def cmd_status(args: argparse.Namespace) -> int:
    config = _load(args)
    _setup_logging(args.log_level or "warning", quiet_tty=False)
    from .sensors import FaultMonitor, ThermalMonitor
    from .steam import SteamMonitor, describe_flags

    print(f"ledbar {__version__}")
    print(f"config: {config.path}{'' if config.path and config.path.is_file() else ' (not found, using defaults)'}")
    for warning in config.warnings:
        print(f"  warning: {warning}")
    leds = config.leds
    print(f"leds: count={leds.count} offset={leds.offset} reverse={leds.reverse} brightness={leds.brightness}% "
          f"gamma={leds.gamma} color_order={leds.color_order} fps={leds.fps}")
    print(f"backend: {config.output.backend}")
    print()

    # Steam ---------------------------------------------------------------
    monitor = SteamMonitor(
        extra_roots=config.steam.paths,
        stale_seconds=config.progress.stale_seconds,
        show_paused=config.progress.show_paused,
        require_steam_process=config.steam.require_process,
    )
    monitor.rescan_libraries()
    print("Steam:")
    if not monitor.roots:
        print("  no Steam installation found (set [steam] paths if Steam lives somewhere unusual)")
    for root in monitor.roots:
        print(f"  root: {root}")
    for library in monitor.libraries:
        print(f"  library: {library}")
    state = monitor.poll()
    print(f"  client running: {state.steam_running}")
    if state.running_appid is not None:
        print(f"  game running: app {state.running_appid}")
    if state.active and state.app is not None:
        pct = f"{state.fraction * 100:.1f}%" if state.fraction is not None else "n/a"
        print(f"  ACTIVE: {state.app.name} ({state.app.appid}) {state.phase} {pct}")
    else:
        print("  no active download")
    statuses = list(monitor._read_manifests())
    interesting = [s for s in statuses if s.flags not in (4,)]
    for status in sorted(interesting, key=lambda s: -s.mtime)[:8]:
        age = time.time() - status.mtime
        print(f"  {status.appid:>8} {status.name[:32]:<32} flags={status.flags} ({describe_flags(status.flags)}) "
              f"dl {status.bytes_downloaded}/{status.bytes_to_download} stage {status.bytes_staged}/{status.bytes_to_stage} "
              f"age {age:.0f}s{' downloading/' if status.downloading_dir else ''}")
    print(f"  manifests scanned: {len(statuses)}")
    print()

    # Sensors --------------------------------------------------------------
    thermal = ThermalMonitor(config.thermal)
    thermal.poll(force=True)
    print(f"Temperatures: {thermal.summary()}")
    print(f"  thresholds: critical {config.thermal.critical_c:.0f}C, warning "
          f"{'off' if config.thermal.warning_c <= 0 else f'{config.thermal.warning_c:.0f}C'}")
    for sensor in thermal.sensors[:40]:
        print(f"  {sensor}")
    if not thermal.sensors:
        print("  no hwmon sensors found")
    faults = FaultMonitor(config.faults, lambda: list(monitor.libraries)).poll(force=True)
    print(f"Faults: {', '.join(f.message for f in faults) if faults else 'none'}"
          f" (disk_space={'on' if config.faults.disk_space else 'off'}, failed_units={'on' if config.faults.failed_units else 'off'})")
    tool = "gdbus" if shutil.which("gdbus") else "busctl" if shutil.which("busctl") else "none"
    print(f"Sleep/shutdown detection: {tool}")
    print()

    # OpenRGB --------------------------------------------------------------
    if config.output.backend == "openrgb":
        from .backends import create_backend
        from .backends.base import BackendError

        try:
            from .backends.openrgb import inspect

            print(inspect(config.openrgb))
        except BackendError as exc:
            print(f"OpenRGB: {exc}")
            return 1
        except Exception as exc:
            print(f"OpenRGB: cannot connect to {config.openrgb.host}:{config.openrgb.port} ({exc}).")
            print("  Is the SDK server running?  systemctl --user status ledbar-openrgb")
            return 1
        try:
            backend = create_backend(config)
            backend.open()
            print(f"selected: {backend.describe()}" if backend.healthy else "selected: (could not prepare device)")
            backend.close(None, "hold")
        except BackendError as exc:
            print(f"selection error: {exc}")
            return 1
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    if args.action == "path":
        print(DEFAULT_CONFIG_PATH if not args.config else Path(args.config).expanduser())
        return 0
    if args.action == "init":
        target = Path(args.config).expanduser() if args.config else DEFAULT_CONFIG_PATH
        if target.exists() and not args.force:
            print(f"{target} already exists (use --force to overwrite)")
            return 1
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(EXAMPLE_CONFIG, target)
        print(f"wrote {target}")
        return 0
    if args.action == "show":
        config = _load(args)
        print(json.dumps(as_dict(config), indent=2))
        for warning in config.warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return 0
    if args.action == "check":
        config = _load(args)
        print(f"{config.path}: OK" + (f" ({len(config.warnings)} warning(s))" if config.warnings else ""))
        for warning in config.warnings:
            print(f"  {warning}")
        return 0
    return 2


# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument("--config", "-c", default=default, help=f"config file (default {DEFAULT_CONFIG_PATH})")
    parser.add_argument("--backend", "-b", default=default, choices=["openrgb", "terminal", "null"], help="override [output] backend")
    parser.add_argument("--log-level", "-l", default=default, choices=["debug", "info", "warning", "error"], help="log verbosity")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledbar", description="Steam Machine style LED bar for Bazzite / Linux PCs.")
    _add_common(parser, suppress=False)
    parser.add_argument("--version", action="version", version=f"ledbar {__version__}")
    # the same options are accepted after the subcommand too (ledbar demo --backend terminal)
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the LED bar daemon (what the systemd service does)", parents=[common])
    run.add_argument("--seconds", type=float, default=None, help="stop after this many seconds (testing)")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show what ledbar sees: Steam, sensors, OpenRGB devices", parents=[common])
    status.set_defaults(func=cmd_status)

    identify = sub.add_parser("identify", help="light LEDs to work out direction, colour order and offset", parents=[common])
    identify.set_defaults(func=cmd_identify)

    demo = sub.add_parser("demo", help="cycle through every state (or one state with --state)", parents=[common])
    demo.add_argument("--state", "-s", help="show a single state (see --list)")
    demo.add_argument("--seconds", type=float, default=None, help="duration for --state (default 10)")
    demo.add_argument("--loop", action="store_true", help="repeat forever")
    demo.add_argument("--list", action="store_true", help="list demo state names")
    demo.set_defaults(func=cmd_demo)

    config = sub.add_parser("config", help="manage the config file", parents=[common])
    config.add_argument("action", choices=["init", "show", "path", "check"])
    config.add_argument("--force", action="store_true", help="overwrite an existing file (init)")
    config.set_defaults(func=cmd_config)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    _setup_paths()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - last resort, keep the message readable
        from .backends.base import BackendError

        if isinstance(exc, BackendError):
            print(f"error: {exc}", file=sys.stderr)
            return 3
        raise

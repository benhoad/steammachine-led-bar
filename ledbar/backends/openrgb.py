"""OpenRGB SDK backend (uses the ``openrgb-python`` package).

Design notes
------------
* Colours are always written with a whole-device update.  Several OpenRGB
  drivers (ASRock Polychrome USB among them) only honour per-LED colours on
  the device-level update; the zone-level update collapses to one colour.
* The device must offer a ``Direct`` mode.  Modes such as ``Static`` on
  motherboard controllers are saved to the controller's flash memory on
  every update, which would wear it out at 30 frames per second.
* The addressable zone is resized to the configured LED count so that the
  controller clocks out exactly as many pixels as the strip has.
* The connection is re-established automatically if OpenRGB restarts.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from ..colors import RGB, RGB8, clamp01, lerp, reorder, scale, to_rgb8
from ..config import OpenRGBConfig
from .base import Backend, BackendError

log = logging.getLogger("ledbar.openrgb")

ADDRESSABLE_RE = re.compile(r"addressable|argb|led strip|strip|d-?rgb|jrainbow|gen ?2", re.IGNORECASE)


def _import_openrgb() -> tuple[Any, Any, Any]:
    try:
        from openrgb import OpenRGBClient
        from openrgb.utils import DeviceType, RGBColor
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise BackendError(
            "the 'openrgb-python' package is not installed. Run install.sh, or "
            "'pip install openrgb-python', or set [output] backend = \"terminal\" to simulate.",
            fatal=True,
        ) from exc
    return OpenRGBClient, DeviceType, RGBColor


class OpenRGBBackend(Backend):
    name = "openrgb"

    def __init__(self, cfg: OpenRGBConfig, total: int) -> None:
        super().__init__(total)
        self.cfg = cfg
        self.client: Any = None
        self.device: Any = None
        self.zone: Any = None
        self.device_index = -1
        self.zone_index = -1
        self.start_index = 0
        self._device_colors: list[Any] = []
        self._connected = False
        self._next_attempt = 0.0
        self._failures = 0
        self._last_error = ""
        self._RGBColor: Any = None
        self.direct_mode_name = ""

    # -- connection --------------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._connected

    @property
    def last_error(self) -> str:
        return self._last_error

    def open(self) -> None:
        try:
            self._connect()
        except BackendError as exc:
            if exc.fatal:
                raise
            self._note_failure(str(exc))
        except Exception as exc:  # server not up yet, etc.
            self._note_failure(f"cannot connect to OpenRGB at {self.cfg.host}:{self.cfg.port}: {exc}")

    def _note_failure(self, message: str) -> None:
        self._connected = False
        self._failures += 1
        delay = min(self.cfg.reconnect_seconds * (2 ** min(self._failures - 1, 5)), 30.0)
        self._next_attempt = time.monotonic() + delay
        if message != self._last_error:
            log.warning("%s (retrying in %.0fs)", message, delay)
            self._last_error = message
        self._close_client()

    def _close_client(self) -> None:
        if self.client is not None:
            try:
                self.client.disconnect()
            except Exception:
                pass
        self.client = None
        self.device = None
        self.zone = None

    def _connect(self) -> None:
        OpenRGBClient, DeviceType, RGBColor = _import_openrgb()
        self._RGBColor = RGBColor
        client = OpenRGBClient(self.cfg.host, self.cfg.port, self.cfg.client_name)
        try:
            devices = list(client.devices)
            if not devices:
                raise BackendError(
                    "OpenRGB reports no devices yet - it may still be detecting hardware, "
                    "or the controller is not visible (udev rules, i2c)."
                )
            self.device_index = self._select_device(devices, DeviceType)
            device = devices[self.device_index]
            self.zone_index = self._select_zone(device)
            self._prepare(client, device)
        except Exception:
            try:
                client.disconnect()
            except Exception:
                pass
            raise
        self.client = client
        self._connected = True
        self._failures = 0
        self._last_error = ""
        log.info("connected: %s", self.describe())

    def _prepare(self, client: Any, device: Any) -> None:
        # 1. Direct mode ------------------------------------------------------
        mode_names = [m.name for m in device.modes]
        direct = next((m for m in device.modes if m.name.lower() == "direct"), None)
        if direct is None:
            if self.cfg.require_direct:
                raise BackendError(
                    f"device '{device.name}' has no 'Direct' mode (modes: {', '.join(mode_names)}). "
                    "Per-LED animation needs Direct mode; older SMBus ASRock boards save every "
                    "update to flash and are not suitable. Set [openrgb] require_direct = false to override."
                )
            log.warning("device '%s' has no Direct mode; using custom mode (may write to flash!)", device.name)
            device.set_custom_mode()
        else:
            self.direct_mode_name = direct.name
            if device.active_mode != direct.id:
                device.set_mode(direct.name)
        device = client.devices[self.device_index]

        # 2. Zone size ----------------------------------------------------------
        device, zone = self._ensure_zone_size(client, device)
        self.device = device
        self.zone = zone
        self.start_index = zone.leds[0].id
        self._device_colors = [self._RGBColor(c.red, c.green, c.blue) for c in device.colors]

    def _ensure_zone_size(self, client: Any, device: Any) -> tuple[Any, Any]:
        """Resize the selected zone to ``total`` LEDs and validate it."""
        zone = device.zones[self.zone_index]
        if self.cfg.set_zone_size and len(zone.leds) != self.total:
            zone_data = device.data.zones[self.zone_index]
            if zone_data.leds_min == zone_data.leds_max and zone_data.leds_max != self.total:
                raise BackendError(
                    f"zone '{zone.name}' has a fixed size of {zone_data.leds_max} LEDs; "
                    f"set [leds] count/offset to match or pick another zone."
                )
            if self.total > zone_data.leds_max:
                raise BackendError(
                    f"zone '{zone.name}' supports at most {zone_data.leds_max} LEDs, "
                    f"but [leds] count + offset = {self.total}."
                )
            log.info("resizing zone '%s' from %d to %d LEDs", zone.name, len(zone.leds), self.total)
            zone.resize(self.total)
            device = client.devices[self.device_index]
            zone = device.zones[self.zone_index]
        if len(zone.leds) < self.total:
            raise BackendError(
                f"zone '{zone.name}' has {len(zone.leds)} LEDs but {self.total} are configured "
                "(enable [openrgb] set_zone_size or resize the zone in OpenRGB)."
            )
        if not zone.leds:
            raise BackendError(f"zone '{zone.name}' has no LEDs")
        return device, zone

    # -- selection ---------------------------------------------------------

    def _select_device(self, devices: list[Any], DeviceType: Any) -> int:
        spec = self.cfg.device.strip()
        if spec:
            if spec.isdigit():
                index = int(spec)
                if index >= len(devices):
                    raise BackendError(f"[openrgb] device index {index} out of range (0..{len(devices) - 1})")
                return index
            for index, device in enumerate(devices):
                if spec.lower() in device.name.lower():
                    return index
            names = ", ".join(f"{i}: {d.name}" for i, d in enumerate(devices))
            raise BackendError(f"no OpenRGB device matches '{spec}'. Devices: {names}")

        best_index, best_score = -1, -1
        for index, device in enumerate(devices):
            score = 0
            if any(m.name.lower() == "direct" for m in device.modes):
                score += 4
            if any(ADDRESSABLE_RE.search(z.name or "") for z in device.zones):
                score += 2
            if any(zd.leds_min != zd.leds_max for zd in device.data.zones):
                score += 1
            if device.type in (DeviceType.MOTHERBOARD, DeviceType.LEDSTRIP):
                score += 1
            if score > best_score:
                best_index, best_score = index, score
        if best_index < 0:
            raise BackendError("no usable OpenRGB device found")
        return best_index

    def _select_zone(self, device: Any) -> int:
        zones = list(device.zones)
        if not zones:
            raise BackendError(f"device '{device.name}' has no zones")
        spec = self.cfg.zone.strip()
        if spec:
            if spec.isdigit():
                index = int(spec)
                if index >= len(zones):
                    raise BackendError(f"[openrgb] zone index {index} out of range (0..{len(zones) - 1})")
                return index
            for index, zone in enumerate(zones):
                if spec.lower() in (zone.name or "").lower():
                    return index
            names = ", ".join(f"{i}: {z.name}" for i, z in enumerate(zones))
            raise BackendError(f"no zone on '{device.name}' matches '{spec}'. Zones: {names}")
        for index, zone in enumerate(zones):
            if ADDRESSABLE_RE.search(zone.name or ""):
                return index
        for index, zone_data in enumerate(device.data.zones):
            if zone_data.leds_min != zone_data.leds_max:
                return index
        return 0

    # -- output ------------------------------------------------------------

    def write(self, frame: list[RGB8]) -> bool:
        if not self._connected:
            if time.monotonic() < self._next_attempt:
                return False
            try:
                self._connect()
            except BackendError as exc:
                if exc.fatal:
                    raise
                self._note_failure(str(exc))
                return False
            except Exception as exc:
                self._note_failure(f"cannot connect to OpenRGB at {self.cfg.host}:{self.cfg.port}: {exc}")
                return False
        try:
            colors = self._device_colors
            RGBColor = self._RGBColor
            start = self.start_index
            for offset, (r, g, b) in enumerate(frame[: self.total]):
                colors[start + offset] = RGBColor(r, g, b)
            self.device.set_colors(colors, fast=True)
            return True
        except Exception as exc:
            self._note_failure(f"lost connection to OpenRGB: {exc}")
            return False

    def resync(self) -> None:
        """Force a reconnect (after resume the controller may have been reset)."""
        self._close_client()
        self._connected = False
        self._next_attempt = 0.0

    def close(self, final: Optional[list[RGB8]], on_exit: str = "off") -> None:
        if self._connected and self.device is not None:
            try:
                if on_exit.startswith("mode:"):
                    if not self._hand_over_to_mode(on_exit[5:].strip(), final):
                        self.write([(0, 0, 0)] * self.total)
                elif on_exit == "hold":
                    pass
                elif final is not None:
                    self.write(final)
            except Exception as exc:
                log.warning("error while closing: %s", exc)
        self._close_client()
        self._connected = False

    def _hand_over_to_mode(self, mode_name: str, final: Optional[list[RGB8]]) -> bool:
        """Switch the controller to one of its own modes (e.g. Breathing) in the bar colour."""
        available = [m.name for m in self.device.modes]
        if not any(m.lower() == mode_name.lower() for m in available):
            log.warning("controller has no '%s' mode (modes: %s); switching the strip off instead",
                        mode_name, ", ".join(available))
            return False
        try:
            self.device.set_mode(mode_name)
        except Exception as exc:
            log.warning("could not switch to mode '%s': %s; switching the strip off instead", mode_name, exc)
            # set_mode may have half-applied; make sure we are back in Direct before writing black
            try:
                if self.direct_mode_name:
                    self.device.set_mode(self.direct_mode_name)
            except Exception:
                pass
            return False
        device = self.client.devices[self.device_index]
        if final is not None:
            colors = [self._RGBColor(c.red, c.green, c.blue) for c in device.colors]
            for offset, (r, g, b) in enumerate(final[: self.total]):
                colors[self.start_index + offset] = self._RGBColor(r, g, b)
            try:
                device.set_colors(colors, fast=True)
            except Exception as exc:
                log.debug("could not set colours for mode %s: %s", mode_name, exc)
        log.info("left controller in hardware mode '%s'", mode_name)
        return True

    def describe(self) -> str:
        if self.device is None or self.zone is None:
            return f"OpenRGB at {self.cfg.host}:{self.cfg.port} (not connected)"
        return (
            f"OpenRGB device {self.device_index} '{self.device.name}', zone {self.zone_index} "
            f"'{self.zone.name}' ({len(self.zone.leds)} LEDs, mode {self.direct_mode_name or 'custom'})"
        )


# The controller's own hardware effects we map ledbar states onto, in order of
# preference.  All ASRock Polychrome USB boards have Static; the rest are used
# when present and fall back to Static.
_EFFECT_CANDIDATES = {
    "static": ["Static", "Direct"],
    "breathe": ["Breathing", "Breath", "Static"],
    "strobe": ["Strobe", "Flashing", "Blink", "Breathing", "Static"],
    "off": ["Off"],
}


class OpenRGBModeBackend(OpenRGBBackend):
    """"Basic" whole-strip output for controllers without a usable Direct mode.

    Instead of streaming per-LED frames, it maps each ledbar state onto one of
    the controller's built-in hardware effects (solid, breathing, ...) and a
    single colour, and only writes when that changes.  There is no progress
    fill, but it is rock solid on boards where Direct mode flashes (many ASRock
    Polychrome USB motherboards).
    """

    name = "openrgb-basic"
    mode_based = True

    def __init__(self, cfg: OpenRGBConfig, total: int, brightness: float = 1.0, color_order: str = "RGB") -> None:
        super().__init__(cfg, total)
        self.brightness = clamp01(brightness)
        self.color_order = color_order.upper()
        self._mode_names: dict[str, str] = {}
        self._active_mode: str = ""
        self._last_target: tuple = ()

    # -- preparation -------------------------------------------------------

    def _prepare(self, client: Any, device: Any) -> None:
        available = {m.name.lower(): m.name for m in device.modes}
        if "static" not in available and "direct" not in available:
            raise BackendError(
                f"device '{device.name}' has no Static mode; whole-strip (basic) mode needs one "
                f"(modes: {', '.join(m.name for m in device.modes)})."
            )
        self._mode_names = available
        device, zone = self._ensure_zone_size(client, device)
        self.device = device
        self.zone = zone
        self.start_index = zone.leds[0].id
        self._device_colors = [self._RGBColor(c.red, c.green, c.blue) for c in device.colors]
        self._active_mode = ""
        self._last_target = ()

    def _mode_for(self, key: str) -> str:
        for candidate in _EFFECT_CANDIDATES.get(key, ["Static"]):
            actual = self._mode_names.get(candidate.lower())
            if actual:
                return actual
        return self._mode_names.get("static") or next(iter(self._mode_names.values()))

    # -- state -> (effect, colour) ----------------------------------------

    def _target(self, decision: Any) -> tuple[str, RGB]:
        scene = decision.scene
        kind = scene.kind
        level = clamp01(getattr(scene, "level", 1.0))
        color = getattr(scene, "color", (0.0, 0.0, 0.0))
        if kind == "off" or level <= 0.0:
            return "off", (0.0, 0.0, 0.0)
        if kind == "solid":
            return "static", scale(color, level)
        if kind == "pulse":
            return "static", color
        if kind in ("breathe", "segment_breathe"):
            # faults lose their segment but keep the breathing error colour
            return "breathe", scale(color, level)
        if kind == "blink":
            return "strobe", scale(color, level)
        if kind == "fill":
            # no per-LED fill available: show "busy" as a breathing bar
            return "breathe", scale(color, level)
        if kind == "gauge":
            hot = getattr(scene, "color2", None) or color
            fraction = clamp01(getattr(scene, "fraction", 0.0) or 0.0)
            return "static", scale(lerp(color, hot, fraction), level)
        return "static", scale(color, level)

    # -- output ------------------------------------------------------------

    def write(self, frame: list[RGB8]) -> bool:  # pragma: no cover - never used
        return self._connected

    def apply_decision(self, decision: Any, now: float) -> bool:
        if not self._connected:
            import time as _time

            if _time.monotonic() < self._next_attempt:
                return False
            try:
                self._connect()
            except BackendError as exc:
                if exc.fatal:
                    raise
                self._note_failure(str(exc))
                return False
            except Exception as exc:
                self._note_failure(f"cannot connect to OpenRGB at {self.cfg.host}:{self.cfg.port}: {exc}")
                return False

        key, rgb = self._target(decision)
        rgb8 = to_rgb8(rgb, gamma=1.0, brightness=self.brightness)
        if self.color_order != "RGB":
            rgb8 = reorder(rgb8, self.color_order)
        target = (key, rgb8)
        if target == self._last_target:
            return True
        try:
            mode_name = "Off" if (key == "off" and "off" in self._mode_names) else self._mode_for(key)
            if mode_name != self._active_mode:
                self.device.set_mode(mode_name)
                self.device = self.client.devices[self.device_index]
                self._active_mode = mode_name
                self._device_colors = [self._RGBColor(c.red, c.green, c.blue) for c in self.device.colors]
            if not (key == "off" and mode_name == "Off"):
                colors = self._device_colors
                RGBColor = self._RGBColor
                for offset in range(self.total):
                    colors[self.start_index + offset] = RGBColor(*rgb8)
                self.device.set_colors(colors, fast=True)
            self._last_target = target
            return True
        except Exception as exc:
            self._note_failure(f"lost connection to OpenRGB: {exc}")
            return False

    def resync(self) -> None:
        super().resync()
        self._active_mode = ""
        self._last_target = ()

    def describe(self) -> str:
        if self.device is None or self.zone is None:
            return f"OpenRGB (basic/whole-strip) at {self.cfg.host}:{self.cfg.port} (not connected)"
        return (
            f"OpenRGB (basic/whole-strip) device {self.device_index} '{self.device.name}', "
            f"zone {self.zone_index} '{self.zone.name}' ({len(self.zone.leds)} LEDs, hardware effects)"
        )


def inspect(cfg: OpenRGBConfig) -> str:
    """Human readable dump of the OpenRGB device tree (for ``ledbar status``)."""
    OpenRGBClient, DeviceType, _ = _import_openrgb()
    client = OpenRGBClient(cfg.host, cfg.port, cfg.client_name + "-status")
    lines = [f"OpenRGB server {cfg.host}:{cfg.port} (protocol {client.protocol_version})"]
    try:
        for index, device in enumerate(client.devices):
            modes = ", ".join(m.name for m in device.modes)
            active = device.modes[device.active_mode].name if device.modes else "?"
            lines.append(f"  device {index}: {device.name}  [type {device.type.name}, active mode {active}]")
            lines.append(f"    modes: {modes}")
            for zone_index, zone in enumerate(device.zones):
                zone_data = device.data.zones[zone_index]
                size = f"{len(zone.leds)} LEDs"
                if zone_data.leds_min != zone_data.leds_max:
                    size += f" (resizable {zone_data.leds_min}-{zone_data.leds_max})"
                lines.append(f"    zone {zone_index}: {zone.name}  {size}")
    finally:
        client.disconnect()
    return "\n".join(lines)

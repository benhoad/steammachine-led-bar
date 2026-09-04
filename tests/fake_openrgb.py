"""A tiny fake OpenRGB SDK server, enough to exercise the OpenRGB backend.

It speaks the OpenRGB network protocol (version 4) for the handful of
packets openrgb-python uses and mimics the behaviour of the ASRock
Polychrome USB driver: resizable addressable zones, a Direct mode, and a
record of every colour update it receives.
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Optional

from openrgb.utils import (
    ControllerData, DeviceType, LEDData, MetaData, ModeColors, ModeData, ModeFlags, PacketType, RGBColor, ZoneData,
    ZoneType,
)

HEADER = struct.Struct("<4sIII")
PROTOCOL_VERSION = 4


@dataclass
class FakeZone:
    name: str
    leds_min: int
    leds_max: int
    count: int


@dataclass
class FakeDevice:
    name: str
    zones: list[FakeZone]
    mode_names: list[str]
    device_type: DeviceType = DeviceType.MOTHERBOARD
    active_mode: int = 0
    colors: list[tuple[int, int, int]] = field(default_factory=list)
    device_updates: int = 0
    zone_updates: list[tuple[int, list[tuple[int, int, int]]]] = field(default_factory=list)
    resizes: list[tuple[int, int]] = field(default_factory=list)
    mode_history: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._sync_colors()

    @property
    def led_count(self) -> int:
        return sum(z.count for z in self.zones)

    def _sync_colors(self) -> None:
        n = self.led_count
        if len(self.colors) < n:
            self.colors += [(0, 0, 0)] * (n - len(self.colors))
        else:
            del self.colors[n:]

    def resize(self, zone_index: int, size: int) -> None:
        zone = self.zones[zone_index]
        size = max(zone.leds_min, min(zone.leds_max, size))
        zone.count = size
        self.resizes.append((zone_index, size))
        self._sync_colors()

    def modes(self) -> list[ModeData]:
        modes = []
        for index, name in enumerate(self.mode_names):
            flags = ModeFlags.HAS_PER_LED_COLOR
            speed_min = speed_max = speed = None
            if name == "Breathing":
                flags |= ModeFlags.HAS_SPEED
                speed_min, speed_max, speed = 0, 5, 2
            modes.append(ModeData(index, name, index if name != "Direct" else 15, flags, speed_min, speed_max,
                                  None, None, None, None, speed, None, None, ModeColors.PER_LED, None))
        return modes

    def controller_data(self, version: int) -> bytes:
        leds: list[LEDData] = []
        zones: list[ZoneData] = []
        for zone_index, zone in enumerate(self.zones):
            zone_leds = [LEDData(f"{zone.name} LED {i}", zone_index) for i in range(zone.count)]
            leds.extend(zone_leds)
            zones.append(ZoneData(zone.name, ZoneType.LINEAR, zone.leds_min, zone.leds_max, zone.count, 0, 0, None, [], zone_leds, [], 0))
        colors = [RGBColor(*c) for c in self.colors]
        data = ControllerData(self.name, MetaData("Fake", "fake device", "1.0", "", "HID: fake"), self.device_type,
                              leds, zones, self.modes(), colors, self.active_mode)
        return data.pack(version)


class FakeServer:
    """Runs in a background thread; ``port`` is assigned by the OS."""

    def __init__(self, devices: list[FakeDevice], host: str = "127.0.0.1", port: int = 0) -> None:
        self.devices = devices
        self.host = host
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.client_names: list[str] = []
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._clients: list[socket.socket] = []

    def start(self) -> "FakeServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for client in list(self._clients):
            try:
                client.close()
            except OSError:
                pass
        try:
            self.sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections += 1
            self._clients.append(client)
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _recv(self, client: socket.socket, size: int) -> Optional[bytes]:
        data = b""
        while len(data) < size:
            chunk = client.recv(size - len(data))
            if not chunk:
                return None
            data += chunk
        return data

    def _send(self, client: socket.socket, device_id: int, packet_type: int, payload: bytes) -> None:
        client.sendall(HEADER.pack(b"ORGB", device_id, packet_type, len(payload)) + payload)

    def _handle(self, client: socket.socket) -> None:
        version = PROTOCOL_VERSION
        try:
            while not self._stop.is_set():
                header = self._recv(client, HEADER.size)
                if header is None:
                    break
                magic, device_id, packet_type, size = HEADER.unpack(header)
                if magic != b"ORGB":
                    break
                data = self._recv(client, size) if size else b""
                if data is None:
                    break
                if packet_type == PacketType.REQUEST_PROTOCOL_VERSION:
                    version = min(struct.unpack("I", data)[0], PROTOCOL_VERSION)
                    self._send(client, 0, packet_type, struct.pack("I", PROTOCOL_VERSION))
                elif packet_type == PacketType.SET_CLIENT_NAME:
                    self.client_names.append(data.rstrip(b"\0").decode())
                elif packet_type == PacketType.REQUEST_CONTROLLER_COUNT:
                    self._send(client, 0, packet_type, struct.pack("I", len(self.devices)))
                elif packet_type == PacketType.REQUEST_CONTROLLER_DATA:
                    if data:
                        version = min(struct.unpack("I", data)[0], PROTOCOL_VERSION)
                    self._send(client, device_id, packet_type, self.devices[device_id].controller_data(version))
                elif packet_type in (PacketType.REQUEST_PROFILE_LIST, PacketType.REQUEST_PLUGIN_LIST):
                    self._send(client, 0, packet_type, struct.pack("IH", 6, 0))
                elif packet_type == PacketType.RGBCONTROLLER_RESIZEZONE:
                    zone, new_size = struct.unpack("ii", data)
                    self.devices[device_id].resize(zone, new_size)
                elif packet_type == PacketType.RGBCONTROLLER_UPDATELEDS:
                    count = struct.unpack_from("H", data, 4)[0]
                    colors = [struct.unpack_from("BBBx", data, 6 + 4 * i)[:3] for i in range(count)]
                    device = self.devices[device_id]
                    device.colors = [tuple(c) for c in colors]
                    device.device_updates += 1
                elif packet_type == PacketType.RGBCONTROLLER_UPDATEZONELEDS:
                    zone, count = struct.unpack_from("iH", data, 4)
                    colors = [tuple(struct.unpack_from("BBBx", data, 10 + 4 * i)[:3]) for i in range(count)]
                    self.devices[device_id].zone_updates.append((zone, colors))
                elif packet_type == PacketType.RGBCONTROLLER_UPDATEMODE:
                    mode_id = struct.unpack_from("i", data, 4)[0]
                    device = self.devices[device_id]
                    device.active_mode = mode_id
                    device.mode_history.append(mode_id)
                elif packet_type == PacketType.RGBCONTROLLER_SETCUSTOMMODE:
                    device = self.devices[device_id]
                    if "Direct" in device.mode_names:
                        device.active_mode = device.mode_names.index("Direct")
                # SAVEMODE / UPDATESINGLELED: ignored
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass
            if client in self._clients:
                self._clients.remove(client)


def asrock_like() -> FakeDevice:
    return FakeDevice(
        "ASRock Polychrome USB",
        [FakeZone("RGB LED 1 Header", 1, 1, 1), FakeZone("RGB LED 2 Header", 1, 1, 1),
         FakeZone("Addressable Header 1", 0, 100, 0), FakeZone("Addressable Header 2", 0, 100, 0),
         FakeZone("PCH", 1, 1, 1)],
        ["Static", "Breathing", "Direct"],
    )


def keyboard_like() -> FakeDevice:
    return FakeDevice("Fake Keyboard", [FakeZone("Keyboard", 104, 104, 104)], ["Static"], DeviceType.KEYBOARD)


def asrock_no_direct() -> FakeDevice:
    """An ASRock-style board whose Direct mode does not work: only effects present."""
    return FakeDevice(
        "ASRock B650I Lightning WiFi",
        [FakeZone("Addressable Header 1", 0, 100, 0), FakeZone("Addressable Header 2", 0, 100, 2)],
        ["Off", "Static", "Breathing", "Strobe"],
    )


def static_only() -> FakeDevice:
    return FakeDevice(
        "Minimal Board",
        [FakeZone("Addressable Header 1", 0, 100, 0)],
        ["Off", "Static"],
    )

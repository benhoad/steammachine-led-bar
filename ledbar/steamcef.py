"""Live download progress from the Steam client itself (optional).

Steam's UI is Chromium, and its JS context exposes a real push API:

    SteamClient.Downloads.RegisterForDownloadOverview(cb)
    SteamClient.Downloads.RegisterForDownloadItems(cb)

That is the same data Steam's own download page renders, updated continuously
rather than in the multi-minute jumps we get from ``appmanifest_*.acf``.  It is
reachable from outside the client only through CEF's remote debugging port,
which the user must enable:

    touch ~/.steam/steam/.cef-enable-remote-debugging   # then restart Steam

Everything here is best effort.  If the port is closed, the API is missing, or
Valve renames something, the source simply reports "unavailable" and ledbar
falls back to reading manifests.

No third party packages: the CDP transport is a small WebSocket client built on
``socket``, because ledbar is installed on immutable systems where adding
dependencies is awkward.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import struct
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger("ledbar.steamcef")

# --------------------------------------------------------------------------
# Minimal RFC 6455 client (text frames only)
# --------------------------------------------------------------------------

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


def encode_frame(payload: bytes, opcode: int = OP_TEXT) -> bytes:
    """Client frames are always masked and never fragmented here."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < (1 << 16):
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    else:
        header.append(0x80 | 127)
        header += struct.pack("!Q", length)
    mask = os.urandom(4)
    header += mask
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


class MiniWebSocket:
    """Just enough WebSocket to talk CDP to Steam."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buffer = b""

    # -- connection --------------------------------------------------------

    def connect(self) -> None:
        if not self.url.startswith("ws://"):
            raise WebSocketError(f"only ws:// is supported, got {self.url!r}")
        rest = self.url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        path = "/" + path
        host, _, port_text = hostport.partition(":")
        port = int(port_text or 80)

        sock = socket.create_connection((host, port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        # NB: no Origin header - Steam's CEF rejects the default one.
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {hostport}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(request.encode())

        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise WebSocketError("connection closed during handshake")
            response += chunk
            if len(response) > 65536:
                sock.close()
                raise WebSocketError("handshake response too large")
        head, _, remainder = response.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if "101" not in status:
            sock.close()
            raise WebSocketError(f"upgrade refused: {status}")
        self.sock = sock
        self._buffer = remainder

    # -- framing -----------------------------------------------------------

    def _read(self, count: int) -> bytes:
        assert self.sock is not None
        while len(self._buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buffer += chunk
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _read_frame(self) -> tuple[int, bytes]:
        first, second = self._read(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        if length > 32 * 1024 * 1024:
            raise WebSocketError("frame too large")
        mask = self._read(4) if masked else b""
        payload = self._read(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if not fin:
            # continuation: keep reading until FIN
            while True:
                nxt_op, nxt_payload = self._read_frame()
                payload += nxt_payload
                if nxt_op != OP_CONT:
                    break
                break
        return opcode, payload

    def send_text(self, text: str) -> None:
        if self.sock is None:
            raise WebSocketError("not connected")
        self.sock.sendall(encode_frame(text.encode("utf-8"), OP_TEXT))

    def recv_text(self) -> str:
        while True:
            opcode, payload = self._read_frame()
            if opcode == OP_TEXT:
                return payload.decode("utf-8", errors="replace")
            if opcode == OP_PING:
                assert self.sock is not None
                self.sock.sendall(encode_frame(payload, OP_PONG))
            elif opcode == OP_CLOSE:
                raise WebSocketError("server closed the connection")
            # binary/pong: ignore

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.sendall(encode_frame(b"", OP_CLOSE))
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


# --------------------------------------------------------------------------
# Chrome DevTools Protocol
# --------------------------------------------------------------------------


class CdpSession:
    """Evaluates JavaScript in Steam's SharedJSContext."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080, timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.ws: Optional[MiniWebSocket] = None
        self._next_id = 1

    def targets(self) -> list[dict[str, Any]]:
        url = f"http://{self.host}:{self.port}/json"
        with urllib.request.urlopen(url, timeout=self.timeout) as response:  # noqa: S310 - localhost only
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        return data if isinstance(data, list) else []

    def connect(self) -> None:
        target = None
        for candidate in self.targets():
            if candidate.get("title") == "SharedJSContext" and candidate.get("webSocketDebuggerUrl"):
                target = candidate
                break
        if target is None:
            raise WebSocketError("SharedJSContext not found (is Steam running with CEF debugging enabled?)")
        ws = MiniWebSocket(str(target["webSocketDebuggerUrl"]), timeout=self.timeout)
        ws.connect()
        self.ws = ws

    def evaluate(self, expression: str) -> Any:
        if self.ws is None:
            raise WebSocketError("not connected")
        message_id = self._next_id
        self._next_id += 1
        self.ws.send_text(json.dumps({
            "id": message_id,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            message = json.loads(self.ws.recv_text())
            if message.get("id") != message_id:
                continue                      # a CDP event, not our reply
            if "error" in message:
                raise WebSocketError(f"CDP error: {message['error']}")
            result = message.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                raise WebSocketError(f"JS error: {result.get('description')}")
            return result.get("value")
        raise WebSocketError("timed out waiting for the CDP reply")

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
        self.ws = None


# --------------------------------------------------------------------------
# The Steam download source
# --------------------------------------------------------------------------

_KEY = "__ledbar_downloads_v1"

REGISTER_JS = """
(() => {
  const KEY = "%s";
  const num = v => (typeof v === "number" && isFinite(v)) ? v : 0;
  if (window[KEY] && window[KEY].ready) return {ready: true, reused: true, error: null};
  const s = window[KEY] = {ready: false, error: null, downloading: false, overview: null, seq: 0, subs: []};
  try {
    const d = window.SteamClient && window.SteamClient.Downloads;
    if (!d) throw new Error("SteamClient.Downloads unavailable");
    if (typeof d.RegisterForDownloadOverview !== "function")
      throw new Error("RegisterForDownloadOverview unavailable");
    s.subs.push(d.RegisterForDownloadOverview(o => {
      s.overview = o ? {
        paused: !!o.paused,
        percent: num(o.overall_percent_complete ?? o.overallPercentComplete),
        appid: num(o.update_appid ?? o.updateAppId),
        state: String(o.update_state ?? o.updateState ?? "None"),
        rate: num(o.update_network_bytes_per_second ?? o.updateNetworkBytesPerSecond)
      } : null;
      s.seq++;
    }));
    if (typeof d.RegisterForDownloadItems === "function") {
      s.subs.push(d.RegisterForDownloadItems(active => { s.downloading = !!active; s.seq++; }));
    }
    s.ready = true;
    return {ready: true, reused: false, error: null};
  } catch (e) {
    s.error = String((e && e.message) || e);
    return {ready: false, reused: false, error: s.error};
  }
})()
""" % _KEY

READ_JS = """
(() => {
  const s = window["%s"];
  if (!s) return {ready: false, error: "not registered"};
  return {ready: !!s.ready, error: s.error, downloading: !!s.downloading,
          overview: s.overview, seq: s.seq};
})()
""" % _KEY


@dataclass
class CefProgress:
    """What the Steam client says about the current download."""

    active: bool = False
    fraction: Optional[float] = None
    appid: Optional[int] = None
    paused: bool = False
    state: str = ""
    rate_bytes_per_second: float = 0.0


def _fraction_from_percent(percent: float) -> Optional[float]:
    """Steam reports 0..100; tolerate a 0..1 API change."""
    if percent <= 0.0:
        return 0.0
    value = percent / 100.0 if percent > 1.0 else percent
    return max(0.0, min(1.0, value))


class CefDownloadSource:
    """Polls Steam's own download state, reconnecting as needed."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8080,
                 timeout: float = 5.0, retry_seconds: float = 15.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.retry_seconds = retry_seconds
        self.session: Optional[CdpSession] = None
        self.available = False
        self.last_error = ""
        self._next_attempt = 0.0
        self._registered = False

    # -- lifecycle ---------------------------------------------------------

    def _disconnect(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        self.available = False
        self._registered = False

    def _note_failure(self, message: str) -> None:
        if message != self.last_error:
            log.info("Steam CEF unavailable: %s (falling back to manifests)", message)
            self.last_error = message
        self._disconnect()
        self._next_attempt = time.monotonic() + self.retry_seconds

    def _ensure(self) -> bool:
        if self.session is not None and self._registered:
            return True
        if time.monotonic() < self._next_attempt:
            return False
        try:
            session = CdpSession(self.host, self.port, self.timeout)
            session.connect()
            result = session.evaluate(REGISTER_JS)
            if not isinstance(result, dict) or not result.get("ready"):
                error = (result or {}).get("error") if isinstance(result, dict) else "no result"
                session.close()
                self._note_failure(str(error))
                return False
            self.session = session
            self._registered = True
            self.available = True
            self.last_error = ""
            log.info("Steam CEF connected: live download progress from the Steam client")
            return True
        except Exception as exc:
            self._note_failure(str(exc))
            return False

    # -- polling -----------------------------------------------------------

    def poll(self) -> Optional[CefProgress]:
        """Current download state, or None when the source is unusable."""
        if not self._ensure():
            return None
        try:
            data = self.session.evaluate(READ_JS)  # type: ignore[union-attr]
        except Exception as exc:
            self._note_failure(str(exc))
            return None
        if not isinstance(data, dict):
            self._note_failure("unexpected reply")
            return None
        if not data.get("ready"):
            # the JS context was recreated (Steam restarted or reloaded its UI)
            self._registered = False
            return None

        overview = data.get("overview") or {}
        state = str(overview.get("state") or "None")
        paused = bool(overview.get("paused"))
        appid = int(overview.get("appid") or 0) or None
        fraction = _fraction_from_percent(float(overview.get("percent") or 0.0))
        # "None" is Steam's idle state; items flag corroborates it
        active = bool(data.get("downloading")) or (state.lower() not in ("none", "", "0"))
        return CefProgress(
            active=active and not (paused and state.lower() == "none"),
            fraction=fraction if active else None,
            appid=appid,
            paused=paused,
            state=state,
            rate_bytes_per_second=float(overview.get("rate") or 0.0),
        )

    def close(self) -> None:
        self._disconnect()


def debugging_enabled(steam_roots) -> Optional[bool]:
    """True/False if we can tell whether CEF remote debugging is switched on."""
    found = False
    for root in steam_roots:
        marker = os.path.join(str(root), ".cef-enable-remote-debugging")
        if os.path.exists(marker):
            return True
        found = True
    return False if found else None

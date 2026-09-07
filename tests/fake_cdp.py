"""A tiny stand-in for Steam's CEF remote-debugging endpoint.

Serves ``GET /json`` over HTTP and accepts a WebSocket upgrade on the same
port, answering ``Runtime.evaluate`` calls.  Enough to exercise the handshake,
the frame codec and the CDP request/response matching for real.
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _decode_client_frame(sock: socket.socket) -> tuple[int, bytes]:
    def read(n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = sock.recv(n - len(data))
            if not chunk:
                raise ConnectionError("closed")
            data += chunk
        return data

    first, second = read(2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read(8))[0]
    mask = read(4) if masked else b""
    payload = read(length)
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def _encode_server_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < (1 << 16):
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


class FakeCdpServer:
    def __init__(self, overview: dict | None = None, downloading: bool = True,
                 register_ok: bool = True, title: str = "SharedJSContext") -> None:
        self.overview = overview
        self.downloading = downloading
        self.register_ok = register_ok
        self.title = title
        self.evaluations: list[str] = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeCdpServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # ------------------------------------------------------------------

    def _serve(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client.recv(4096)
                if not chunk:
                    return
                request += chunk
            head = request.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
            lines = head.split("\r\n")
            path = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "/"
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    name, _, value = line.partition(":")
                    headers[name.strip().lower()] = value.strip()

            if "websocket" in headers.get("upgrade", "").lower():
                self._do_websocket(client, headers)
                return

            if path == "/json":
                body = json.dumps([
                    {"title": "Other Page", "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/OTHER"},
                    {"title": self.title, "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/page/ABC"},
                ]).encode()
                client.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
                )
            else:
                client.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def _do_websocket(self, client: socket.socket, headers: dict) -> None:
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        client.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\n" + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        while not self._stop.is_set():
            try:
                opcode, payload = _decode_client_frame(client)
            except (ConnectionError, OSError):
                return
            if opcode == 0x8:
                return
            if opcode != 0x1:
                continue
            message = json.loads(payload.decode())
            expression = message.get("params", {}).get("expression", "")
            self.evaluations.append(expression)
            if "RegisterForDownloadOverview" in expression:
                value = ({"ready": True, "reused": False, "error": None} if self.register_ok
                         else {"ready": False, "reused": False, "error": "SteamClient.Downloads unavailable"})
            else:
                value = {"ready": True, "error": None, "downloading": self.downloading,
                         "overview": self.overview, "seq": 7}
            reply = {"id": message.get("id"), "result": {"result": {"type": "object", "value": value}}}
            # an unsolicited CDP event first: the client must skip it
            client.sendall(_encode_server_frame(json.dumps({"method": "Runtime.consoleAPICalled"}).encode()))
            client.sendall(_encode_server_frame(json.dumps(reply).encode()))

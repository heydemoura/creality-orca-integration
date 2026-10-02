"""A tiny stand-in for a Creality K-series printer's port-9999 WebSocket service.

It speaks the subset of the protocol the plugin uses, seeded from the live K1 Max capture:

* `get` requests for boxsInfo / boxConfig / reqMaterials answer with the recorded objects,
* `set modifyMaterial` updates the slot table, acknowledges with `{"modifyMaterial": 0}` and pushes
  the new `boxsInfo` the way the real firmware does,
* `set gcodeCmd` is recorded,
* a `heart_beat` text frame is emitted periodically to exercise the keep-alive handling.

Only the standard library is used so the test-suite runs anywhere.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import struct
import threading
import time
from typing import Any

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "k1max_capture.json")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def load_fixture() -> dict:
    with open(FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


class FakePrinter:
    def __init__(self, *, device_state: int = 0, heartbeat_interval: float = 0.0, ack_modify: bool = True, push_after_modify: bool = True):
        data = load_fixture()
        self.boxs_info = copy.deepcopy(data["boxsInfo"])
        self.box_config = copy.deepcopy(data["boxConfig"])
        self.materials = copy.deepcopy(data["retMaterials"])
        self.status = copy.deepcopy(data["status"])
        self.status["deviceState"] = device_state
        self.status["state"] = device_state
        self.heartbeat_interval = heartbeat_interval
        self.ack_modify = ack_modify
        self.push_after_modify = push_after_modify
        self.received: list[dict] = []
        self.gcode: list[str] = []
        self.lock = threading.Lock()
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(5)
        self.port = self._server.getsockname()[1]
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)

    # -- lifecycle --------------------------------------------------------------------------------
    def start(self) -> "FakePrinter":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._server.close()
        except Exception:
            pass
        for c in list(self._clients):
            try:
                c.close()
            except Exception:
                pass

    def __enter__(self) -> "FakePrinter":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- helpers for tests -------------------------------------------------------------------------
    def slot(self, box_id: int, slot_id: int) -> dict:
        for box in self.boxs_info["materialBoxs"]:
            if box["id"] == box_id:
                for m in box["materials"]:
                    if m["id"] == slot_id:
                        return m
        raise KeyError((box_id, slot_id))

    def set_device_state(self, value: int) -> None:
        with self.lock:
            self.status["deviceState"] = value
            self.status["state"] = value
        self.broadcast({"deviceState": value, "state": value})

    def broadcast(self, obj: dict) -> None:
        text = json.dumps(obj)
        for c in list(self._clients):
            try:
                _send_text(c, text)
            except Exception:
                pass

    # -- server -------------------------------------------------------------------------------------
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._server.accept()
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            if not _handshake(conn):
                conn.close()
                return
            self._clients.append(conn)
            # Like the real firmware: a status burst right after connecting.
            _send_text(conn, json.dumps(self.status))
            last_hb = time.time()
            conn.settimeout(0.2)
            buffer = b""
            while not self._stop.is_set():
                if self.heartbeat_interval and time.time() - last_hb > self.heartbeat_interval:
                    _send_text(conn, "heart_beat")
                    last_hb = time.time()
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while True:
                    frame, buffer = _parse_frame(buffer)
                    if frame is None:
                        break
                    opcode, payload = frame
                    if opcode == 0x8:
                        try:
                            _send_frame(conn, 0x8, payload[:2])
                        except Exception:
                            pass
                        raise ConnectionError("client closed")
                    if opcode == 0x9:
                        _send_frame(conn, 0xA, payload)
                        continue
                    if opcode != 0x1:
                        continue
                    text = payload.decode("utf-8", "replace")
                    if text == "ok":
                        continue
                    try:
                        msg = json.loads(text)
                    except ValueError:
                        continue
                    self._handle(conn, msg)
        except Exception:
            pass
        finally:
            if conn in self._clients:
                self._clients.remove(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _handle(self, conn: socket.socket, msg: dict) -> None:
        with self.lock:
            self.received.append(msg)
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "get":
            reply: dict[str, Any] = {}
            if params.get("boxsInfo"):
                reply["boxsInfo"] = copy.deepcopy(self.boxs_info)
            if params.get("boxConfig"):
                reply.update(copy.deepcopy(self.box_config))
                reply["boxConfig"] = copy.deepcopy(self.box_config)
            if params.get("reqMaterials"):
                _send_text(conn, json.dumps({"retMaterials": self.materials}))
            if reply:
                _send_text(conn, json.dumps(reply))
            return
        if method == "set":
            if "modifyMaterial" in params:
                mm = params["modifyMaterial"]
                with self.lock:
                    slot = self.slot(int(mm["boxId"]), int(mm["id"]))
                    cleared = not (mm.get("type") or mm.get("name") or mm.get("vendor") or mm.get("color"))
                    slot["vendor"] = mm.get("vendor", "")
                    slot["type"] = mm.get("type", "")
                    slot["name"] = mm.get("name", "")
                    slot["rfid"] = mm.get("rfid", "")
                    slot["color"] = mm.get("color", "")
                    slot["minTemp"] = int(round(float(mm.get("minTemp", 0))))
                    slot["maxTemp"] = int(round(float(mm.get("maxTemp", 0))))
                    slot["pressure"] = float(mm.get("pressure", 0))
                    slot["state"] = 0 if cleared else 1
                    slot["editStatus"] = 0 if cleared else 1
                if self.ack_modify:
                    _send_text(conn, json.dumps({"modifyMaterial": 0}))
                if self.push_after_modify:
                    _send_text(conn, json.dumps({"boxsInfo": copy.deepcopy(self.boxs_info)}))
            if "gcodeCmd" in params:
                with self.lock:
                    self.gcode.append(str(params["gcodeCmd"]))
            return


# -- frame helpers (server side: unmasked frames out, masked frames in) ----------------------------


def _handshake(conn: socket.socket) -> bool:
    conn.settimeout(5)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            return False
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
    key = ""
    for line in head.split("\r\n")[1:]:
        k, _, v = line.partition(":")
        if k.strip().lower() == "sec-websocket-key":
            key = v.strip()
    if not key:
        return False
    accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
    )
    return True


def _send_frame(conn: socket.socket, opcode: int, payload: bytes) -> None:
    header = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack("!H", n)
    else:
        header += bytes([127]) + struct.pack("!Q", n)
    conn.sendall(header + payload)


def _send_text(conn: socket.socket, text: str) -> None:
    _send_frame(conn, 0x1, text.encode("utf-8"))


def _parse_frame(buffer: bytes):
    if len(buffer) < 2:
        return None, buffer
    b1, b2 = buffer[0], buffer[1]
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    pos = 2
    if length == 126:
        if len(buffer) < 4:
            return None, buffer
        length = struct.unpack("!H", buffer[2:4])[0]
        pos = 4
    elif length == 127:
        if len(buffer) < 10:
            return None, buffer
        length = struct.unpack("!Q", buffer[2:10])[0]
        pos = 10
    mask = b""
    if masked:
        if len(buffer) < pos + 4:
            return None, buffer
        mask = buffer[pos : pos + 4]
        pos += 4
    if len(buffer) < pos + length:
        return None, buffer
    payload = buffer[pos : pos + length]
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return (opcode, payload), buffer[pos + length :]


if __name__ == "__main__":  # manual testing: python tests/fake_printer.py -> prints the port
    with FakePrinter(heartbeat_interval=5.0) as p:
        print(f"fake printer listening on ws://127.0.0.1:{p.port}")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass

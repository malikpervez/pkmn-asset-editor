"""Live play: the running play-test at full speed (60 frames a second) with sound, streamed to the browser.

A ``LiveRunner`` thread runs the emulator one frame per 1/60 s (or several when fast-forwarding), holding the
buttons the browser says are down, and sends every frame (PNG) and its sound (16-bit stereo) to the connected
viewers over a WebSocket. Other play-test commands (start a battle, run a script, save a checkpoint) take the same
lock, so they happen between frames.

The WebSocket protocol, both ways in binary or JSON text:
* server → browser: ``\\x01`` + PNG (a frame), ``\\x02`` + samples (s16le stereo at ``RATE``),
  ``{"frame": N, "speed": S, "paused": bool}`` now and then.
* browser → server: ``{"keys": mask}`` (GBA KEYINPUT bits, pressed = 1), ``{"speed": 1|2|4|8}``, ``{"pause": bool}``,
  ``{"sound": bool}``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import socket
import struct
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

RATE = 32768
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def accept_key(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()


def ws_frame(payload: bytes, opcode: int = 2) -> bytes:
    n = len(payload)
    head = bytes([0x80 | opcode])
    if n < 126:
        head += bytes([n])
    elif n < 1 << 16:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def ws_read(sock: socket.socket) -> tuple[int, bytes] | None:
    """One message from a browser (masked frames); None when the connection closes."""
    def exact(n):
        buf = b""
        while len(buf) < n:
            part = sock.recv(n - len(buf))
            if not part:
                return None
            buf += part
        return buf
    h = exact(2)
    if h is None:
        return None
    opcode, n = h[0] & 0x0F, h[1] & 0x7F
    if n == 126:
        ext = exact(2)
        n = struct.unpack(">H", ext)[0] if ext else 0
    elif n == 127:
        ext = exact(8)
        n = struct.unpack(">Q", ext)[0] if ext else 0
    mask = exact(4) if h[1] & 0x80 else b"\0\0\0\0"
    data = exact(n) if n else b""
    if mask is None or data is None:
        return None
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


class Viewer:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.sound = False
        self.alive = True
        self.lock = threading.Lock()

    def send(self, payload: bytes, opcode: int = 2) -> None:
        if not self.alive:
            return
        try:
            with self.lock:
                self.sock.sendall(ws_frame(payload, opcode))
        except OSError:
            self.alive = False


class LiveRunner(threading.Thread):
    def __init__(self, session):
        super().__init__(daemon=True)
        self.session = session
        self.keys = 0
        self.speed = 1
        self.paused = False
        self.viewers: list[Viewer] = []
        self.frame = 0
        self.stop_event = threading.Event()
        self._audio_on = None
        fd, name = tempfile.mkstemp(prefix="pae-live-", suffix=".bin",
                                    dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        os.close(fd)
        self.tmp = Path(name)

    def add(self, viewer: Viewer) -> None:
        self.viewers.append(viewer)

    def handle(self, msg: dict) -> None:
        if "keys" in msg:
            self.keys = int(msg["keys"]) & 0x3FF
        if "speed" in msg:
            self.speed = max(1, min(8, int(msg["speed"] or 1)))
        if "pause" in msg:
            self.paused = bool(msg["pause"])

    def run(self) -> None:
        next_t = time.perf_counter()
        status_every = 30
        while not self.stop_event.is_set():
            self.viewers = [v for v in self.viewers if v.alive]
            if not self.viewers:
                time.sleep(0.05)
                next_t = time.perf_counter()
                continue
            game = self.session.game
            if game is None or self.paused:
                time.sleep(0.05)
                next_t = time.perf_counter()
                continue
            try:
                with self.session.lock:
                    game = self.session.game  # it may have been closed or restarted meanwhile
                    if game is None:
                        continue
                    emu = game.emu
                    if self._audio_on is not emu:
                        emu.cmd(f"audio {RATE}")
                        self._audio_on = emu
                    keys = self.keys
                    emu.cmd(f"live {self.speed} {keys} {self.tmp}")
                    self.session.note_frames(keys, self.speed)  # so live play can be saved as a replay test
                    data = self.tmp.read_bytes()
            except Exception:  # noqa: BLE001 - the game stopped: wait for the next one
                time.sleep(0.2)
                next_t = time.perf_counter()
                continue
            self.frame += self.speed
            n = struct.unpack("<I", data[:4])[0]
            video = data[4:4 + emu.width * emu.height * 4]
            audio = data[4 + emu.width * emu.height * 4:4 + emu.width * emu.height * 4 + n * 4]
            buf = io.BytesIO()
            Image.frombytes("RGBA", (emu.width, emu.height), video).convert("RGB").save(buf, "PNG", compress_level=1)
            png = b"\x01" + buf.getvalue()
            for v in self.viewers:
                v.send(png)
                if v.sound and self.speed == 1 and audio:
                    v.send(b"\x02" + audio)
            if self.frame % status_every < self.speed:
                st = json.dumps({"frame": self.frame, "speed": self.speed, "paused": self.paused}).encode()
                for v in self.viewers:
                    v.send(st, opcode=1)
            next_t += 1 / 60
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.25:  # fell behind (a slow command ran): don't try to catch up
                next_t = time.perf_counter()
        self.tmp.unlink(missing_ok=True)

    def stop(self) -> None:
        self.stop_event.set()


def serve_viewer(handler, runner: LiveRunner) -> None:
    """Turn an HTTP request into a WebSocket and keep it until the browser leaves."""
    key = handler.headers.get("Sec-WebSocket-Key", "")
    handler.protocol_version = "HTTP/1.1"
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.wfile.flush()
    sock = handler.connection
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    viewer = Viewer(sock)
    runner.add(viewer)
    try:
        while viewer.alive:
            msg = ws_read(sock)
            if msg is None:
                break
            op, data = msg
            if op == 8:  # close
                break
            if op == 9:  # ping
                viewer.send(data, opcode=10)
                continue
            if op == 1:
                try:
                    m = json.loads(data.decode())
                except ValueError:
                    continue
                if "sound" in m:
                    viewer.sound = bool(m["sound"])
                runner.handle(m)
    except OSError:
        pass
    finally:
        viewer.alive = False
        runner.keys = 0
        handler.close_connection = True

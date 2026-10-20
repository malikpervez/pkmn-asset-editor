import json
import os
import socket
import struct
import threading
import time

from pkmn_asset_editor import live


def test_handshake_and_frames():
    assert live.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="  # RFC 6455's example
    assert live.ws_frame(b"hi", 1) == b"\x81\x02hi"
    big = live.ws_frame(b"x" * 300)
    assert big[:4] == b"\x82\x7e\x01\x2c" and len(big) == 304
    huge = live.ws_frame(b"x" * 70000)
    assert huge[:2] == b"\x82\x7f" and struct.unpack(">Q", huge[2:10])[0] == 70000
    a, b = socket.socketpair()
    mask = b"\x01\x02\x03\x04"
    payload = json.dumps({"keys": 17}).encode()
    a.sendall(bytes([0x81, 0x80 | len(payload)]) + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))
    assert live.ws_read(b) == (1, payload)
    a.close()
    assert live.ws_read(b) is None
    b.close()


class FakeEmu:
    width, height = 240, 160

    def __init__(self):
        self.lines = []

    def cmd(self, line):
        self.lines.append(line)
        if line.startswith("live "):
            path = line.split(" ", 3)[3]
            n = 546
            with open(path, "wb") as f:
                f.write(struct.pack("<I", n) + b"\x40" * (240 * 160 * 4) + b"\x00\x01" * (2 * n))
        return ""


class FakeSession:
    def __init__(self):
        self.lock = threading.RLock()
        self.game = type("G", (), {"emu": FakeEmu()})()


def test_runner_streams_frames_and_sound():
    s = FakeSession()
    r = live.LiveRunner(s)
    a, b = socket.socketpair()
    v = live.Viewer(a)
    v.sound = True
    r.add(v)
    r.handle({"keys": 0xFFFF, "speed": 1})
    assert r.keys == 0x3FF
    r.start()
    got = b""
    deadline = time.time() + 5
    b.settimeout(5)
    while got.count(b"\x82") < 4 and time.time() < deadline:
        got += b.recv(1 << 20)
    r.stop()
    r.join(2)
    assert s.game.emu.lines[0] == f"audio {live.RATE}"
    assert any(x.startswith("live 1 1023 ") for x in s.game.emu.lines)
    assert b"\x01\x89PNG" in got and b"\x02\x00\x01" in got  # a frame and its sound
    assert not os.path.exists(r.tmp)
    a.close()
    b.close()

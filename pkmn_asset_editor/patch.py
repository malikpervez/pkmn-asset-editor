"""UPS and IPS patch creation/application, so edits can be shared without the ROM."""

from __future__ import annotations

import zlib

import numpy as np


def _diff_runs(src: bytes, dst: bytes, n: int, bridge: int = 0) -> list[tuple[int, int]]:
    """[start, end) runs where the (zero-extended) inputs differ; gaps <= ``bridge`` are merged."""
    a = np.zeros(n, dtype=np.uint8)
    b = np.zeros(n, dtype=np.uint8)
    a[: len(src)] = np.frombuffer(src, dtype=np.uint8)[:n]
    b[: len(dst)] = np.frombuffer(dst, dtype=np.uint8)[:n]
    d = np.concatenate([[False], a != b, [False]])
    edges = np.flatnonzero(np.diff(d.astype(np.int8)))
    runs = [(int(s), int(e)) for s, e in zip(edges[0::2], edges[1::2])]
    if bridge and runs:
        merged = [list(runs[0])]
        for s, e in runs[1:]:
            if s - merged[-1][1] <= bridge:
                merged[-1][1] = e
            else:
                merged.append([s, e])
        runs = [tuple(r) for r in merged]
    return runs


def _vlq(n: int) -> bytes:
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n == 0:
            out.append(0x80 | x)
            return bytes(out)
        out.append(x)
        n -= 1


def _read_vlq(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 1
    while True:
        x = data[pos]
        pos += 1
        result += (x & 0x7F) * shift
        if x & 0x80:
            return result, pos
        shift <<= 7
        result += shift


def make_ups(src: bytes, dst: bytes) -> bytes:
    out = bytearray(b"UPS1")
    out += _vlq(len(src)) + _vlq(len(dst))
    n = max(len(src), len(dst))
    s_ext = src.ljust(n, b"\0")
    d_ext = dst.ljust(n, b"\0")
    last = 0
    for start, end in _diff_runs(src, dst, n):
        out += _vlq(start - last)
        out += bytes(x ^ y for x, y in zip(s_ext[start:end], d_ext[start:end]))
        out.append(0)
        last = end + 1
    out += zlib.crc32(src).to_bytes(4, "little")
    out += zlib.crc32(dst).to_bytes(4, "little")
    out += zlib.crc32(bytes(out)).to_bytes(4, "little")
    return bytes(out)


def apply_ups(src: bytes, patch: bytes) -> bytes:
    if patch[:4] != b"UPS1":
        raise ValueError("not a UPS patch")
    pos = 4
    src_len, pos = _read_vlq(patch, pos)
    dst_len, pos = _read_vlq(patch, pos)
    if len(src) != src_len or zlib.crc32(src) != int.from_bytes(patch[-12:-8], "little"):
        raise ValueError("UPS patch does not match this ROM")
    out = bytearray(src[:dst_len].ljust(dst_len, b"\0"))
    i = 0
    end = len(patch) - 12
    while pos < end:
        skip, pos = _read_vlq(patch, pos)
        i += skip
        while True:
            x = patch[pos]
            pos += 1
            if x == 0:
                i += 1
                break
            if i < dst_len:
                out[i] ^= x
            i += 1
    if zlib.crc32(bytes(out)) != int.from_bytes(patch[-8:-4], "little"):
        raise ValueError("UPS result checksum mismatch")
    return bytes(out)


def make_ips(src: bytes, dst: bytes) -> bytes:
    if len(dst) > 0x1000000:
        raise ValueError("IPS cannot address ROMs larger than 16 MiB; use the UPS patch")
    out = bytearray(b"PATCH")
    n = len(dst)
    # bytes past the end of src always need writing, even if they happen to be 0
    runs = _diff_runs(src[:n], dst, n, bridge=5)
    if n > len(src):
        runs = [r for r in runs if r[1] <= len(src)] + [(len(src), n)]
    for start, end in runs:
        if start == 0x454F46:  # "EOF" can't be used as an offset
            start -= 1
        while start < end:
            chunk = min(0xFFFF, end - start)
            out += start.to_bytes(3, "big") + chunk.to_bytes(2, "big") + dst[start:start + chunk]
            start += chunk
            if start == 0x454F46 and start < end:
                start -= 1
    out += b"EOF"
    return bytes(out)


def apply_ips(src: bytes, patch: bytes) -> bytes:
    if patch[:5] != b"PATCH":
        raise ValueError("not an IPS patch")
    out = bytearray(src)
    pos = 5
    while patch[pos:pos + 3] != b"EOF":
        off = int.from_bytes(patch[pos:pos + 3], "big")
        size = int.from_bytes(patch[pos + 3:pos + 5], "big")
        pos += 5
        if size == 0:
            rle = int.from_bytes(patch[pos:pos + 2], "big")
            data = patch[pos + 2:pos + 3] * rle
            pos += 3
        else:
            data = patch[pos:pos + size]
            pos += size
        if off + len(data) > len(out):
            out.extend(b"\0" * (off + len(data) - len(out)))
        out[off:off + len(data)] = data
    return bytes(out)

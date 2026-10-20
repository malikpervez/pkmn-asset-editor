"""BPS, UPS and IPS patch creation/application, so edits can be shared without the ROM."""

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
    xor = (np.frombuffer(s_ext, dtype=np.uint8) ^ np.frombuffer(d_ext, dtype=np.uint8)).tobytes()
    last = 0
    for start, end in _diff_runs(src, dst, n):
        out += _vlq(start - last)
        out += xor[start:end]
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


def _window_keys(a: np.ndarray, w: int) -> np.ndarray:
    """A 64-bit key for the ``w``-byte window at every 4-byte-aligned offset of ``a``."""
    words = a[: len(a) - len(a) % 4].view("<u4").astype(np.uint64)
    k = w // 4
    n = len(words) - k + 1
    if n <= 0:
        return np.zeros(0, dtype=np.uint64)
    key = np.zeros(n, dtype=np.uint64)
    with np.errstate(over="ignore"):
        for j in range(k):
            key = key * np.uint64(0x100000001B3) + words[j:j + n] + np.uint64(j * 0x9E3779B97F4A7C15 & (2**64 - 1))
    return key


def _moved_blocks(src: bytes, dst: bytes, w: int = 32) -> list[tuple[int, int, int]]:
    """(target offset, source offset, length) of data the target has at another place than the source: GBA data is
    4-byte aligned, so whole graphics or music blobs that only moved are found by their aligned 32-byte windows."""
    a = np.frombuffer(src, dtype=np.uint8)
    b = np.frombuffer(dst, dtype=np.uint8)
    ka, kb = _window_keys(a, w), _window_keys(b, w)
    if not len(ka) or not len(kb):
        return []
    order = np.argsort(ka, kind="stable")
    sk = ka[order]
    pos = np.searchsorted(sk, kb)
    pos[pos >= len(sk)] = len(sk) - 1
    hit = sk[pos] == kb
    cand = np.full(len(kb), -1, dtype=np.int64)
    cand[hit] = order[pos[hit]].astype(np.int64) * 4
    same = np.zeros(len(kb), dtype=bool)  # already the same at the same place: SourceRead covers it
    m = min(len(ka), len(kb))
    same[:m] = ka[:m] == kb[:m]
    cand[same] = -1
    out = []
    starts = np.flatnonzero(cand >= 0)
    t_end = 0
    for wi in starts:
        t = int(wi) * 4
        if t < t_end:
            continue
        sp = int(cand[wi])
        if bytes(a[sp:sp + w]) != bytes(b[t:t + w]):
            continue  # a hash collision
        n = w
        step = 256
        while t + n < len(b) and sp + n < len(a):  # extend the match as far as it goes
            k = min(step, len(b) - t - n, len(a) - sp - n)
            eq = a[sp + n:sp + n + k] == b[t + n:t + n + k]
            if eq.all():
                n += k
                step *= 2
                continue
            n += int(np.argmin(eq))
            break
        out.append((t, sp, n))
        t_end = t + n
    return out


def make_bps(src: bytes, dst: bytes, metadata: bytes = b"") -> bytes:
    """A BPS patch (what Flips and Rom Patcher JS apply). Bytes that are the same at the same place are read from the
    original ROM, data that only moved is copied from where it is in the original, and the rest is stored."""
    out = bytearray(b"BPS1") + _vlq(len(src)) + _vlq(len(dst)) + _vlq(len(metadata)) + metadata
    n, m = len(dst), min(len(src), len(dst))
    moved = _moved_blocks(src, dst)
    pos = src_rel = 0

    def plain(start: int, end: int) -> None:
        """[start, end) without moved data: SourceRead where unchanged, TargetRead where not."""
        nonlocal out
        cut = min(end, m)
        runs = [list(r) for r in _diff_runs(src[start:cut], dst[start:cut], cut - start, bridge=6)] if cut > start else []
        runs = [[s + start, e + start] for s, e in runs]
        if end > cut:
            if runs and runs[-1][1] == cut:
                runs[-1][1] = end
            else:
                runs.append([cut, end])
        p = start
        for s, e in runs:
            if s > p:
                out += _vlq(((s - p) - 1) << 2 | 0)  # SourceRead
            out += _vlq(((e - s) - 1) << 2 | 1) + dst[s:e]  # TargetRead
            p = e
        if p < end:
            out += _vlq(((end - p) - 1) << 2 | 0)

    for t, sp, length in moved:
        plain(pos, t)
        d = sp - src_rel
        out += _vlq((length - 1) << 2 | 2) + _vlq((abs(d) << 1) | (1 if d < 0 else 0))  # SourceCopy
        src_rel = sp + length
        pos = t + length
    plain(pos, n)
    out += zlib.crc32(src).to_bytes(4, "little")
    out += zlib.crc32(dst).to_bytes(4, "little")
    out += zlib.crc32(bytes(out)).to_bytes(4, "little")
    return bytes(out)


def apply_bps(src: bytes, patch: bytes) -> bytes:
    if patch[:4] != b"BPS1":
        raise ValueError("not a BPS patch")
    if zlib.crc32(patch[:-4]) != int.from_bytes(patch[-4:], "little"):
        raise ValueError("the patch file is damaged")
    if zlib.crc32(src) != int.from_bytes(patch[-12:-8], "little"):
        raise ValueError("this patch is for a different ROM")
    pos = 4
    src_size, pos = _read_vlq(patch, pos)
    dst_size, pos = _read_vlq(patch, pos)
    meta, pos = _read_vlq(patch, pos)
    pos += meta
    out = bytearray(dst_size)
    o = src_rel = dst_rel = 0
    end = len(patch) - 12
    while pos < end:
        v, pos = _read_vlq(patch, pos)
        cmd, length = v & 3, (v >> 2) + 1
        if cmd == 0:
            out[o:o + length] = src[o:o + length]
        elif cmd == 1:
            out[o:o + length] = patch[pos:pos + length]
            pos += length
        else:
            d, pos = _read_vlq(patch, pos)
            d = -(d >> 1) if d & 1 else d >> 1
            if cmd == 2:
                src_rel += d
                out[o:o + length] = src[src_rel:src_rel + length]
                src_rel += length
            else:
                dst_rel += d
                for k in range(length):  # may overlap the bytes being written
                    out[o + k] = out[dst_rel + k]
                dst_rel += length
        o += length
    if zlib.crc32(bytes(out)) != int.from_bytes(patch[-8:-4], "little"):
        raise ValueError("the patched ROM doesn't match (wrong original ROM?)")
    return bytes(out)

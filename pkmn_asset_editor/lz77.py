"""GBA BIOS LZ77 (type 0x10) compression.

The compressor is VRAM-safe (never emits a back-reference with displacement
1) so data it produces can be decompressed with either LZ77UnCompWram or
LZ77UnCompVram, and it uses an optimal parse so re-compressed graphics fit
back into their original slot as often as possible.
"""

from __future__ import annotations

MAX_RAW_SIZE = 0x40000  # nothing in a GBA game decompresses to more than this


class LZ77Error(ValueError):
    pass


def peek_header(data: bytes | bytearray, offset: int) -> int | None:
    """Return the decompressed size declared at ``offset`` or None if it is not an LZ77 header."""
    if offset + 4 > len(data) or data[offset] != 0x10:
        return None
    size = data[offset + 1] | (data[offset + 2] << 8) | (data[offset + 3] << 16)
    if size == 0 or size > MAX_RAW_SIZE:
        return None
    return size


def decompress(data: bytes | bytearray, offset: int = 0) -> tuple[bytes, int]:
    """Decompress the stream at ``offset``.

    Returns ``(raw, consumed)`` where ``consumed`` is the number of compressed
    bytes read (including the 4-byte header). Raises LZ77Error on malformed data.
    """
    size = peek_header(data, offset)
    if size is None:
        raise LZ77Error(f"no LZ77 header at 0x{offset:X}")
    out = bytearray()
    pos = offset + 4
    end = len(data)
    while len(out) < size:
        if pos >= end:
            raise LZ77Error("unexpected end of data")
        flags = data[pos]
        pos += 1
        for bit in range(8):
            if len(out) >= size:
                break
            if flags & (0x80 >> bit):
                if pos + 1 >= end:
                    raise LZ77Error("unexpected end of data")
                b0, b1 = data[pos], data[pos + 1]
                pos += 2
                length = (b0 >> 4) + 3
                disp = (((b0 & 0xF) << 8) | b1) + 1
                if disp > len(out):
                    raise LZ77Error("back-reference before start of output")
                start = len(out) - disp
                for i in range(length):
                    out.append(out[start + i])
            else:
                if pos >= end:
                    raise LZ77Error("unexpected end of data")
                out.append(data[pos])
                pos += 1
    return bytes(out[:size]), pos - offset


def try_decompress(data: bytes | bytearray, offset: int) -> tuple[bytes, int] | None:
    try:
        return decompress(data, offset)
    except LZ77Error:
        return None


_MIN_LEN = 3
_MAX_LEN = 18
_MIN_DISP = 2  # VRAM safe
_MAX_DISP = 4096
_MAX_CHAIN = 256


def _longest_matches(src: bytes) -> tuple[list[int], list[int]]:
    """For every position, the longest usable match length and its displacement."""
    n = len(src)
    best_len = [0] * n
    best_disp = [0] * n
    heads: dict[bytes, list[int]] = {}
    for i in range(n):
        if i + _MIN_LEN <= n:
            key = src[i:i + _MIN_LEN]
            chain = heads.get(key)
            if chain:
                limit = min(_MAX_LEN, n - i)
                blen = 0
                bdisp = 0
                checked = 0
                for j in reversed(chain):
                    disp = i - j
                    if disp > _MAX_DISP:
                        break
                    if disp < _MIN_DISP:
                        continue
                    checked += 1
                    length = _MIN_LEN
                    while length < limit and src[j + length] == src[i + length]:
                        length += 1
                    if length > blen:
                        blen, bdisp = length, disp
                        if length == limit:
                            break
                    if checked >= _MAX_CHAIN:
                        break
                best_len[i] = blen
                best_disp[i] = bdisp
                # keep chains bounded to the window
                if len(chain) > 2 * _MAX_CHAIN and i - chain[0] > _MAX_DISP:
                    del chain[: len(chain) - _MAX_CHAIN]
                chain.append(i)
            else:
                heads[key] = [i]
    return best_len, best_disp


def compress(src: bytes | bytearray) -> bytes:
    """Compress ``src`` into a GBA LZ77 stream padded to a multiple of 4 bytes."""
    src = bytes(src)
    n = len(src)
    if n == 0 or n > 0xFFFFFF:
        raise LZ77Error("cannot compress empty or oversized data")
    best_len, best_disp = _longest_matches(src)

    # Optimal parse (in bits): literal = 9 bits, match = 17 bits.
    cost = [0] * (n + 1)
    choice = [0] * (n + 1)  # 0 = literal, otherwise match length
    for i in range(n - 1, -1, -1):
        c = cost[i + 1] + 9
        ch = 0
        ml = best_len[i]
        if ml >= _MIN_LEN:
            for length in range(_MIN_LEN, ml + 1):
                mc = cost[i + length] + 17
                if mc < c:
                    c, ch = mc, length
        cost[i] = c
        choice[i] = ch

    out = bytearray(b"\x10" + n.to_bytes(3, "little"))
    i = 0
    while i < n:
        flag_pos = len(out)
        out.append(0)
        flags = 0
        for bit in range(8):
            if i >= n:
                break
            length = choice[i]
            if length:
                disp = best_disp[i] - 1
                out.append(((length - 3) << 4) | (disp >> 8))
                out.append(disp & 0xFF)
                flags |= 0x80 >> bit
                i += length
            else:
                out.append(src[i])
                i += 1
        out[flag_pos] = flags
    while len(out) % 4:
        out.append(0)
    return bytes(out)

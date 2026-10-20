"""Gen 3 (western) character encoding, enough to read names out of the ROM."""

from __future__ import annotations

import re

CHARMAP: dict[int, str] = {0x00: " ", 0xAB: "!", 0xAC: "?", 0xAD: ".", 0xAE: "-", 0xB0: "…",
                           0xB1: "“", 0xB2: "”", 0xB3: "‘", 0xB4: "'", 0xB5: "♂", 0xB6: "♀",
                           0xB8: ",", 0xBA: "/", 0x1B: "é", 0x5C: "(", 0x5D: ")", 0x2D: "&",
                           0xF0: ":"}
for _i in range(10):
    CHARMAP[0xA1 + _i] = str(_i)
for _i in range(26):
    CHARMAP[0xBB + _i] = chr(ord("A") + _i)
    CHARMAP[0xD5 + _i] = chr(ord("a") + _i)
# accented capitals/lowercase used by the European releases
for _code, _ch in zip(range(0x01, 0x1B), "ÀÁÂÇÈÉÊËÌ ÎÏÒÓÔŒÙÚÛÑßàá çè"):
    if _ch != " ":
        CHARMAP.setdefault(_code, _ch)
for _code, _ch in zip(range(0x1C, 0x2D), "êëì îïòóôœùúûñºª"):
    if _ch != " ":
        CHARMAP.setdefault(_code, _ch)

TERMINATOR = 0xFF


def decode(raw: bytes | bytearray, strict: bool = False) -> str | None:
    out = []
    for b in raw:
        if b == TERMINATOR:
            return "".join(out)
        ch = CHARMAP.get(b)
        if ch is None:
            if strict:
                return None
            ch = "?"
        out.append(ch)
    return None if strict else "".join(out)


def encode(s: str) -> bytes:
    rev = {v: k for k, v in CHARMAP.items()}
    return bytes(rev[c] for c in s) + bytes([TERMINATOR])


def slug(name: str) -> str:
    name = name.replace("♂", "_m").replace("♀", "_f").replace("é", "e").replace("'", "")
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "unnamed"

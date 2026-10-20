"""ROM container, header parsing and the global pointer index."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np

ROM_BASE = 0x08000000
MAX_ROM_SIZE = 0x02000000  # 32 MiB

GAMES = {
    "BPR": "FireRed",
    "BPG": "LeafGreen",
    "BPE": "Emerald",
    "AXV": "Ruby",
    "AXP": "Sapphire",
}
REGIONS = {"E": "USA/Europe", "J": "Japan", "D": "Germany", "F": "France", "I": "Italy", "S": "Spain"}


@dataclass(frozen=True)
class GameInfo:
    title: str
    code: str
    version: int
    family: str  # "frlg" | "rse" | "unknown"
    name: str

    @property
    def num_primary_tile_palettes(self) -> int:
        return 7 if self.family == "frlg" else 6

    @property
    def num_primary_tiles(self) -> int:
        return 640 if self.family == "frlg" else 512

    @property
    def num_primary_metatiles(self) -> int:
        return 640 if self.family == "frlg" else 512


def parse_header(data: bytes | bytearray) -> GameInfo:
    if len(data) < 0xC0:
        raise ValueError("file is too small to be a GBA ROM")
    title = bytes(data[0xA0:0xAC]).rstrip(b"\0").decode("ascii", "replace")
    code = bytes(data[0xAC:0xB0]).decode("ascii", "replace")
    version = data[0xBC]
    prefix = code[:3]
    if prefix in ("BPR", "BPG"):
        family = "frlg"
    elif prefix in ("BPE", "AXV", "AXP"):
        family = "rse"
    else:
        family = "unknown"
    name = GAMES.get(prefix, "Unknown game")
    region = REGIONS.get(code[3:4])
    if region:
        name = f"{name} ({region})"
    return GameInfo(title=title, code=code, version=version, family=family, name=name)


def sha1(data: bytes | bytearray) -> str:
    return hashlib.sha1(data).hexdigest()


class Rom:
    def __init__(self, data: bytes | bytearray):
        self.data = bytearray(data)
        self.info = parse_header(self.data)
        self._words: np.ndarray | None = None
        self._pointers: PointerIndex | None = None

    @classmethod
    def load(cls, path) -> "Rom":
        with open(path, "rb") as fh:
            return cls(fh.read())

    def __len__(self) -> int:
        return len(self.data)

    def u8(self, off: int) -> int:
        return self.data[off]

    def u16(self, off: int) -> int:
        return self.data[off] | (self.data[off + 1] << 8)

    def u32(self, off: int) -> int:
        return int.from_bytes(self.data[off:off + 4], "little")

    def is_ptr(self, value: int) -> bool:
        return ROM_BASE <= value < ROM_BASE + len(self.data)

    def ptr(self, off: int) -> int | None:
        """Read a pointer at ``off`` and return the file offset it targets, or None."""
        if off < 0 or off + 4 > len(self.data):
            return None
        v = self.u32(off)
        return v - ROM_BASE if self.is_ptr(v) else None

    @property
    def words(self) -> np.ndarray:
        """The ROM viewed as little-endian u32 words (index k == file offset 4k)."""
        if self._words is None:
            n = len(self.data) // 4
            self._words = np.frombuffer(bytes(self.data[: n * 4]), dtype="<u4").astype(np.int64)
        return self._words

    @property
    def pointers(self) -> "PointerIndex":
        if self._pointers is None:
            self._pointers = PointerIndex(self)
        return self._pointers


class PointerIndex:
    """Every 4-byte aligned word in the ROM that looks like a pointer into the ROM."""

    def __init__(self, rom: Rom):
        w = rom.words
        mask = (w >= ROM_BASE) & (w < ROM_BASE + len(rom.data))
        locs = np.nonzero(mask)[0] * 4
        targets = w[mask] - ROM_BASE
        order = np.argsort(targets, kind="stable")
        self.targets = targets[order]
        self.locations = locs[order]
        self.unique_targets = np.unique(self.targets)
        self.is_ptr_word = mask

    def refs_to(self, target: int) -> list[int]:
        lo = np.searchsorted(self.targets, target, "left")
        hi = np.searchsorted(self.targets, target, "right")
        return [int(x) for x in self.locations[lo:hi]]

    def is_target(self, target: int) -> bool:
        i = np.searchsorted(self.unique_targets, target)
        return i < len(self.unique_targets) and int(self.unique_targets[i]) == target

    def next_target_after(self, offset: int) -> int | None:
        i = np.searchsorted(self.unique_targets, offset, "right")
        return int(self.unique_targets[i]) if i < len(self.unique_targets) else None

    def any_target_in(self, start: int, end: int) -> bool:
        i = np.searchsorted(self.unique_targets, start, "left")
        return i < len(self.unique_targets) and int(self.unique_targets[i]) < end

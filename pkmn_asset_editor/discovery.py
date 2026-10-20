"""Locate graphics tables in a Gen 3 ROM by their structure.

Nothing here relies on hardcoded offsets: every table is recognised from
the shape of its entries (pointer + size + sequential tag, and so on), so
the same code works for every language/revision of FireRed, LeafGreen,
Ruby, Sapphire and Emerald, and for ROM hacks whose tables were repointed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import lz77, text
from .rom import ROM_BASE, Rom

ANIM_TAG_START = 10000
SHINY_TAG = 500
ICON_PAL_TAG = 56000

OAM_DIMS = {
    (0, 0): (1, 1), (0, 1): (2, 2), (0, 2): (4, 4), (0, 3): (8, 8),
    (1, 0): (2, 1), (1, 1): (4, 1), (1, 2): (4, 2), (1, 3): (8, 4),
    (2, 0): (1, 2), (2, 1): (1, 4), (2, 2): (2, 4), (2, 3): (4, 8),
}


@dataclass
class SheetTable:
    """A table of ``{ptr, size, tag}`` or ``{ptr, tag, pad}`` entries."""
    offset: int
    entries: list[int | None]  # target file offsets (None = invalid entry)
    tags: list[int]
    sizes: list[int] = field(default_factory=list)


@dataclass
class OwGraphics:
    offset: int
    palette_tag: int
    width: int
    height: int
    size: int
    frames: list[int]  # file offsets of every frame


@dataclass
class Tileset:
    offset: int
    compressed: bool
    secondary: bool
    tiles: int
    palettes: int
    metatiles: int


@dataclass
class Discovery:
    mon_pic_tables: dict[str, SheetTable] = field(default_factory=dict)
    mon_palettes: SheetTable | None = None
    mon_shiny_palettes: SheetTable | None = None
    species_names: list[str] = field(default_factory=list)
    mon_icons: list[int] = field(default_factory=list)
    mon_icon_palette_indices: list[int] = field(default_factory=list)
    mon_icon_palettes: SheetTable | None = None
    trainer_pics: SheetTable | None = None
    trainer_palettes: SheetTable | None = None
    item_icons: list[tuple[int, int]] = field(default_factory=list)
    item_names: list[str] = field(default_factory=list)
    anim_pics: SheetTable | None = None
    anim_palettes: SheetTable | None = None
    anim_frame_dims: dict[int, tuple[int, int]] = field(default_factory=dict)
    anim_backgrounds: list[tuple[int, int, int]] = field(default_factory=list)
    battle_backgrounds: list[tuple[int, int, int, int, int]] = field(default_factory=list)
    ow_graphics: list[OwGraphics] = field(default_factory=list)
    ow_graphics_ids: dict[int, int] = field(default_factory=dict)  # info offset -> graphics id
    ow_palettes: dict[int, int] = field(default_factory=dict)  # tag -> palette offset
    tilesets: list[Tileset] = field(default_factory=list)
    tileset_pairs: dict[int, int] = field(default_factory=dict)  # secondary -> primary
    lz_blobs: list[int] = field(default_factory=list)  # every pointed-to LZ77 stream
    notes: list[str] = field(default_factory=list)


class _Scan:
    def __init__(self, rom: Rom):
        self.rom = rom
        w = rom.words
        n = len(w)
        self.w = np.concatenate([w, np.zeros(16, dtype=np.int64)])  # guard band for k+i lookups
        self.n = n
        valid = (w >= ROM_BASE) & (w < ROM_BASE + len(rom.data))
        self.valid = np.concatenate([valid, np.zeros(16, dtype=bool)])
        tgt = np.where(self.valid, self.w - ROM_BASE, 0)
        self.tgt = tgt
        aligned = self.valid & (tgt % 4 == 0) & (tgt // 4 < n)
        hdr = np.zeros_like(self.w)
        hdr[aligned] = self.w[tgt[aligned] // 4]
        self.lzs = np.where(aligned & ((hdr & 0xFF) == 0x10), hdr >> 8, 0)
        self.lo = self.w & 0xFFFF
        self.hi = self.w >> 16

    def target(self, k: int) -> int | None:
        return int(self.tgt[k]) if self.valid[k] else None

    def shift(self, arr: np.ndarray, d: int) -> np.ndarray:
        """``arr[k + d]`` for every k in [0, n)."""
        return arr[d:d + self.n]


def _runs(scan: _Scan, starts: np.ndarray, stride: int, ok, min_len: int, max_bad: int = 2) -> list[tuple[int, int]]:
    """Extend each start index while ``ok(k, i)`` holds. Returns (start_word, length) runs."""
    found = []
    covered_until = -1
    for k0 in (int(k) for k in np.nonzero(starts)[0]):
        if k0 < covered_until:
            continue
        i = 0
        bad = 0
        while k0 + stride * i < scan.n:
            r = ok(k0 + stride * i, i)
            if r is False:
                break
            if r is None:  # tag matches but pointer is not usable
                bad += 1
                if bad > max_bad:
                    break
            else:
                bad = 0
            i += 1
        if i >= min_len:
            found.append((k0, i))
            covered_until = k0 + stride * i
    return found


def _sheet_table(scan: _Scan, k0: int, length: int, tag_in_hi: bool) -> SheetTable:
    entries, tags, sizes = [], [], []
    for i in range(length):
        k = k0 + 2 * i
        t = scan.target(k)
        entries.append(t if t is not None and scan.lzs[k] else None)
        if tag_in_hi:
            tags.append(int(scan.hi[k + 1]))
            sizes.append(int(scan.lo[k + 1]))
        else:
            tags.append(int(scan.lo[k + 1]))
    return SheetTable(k0 * 4, entries, tags, sizes)


def _find_sheet_tables(scan: _Scan, base: int, sizes: tuple[int, ...] | None, min_len: int) -> list[SheetTable]:
    """CompressedSpriteSheet tables ``{ptr, u16 size, u16 tag}`` with tag == base + i."""
    lzs, lo, hi = scan.lzs, scan.lo, scan.hi
    starts = (scan.shift(lzs, 0) > 0) & (scan.shift(hi, 1) == base)
    if sizes:
        starts &= np.isin(scan.shift(lo, 1), sizes)

    def ok(k, i):
        if int(hi[k + 1]) != base + i:
            return False
        if sizes and int(lo[k + 1]) not in sizes:
            return False
        return True if lzs[k] else None

    return [_sheet_table(scan, k0, n, True) for k0, n in _runs(scan, starts, 2, ok, min_len)]


def _find_palette_tables(scan: _Scan, base: int, min_len: int, compressed: bool = True,
                         pal_size: int | None = 0x20) -> list[SheetTable]:
    """CompressedSpritePalette / SpritePalette tables ``{ptr, u16 tag, u16 0}`` with tag == base + i."""
    lzs, lo, hi = scan.lzs, scan.lo, scan.hi

    def ptr_ok(k):
        if compressed:
            return bool(lzs[k]) and (pal_size is None or int(lzs[k]) == pal_size)
        return bool(scan.valid[k])

    starts = (scan.shift(lo, 1) == base) & (scan.shift(hi, 1) == 0) & scan.shift(scan.valid, 0)
    if compressed:
        starts &= scan.shift(lzs, 0) > 0

    def ok(k, i):
        if int(lo[k + 1]) != base + i or int(hi[k + 1]) != 0:
            return False
        return True if ptr_ok(k) else None

    out = []
    for k0, n in _runs(scan, starts, 2, ok, min_len):
        t = _sheet_table(scan, k0, n, False)
        if not compressed:
            t.entries = [scan.target(k0 + 2 * i) for i in range(n)]
        out.append(t)
    return out


def _species_names(rom: Rom, count: int) -> list[str]:
    data = bytes(rom.data)
    for stride, qmarks in ((11, 10), (6, 5)):
        pat = b"\xAC" * qmarks + b"\xFF"
        start = 0
        while True:
            pos = data.find(pat, start)
            if pos < 0:
                break
            start = pos + 1
            names = []
            for i in range(count):
                raw = data[pos + i * stride: pos + (i + 1) * stride]
                s = text.decode(raw, strict=True)
                if s is None:
                    break
                names.append(s)
            if len(names) >= min(count, 200) and names[1:4] and all(n and n[0].isalpha() for n in names[1:4]):
                return names
    return []


def _item_names(rom: Rom) -> list[str]:
    data = bytes(rom.data)
    pat = b"\xAC" * 8 + b"\xFF"
    start = 0
    while True:
        pos = data.find(pat, start)
        if pos < 0:
            return []
        start = pos + 1
        if pos % 4 or rom.u16(pos + 14) != 0 or pos + 44 * 3 > len(data):
            continue
        if rom.u16(pos + 44 + 14) != 1 or rom.u16(pos + 88 + 14) != 2:
            continue
        names = []
        i = 0
        while pos + 44 * (i + 1) <= len(data) and rom.u16(pos + 44 * i + 14) == i:
            s = text.decode(data[pos + 44 * i: pos + 44 * i + 14], strict=True)
            if s is None:
                break
            names.append(s)
            i += 1
        return names


def _anim_frame_dims(scan: _Scan, ntags: int) -> dict[int, tuple[int, int]]:
    """Infer anim sprite frame sizes from the SpriteTemplates that use each tag."""
    rom = scan.rom
    tile_tag = scan.shift(scan.lo, 0)
    m = (tile_tag >= ANIM_TAG_START) & (tile_tag < ANIM_TAG_START + ntags)
    m &= scan.shift(scan.valid, 1) & (scan.shift(scan.w, 3) == 0) & scan.shift(scan.valid, 5)
    m &= (scan.shift(scan.w, 5) & 1) == 1
    votes: dict[int, dict[tuple[int, int], int]] = {}
    for k in np.nonzero(m)[0]:
        k = int(k)
        oam = scan.target(k + 1)
        if oam is None or oam + 4 > len(rom.data):
            continue
        word = rom.u32(oam)
        shape, size = (word >> 14) & 3, (word >> 30) & 3
        dims = OAM_DIMS.get((shape, size))
        if dims:
            tag = int(scan.lo[k])
            v = votes.setdefault(tag, {})
            v[dims] = v.get(dims, 0) + 1
    return {tag: max(v.items(), key=lambda kv: kv[1])[0] for tag, v in votes.items()}


def _find_icons(scan: _Scan, d: Discovery) -> None:
    rom = scan.rom
    tables = _find_palette_tables(scan, ICON_PAL_TAG, 3, compressed=False)
    if not tables:
        return
    pals = tables[0]
    d.mon_icon_palettes = pals
    npals = len(pals.entries)
    P = pals.offset
    run = 0
    while P - run - 1 >= 0 and rom.data[P - run - 1] < npals:
        run += 1
    for pad in range(4):
        n = run - pad
        if n < 100:
            break
        tstart = P - pad - n - 4 * n
        if tstart < 0 or tstart % 4:
            continue
        ptrs = [rom.ptr(tstart + 4 * i) for i in range(n)]
        if all(p is not None for p in ptrs):
            d.mon_icons = ptrs
            d.mon_icon_palette_indices = list(rom.data[P - pad - n: P - pad])
            return
    d.notes.append("icon palettes found but the icon table next to them was not")


def _find_item_icons(scan: _Scan) -> list[tuple[int, int]]:
    lzs = scan.lzs
    starts = (scan.shift(lzs, 0) == 0x120) & (scan.shift(lzs, 1) == 0x20)

    def ok(k, i):
        return int(lzs[k]) == 0x120 and int(lzs[k + 1]) == 0x20

    runs = _runs(scan, starts, 2, ok, 10, max_bad=0)
    if not runs:
        return []
    k0, n = max(runs, key=lambda r: r[1])
    return [(scan.target(k0 + 2 * i), scan.target(k0 + 2 * i + 1)) for i in range(n)]


def _is_tiles(n): return n > 0 and n % 32 == 0
def _is_pal(n): return n > 0 and n % 32 == 0 and n <= 0x200
def _is_map(n): return 0x500 <= n <= 0x1000 and n % 2 == 0


def _find_bg_tables(scan: _Scan, roles: str, min_len: int) -> list[tuple[int, ...]]:
    """Tables of all-LZ77 pointer tuples. ``roles`` is e.g. 'tpm' (tiles, palette, map)."""
    lzs = scan.lzs
    checks = {"t": _is_tiles, "p": _is_pal, "m": _is_map}
    stride = len(roles)

    def entry_ok(k):
        return all(checks[r](int(lzs[k + j])) for j, r in enumerate(roles))

    starts = np.ones(scan.n, dtype=bool)
    for j, r in enumerate(roles):
        col = scan.shift(lzs, j)
        if r == "t":
            starts &= (col > 0) & (col % 32 == 0)
        elif r == "p":
            starts &= (col > 0) & (col % 32 == 0) & (col <= 0x200)
        else:
            starts &= (col >= 0x500) & (col <= 0x1000) & (col % 2 == 0)
    runs = _runs(scan, starts, stride, lambda k, i: entry_ok(k), min_len, max_bad=0)
    if not runs:
        return []
    k0, n = max(runs, key=lambda r: r[1])
    return [tuple(scan.target(k0 + stride * i + j) for j in range(stride)) for i in range(n)]


def _find_ow(scan: _Scan, d: Discovery) -> None:
    rom = scan.rom
    pidx = rom.pointers
    sz = scan.shift(scan.hi, 1)
    wd = scan.shift(scan.lo, 2)
    ht = scan.shift(scan.hi, 2)
    dims_ok = np.isin(wd, (8, 16, 32, 64)) & np.isin(ht, (8, 16, 32, 64))
    m = (scan.shift(scan.lo, 0) == 0xFFFF) & dims_ok & (sz == wd * ht // 2)
    m &= scan.shift(scan.valid, 4) & scan.shift(scan.valid, 7)
    infos: list[OwGraphics] = []
    for k in (int(k) for k in np.nonzero(m)[0]):
        size = int(scan.hi[k + 1])
        images = scan.target(k + 7)
        frames = []
        off = images
        while off is not None and off + 8 <= len(rom.data) and len(frames) < 128:
            if frames and pidx.is_target(off):
                break
            p = rom.ptr(off)
            if p is None or rom.u16(off + 4) != size or p + size > len(rom.data):
                break
            frames.append(p)
            off += 8
        if frames:
            infos.append(OwGraphics(k * 4, int(scan.hi[k]), int(scan.lo[k + 2]), int(scan.hi[k + 2]), size, frames))
    d.ow_graphics = infos
    if not infos:
        return
    # graphics id order from the pointer table that lists the infos
    info_offs = np.array(sorted(i.offset for i in infos), dtype=np.int64)
    in_set = scan.valid[: scan.n] & np.isin(scan.tgt[: scan.n], info_offs)

    def ok(k, i):
        return bool(in_set[k]) if k < scan.n else False

    runs = _runs(scan, in_set, 1, ok, 8, max_bad=0)
    if runs:
        k0, n = max(runs, key=lambda r: r[1])
        for i in range(n):
            d.ow_graphics_ids.setdefault(int(scan.tgt[k0 + i]), i)
    # palettes by tag
    tags = np.array(sorted({i.palette_tag for i in infos}), dtype=np.int64)
    pm = scan.shift(scan.valid, 0) & np.isin(scan.shift(scan.lo, 1), tags) & (scan.shift(scan.hi, 1) == 0)
    hits = [int(k) for k in np.nonzero(pm)[0]]
    # prefer hits that sit in the longest consecutive run of SpritePalette entries
    best: list[int] = []
    cur: list[int] = []
    for k in hits:
        if cur and k == cur[-1] + 2:
            cur.append(k)
        else:
            cur = [k]
        if len(cur) > len(best):
            best = list(cur)
    for k in best + hits:
        tag = int(scan.lo[k + 1])
        t = scan.target(k)
        if t is not None and t + 32 <= len(rom.data):
            d.ow_palettes.setdefault(tag, t)


def _find_tilesets(scan: _Scan, d: Discovery) -> None:
    rom = scan.rom
    head = scan.shift(scan.w, 0)
    m = np.isin(head, (0x0000, 0x0001, 0x0100, 0x0101))
    for j in (1, 2, 3):
        m &= scan.shift(scan.valid, j)
    c4 = scan.shift(scan.w, 4)
    c5 = scan.shift(scan.w, 5)
    v4 = scan.shift(scan.valid, 4)
    v5 = scan.shift(scan.valid, 5)
    rse_order = v4 & ((c4 & 1) == 0) & ((c5 == 0) | (v5 & ((c5 & 1) == 1)))
    frlg_order = v5 & ((c5 & 1) == 0) & ((c4 == 0) | (v4 & ((c4 & 1) == 1)))
    m &= rse_order | frlg_order
    compressed = (head & 0xFF) == 1
    m &= ~compressed | ((scan.shift(scan.lzs, 1) > 0) & (scan.shift(scan.lzs, 1) % 32 == 0))
    found = []
    for k in (int(k) for k in np.nonzero(m)[0]):
        pal = scan.target(k + 2)
        if pal is None or pal + 512 > len(rom.data):
            continue
        if any(rom.data[pal + 2 * i + 1] & 0x80 for i in range(16)):
            continue
        found.append(Tileset(k * 4, bool(scan.w[k] & 1), bool(scan.w[k] & 0x100),
                             scan.target(k + 1), pal, scan.target(k + 3)))
    d.tilesets = found
    # pair secondaries with primaries through the map layouts that use them
    offs = np.array(sorted(t.offset for t in found), dtype=np.int64)
    if not len(offs):
        return
    ts_by_off = {t.offset: t for t in found}
    wv = scan.shift(scan.w, 0)
    hv = scan.shift(scan.w, 1)
    lm = (wv >= 1) & (wv <= 0x400) & (hv >= 1) & (hv <= 0x400) & scan.shift(scan.valid, 2) & scan.shift(scan.valid, 3)
    lm &= scan.shift(scan.valid, 4) & scan.shift(scan.valid, 5)
    lm &= np.isin(scan.shift(scan.tgt, 4), offs) & np.isin(scan.shift(scan.tgt, 5), offs)
    votes: dict[int, dict[int, int]] = {}
    for k in (int(k) for k in np.nonzero(lm)[0]):
        p, s = int(scan.tgt[k + 4]), int(scan.tgt[k + 5])
        if not ts_by_off[p].secondary and ts_by_off[s].secondary:
            v = votes.setdefault(s, {})
            v[p] = v.get(p, 0) + 1
    d.tileset_pairs = {s: max(v.items(), key=lambda kv: kv[1])[0] for s, v in votes.items()}


def discover(rom: Rom) -> Discovery:
    scan = _Scan(rom)
    d = Discovery()

    # --- Pokémon pictures -----------------------------------------------------
    pic_tables = _find_sheet_tables(scan, 0, (0x800, 0x1000), 40)
    if pic_tables:
        longest = max(len(t.entries) for t in pic_tables)
        mons = sorted([t for t in pic_tables if len(t.entries) >= max(250, int(longest * 0.9))], key=lambda t: t.offset)
        others = [t for t in pic_tables if t not in mons]
        if others:
            d.trainer_pics = max(others, key=lambda t: len(t.entries))

        def raw_size(t):
            e = next((x for x in t.entries[1:] if x is not None), None)
            return lz77.peek_header(rom.data, e) if e is not None else 0

        big = [t for t in mons if raw_size(t) > 0x800]
        small = [t for t in mons if raw_size(t) <= 0x800]
        if big:
            d.mon_pic_tables["front"] = big[0]
            for i, t in enumerate(big[1:]):
                d.mon_pic_tables[f"front_alt{i + 1}"] = t
            if len(small) == 1:
                d.mon_pic_tables["back"] = small[0]
            elif len(small) >= 2:
                d.mon_pic_tables["front_still"] = small[0]
                d.mon_pic_tables["back"] = small[1]
        else:
            names = ["front", "back"]
            for i, t in enumerate(small):
                d.mon_pic_tables[names[i] if i < 2 else f"pics{i}"] = t

    pal_tables = _find_palette_tables(scan, 0, 40)
    if pal_tables:
        pal_tables.sort(key=lambda t: -len(t.entries))
        d.mon_palettes = pal_tables[0]
        if len(pal_tables) > 1:
            d.trainer_palettes = pal_tables[1]
    shiny = _find_palette_tables(scan, SHINY_TAG, 100)
    if shiny:
        d.mon_shiny_palettes = max(shiny, key=lambda t: len(t.entries))
    count = max([len(t.entries) for t in d.mon_pic_tables.values()] or [0])
    if count:
        d.species_names = _species_names(rom, count)

    _find_icons(scan, d)

    # --- items ---------------------------------------------------------------
    d.item_icons = _find_item_icons(scan)
    if d.item_icons:
        d.item_names = _item_names(rom)

    # --- battle animations -----------------------------------------------------
    anim = _find_sheet_tables(scan, ANIM_TAG_START, None, 20)
    if anim:
        d.anim_pics = max(anim, key=lambda t: len(t.entries))
        d.anim_frame_dims = _anim_frame_dims(scan, len(d.anim_pics.entries))
    apal = _find_palette_tables(scan, ANIM_TAG_START, 20, pal_size=None)
    if apal:
        d.anim_palettes = max(apal, key=lambda t: len(t.entries))
    d.anim_backgrounds = _find_bg_tables(scan, "tpm", 8)
    d.battle_backgrounds = _find_bg_tables(scan, "tmtmp", 4)

    # --- overworld -------------------------------------------------------------
    _find_ow(scan, d)
    _find_tilesets(scan, d)

    # --- every LZ77 stream referenced by a pointer ------------------------------
    ks = np.nonzero(scan.lzs[: scan.n] > 0)[0]
    d.lz_blobs = sorted({int(scan.tgt[k]) for k in ks})
    return d

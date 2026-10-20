"""Build a small fake Gen 3 ROM that contains every table type the extractor understands.

No commercial ROM is needed for the test-suite: the structures here mirror the
retail layouts (pokefirered/pokeemerald), filled with generated artwork.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import numpy as np

from pkmn_asset_editor import lz77, text
from pkmn_asset_editor.gfx import encode_palette, encode_tiles

BASE = 0x08000000
N_SPECIES = 300
N_UNIQUE = 10
N_TRAINERS = 60
N_ITEMS = 30
N_ANIM = 30
N_ANIM_BG = 10
N_TERRAIN = 5
NPT = 640  # FireRed primary tile count


@dataclass
class Layout:
    """Where the generator put things, so tests can poke at the ROM directly."""
    front_table: int = 0
    back_table: int = 0
    pal_table: int = 0
    shiny_table: int = 0
    front_blobs: list[int] = field(default_factory=list)
    extra_refs: list[int] = field(default_factory=list)
    unknown_blob: int = 0
    tilesets: list[int] = field(default_factory=list)


def sprite_tiles(seed: int, w_tiles: int = 8, h_tiles: int = 8, noise: bool = False) -> bytes:
    rng = np.random.default_rng(seed)
    h, w = h_tiles * 8, w_tiles * 8
    if noise:
        img = rng.integers(0, 16, (h, w), dtype=np.uint8)
    else:
        img = np.zeros((h, w), dtype=np.uint8)
        for _ in range(4):
            x0, y0 = rng.integers(0, w - 8), rng.integers(0, h - 8)
            x1, y1 = rng.integers(x0 + 4, w + 1), rng.integers(y0 + 4, h + 1)
            img[y0:y1, x0:x1] = rng.integers(1, 16)
    tiles = img.reshape(h_tiles, 8, w_tiles, 8).swapaxes(1, 2).reshape(-1, 8, 8)
    return encode_tiles(tiles)


def palette(seed: int, n: int = 16) -> bytes:
    rng = random.Random(seed)
    cols = [(rng.randrange(0, 256, 8), rng.randrange(0, 256, 8), rng.randrange(0, 256, 8)) for _ in range(n)]
    return encode_palette(cols)


class Builder:
    def __init__(self, size: int = 0x400000):
        self.rom = bytearray(b"\xFF" * size)
        self.pos = 0x1000
        rng = random.Random(1234)
        # pseudo "code" region of random bytes
        for i in range(0x200, 0x1000):
            self.rom[i] = rng.randrange(256)
        self.rom[0xA0:0xAC] = b"POKEMON FIRE"
        self.rom[0xAC:0xB0] = b"BPRE"
        self.rom[0xB0:0xB2] = b"01"
        self.rom[0xBC] = 0
        self.code_ptr_slot = 0x400  # where we drop literal-pool style pointers

    def put(self, data: bytes, align: int = 4) -> int:
        self.pos = (self.pos + align - 1) & ~(align - 1)
        off = self.pos
        self.rom[off:off + len(data)] = data
        self.pos += len(data)
        # pad so the next item never starts inside 0xFF filler that looks free
        return off

    def lz(self, raw: bytes) -> int:
        return self.put(lz77.compress(raw))

    def u32(self, v: int) -> bytes:
        return v.to_bytes(4, "little")

    def ptr(self, off: int) -> bytes:
        return self.u32(BASE + off)

    def code_ref(self, off: int) -> int:
        loc = self.code_ptr_slot
        self.rom[loc:loc + 4] = self.ptr(off)
        self.code_ptr_slot += 4
        return loc


def build() -> tuple[bytes, Layout]:
    b = Builder()
    lay = Layout()

    # ---------------------------------------------------------------- pokémon
    fronts = [b.lz(sprite_tiles(i)) for i in range(N_UNIQUE)]
    backs = [b.lz(sprite_tiles(100 + i)) for i in range(N_UNIQUE)]
    pals = [b.lz(palette(i)) for i in range(N_UNIQUE)]
    shinies = [b.lz(palette(500 + i)) for i in range(N_UNIQUE)]
    q_front, q_back, q_pal = fronts[0], backs[0], pals[0]
    lay.front_blobs = fronts

    def table(entries) -> int:
        return b.put(b"".join(entries))

    lay.front_table = table(b.ptr(fronts[i] if i < N_UNIQUE else q_front) + (0x800).to_bytes(2, "little") +
                            i.to_bytes(2, "little") for i in range(N_SPECIES))
    lay.back_table = table(b.ptr(backs[i] if i < N_UNIQUE else q_back) + (0x800).to_bytes(2, "little") +
                           i.to_bytes(2, "little") for i in range(N_SPECIES))
    lay.pal_table = table(b.ptr(pals[i] if i < N_UNIQUE else q_pal) + i.to_bytes(2, "little") + b"\0\0"
                          for i in range(N_SPECIES))
    lay.shiny_table = table(b.ptr(shinies[i] if i < N_UNIQUE else shinies[0]) + (500 + i).to_bytes(2, "little") +
                            b"\0\0" for i in range(N_SPECIES))
    # an extra reference to front sprite 1 from "code", like a literal pool
    lay.extra_refs.append(b.code_ref(fronts[1]))

    names = bytearray()
    for i in range(N_SPECIES):
        s = "??????????" if i == 0 else f"MON{i}"
        enc = text.encode(s)
        names += enc + b"\0" * (11 - len(enc))
    b.put(bytes(names))

    # icons: table, palette indices, then the icon palette table
    icon_gfx = [b.put(sprite_tiles(700 + i, 4, 8)) for i in range(3)]
    icon_pals = [b.put(palette(800 + i)) for i in range(6)]
    b.pos = (b.pos + 3) & ~3
    tbl = b"".join(b.ptr(icon_gfx[i % 3]) for i in range(N_SPECIES))
    idx = bytes(i % 6 for i in range(N_SPECIES))
    blob = tbl + idx
    blob += b"\0" * ((-len(blob)) % 4)
    blob += b"".join(b.ptr(icon_pals[j]) + (56000 + j).to_bytes(2, "little") + b"\0\0" for j in range(6))
    b.put(blob)

    # ---------------------------------------------------------------- trainers
    tf = [b.lz(sprite_tiles(900 + i)) for i in range(N_TRAINERS)]
    tp = [b.lz(palette(1000 + i)) for i in range(N_TRAINERS)]
    table(b.ptr(tf[i]) + (0x800).to_bytes(2, "little") + i.to_bytes(2, "little") for i in range(N_TRAINERS))
    table(b.ptr(tp[i]) + i.to_bytes(2, "little") + b"\0\0" for i in range(N_TRAINERS))

    # ---------------------------------------------------------------- items
    items = bytearray()
    for i in range(N_ITEMS):
        s = "????????" if i == 0 else f"ITEM{i}"
        enc = text.encode(s)
        entry = enc + b"\0" * (14 - len(enc)) + i.to_bytes(2, "little") + bytes(28)
        items += entry
    b.put(bytes(items))
    ig = [b.lz(sprite_tiles(1100 + i, 3, 3)) for i in range(N_ITEMS)]
    ip = [b.lz(palette(1200 + i)) for i in range(N_ITEMS)]
    table(b.ptr(ig[i]) + b.ptr(ip[i]) for i in range(N_ITEMS))

    # ---------------------------------------------------------------- battle anims
    sizes = [(2, 2, 4), (4, 4, 1), (4, 4, 2), (8, 8, 1), (2, 4, 3)]  # frame w, h (tiles), frames
    ag, specs = [], []
    for i in range(N_ANIM):
        fw, fh, nf = sizes[i % len(sizes)]
        raw = b"".join(sprite_tiles(1300 + i * 7 + f, fw, fh) for f in range(nf))
        ag.append(b.lz(raw))
        specs.append((fw, fh, len(raw)))
    ap = [b.lz(palette(1400 + i)) for i in range(N_ANIM)]
    table(b.ptr(ag[i]) + specs[i][2].to_bytes(2, "little") + (10000 + i).to_bytes(2, "little") for i in range(N_ANIM))
    table(b.ptr(ap[i]) + (10000 + i).to_bytes(2, "little") + b"\0\0" for i in range(N_ANIM))
    # OAM data + sprite templates
    shape_of = {(1, 1): (0, 0), (2, 2): (0, 1), (4, 4): (0, 2), (8, 8): (0, 3), (2, 4): (2, 2)}
    oams = {}
    for dims, (shape, size) in shape_of.items():
        word0 = (shape << 14) | (size << 30)
        oams[dims] = b.put(word0.to_bytes(4, "little") + bytes(4))
    anim_cmds = b.put(bytes(8))
    for i in range(N_ANIM):
        fw, fh, _ = specs[i]
        tag = 10000 + i
        b.put(tag.to_bytes(2, "little") + tag.to_bytes(2, "little") + b.ptr(oams[(fw, fh)]) + b.ptr(anim_cmds) +
              b"\0\0\0\0" + b.ptr(anim_cmds) + b.u32(BASE + 0x301))

    def bg_parts(seed, map_size=0x800, nbanks=1, bank_base=0, shared_tiles=None):
        rng = np.random.default_rng(seed)
        ntiles = 24
        tiles = shared_tiles if shared_tiles is not None else b.lz(
            encode_tiles(rng.integers(0, 16, (ntiles, 8, 8), dtype=np.uint8) & 0x7))
        entries = []
        for j in range(map_size // 2):
            t = int(rng.integers(0, ntiles))
            e = t | (int(rng.integers(0, 2)) << 10) | ((bank_base + int(rng.integers(0, nbanks))) << 12)
            entries.append(e)
        tmap = b.lz(b"".join(e.to_bytes(2, "little") for e in entries))
        pal = b.lz(palette(seed + 77, 16 * nbanks))
        return tiles, pal, tmap

    abgs = [bg_parts(1500 + i) for i in range(N_ANIM_BG)]
    table(b.ptr(t) + b.ptr(p) + b.ptr(m) for t, p, m in abgs)
    terr = []
    for i in range(N_TERRAIN):
        t0, p, m0 = bg_parts(1600 + i, 0x1000, nbanks=3, bank_base=2)
        t1, _p, m1 = bg_parts(1650 + i, 0x800, nbanks=3, bank_base=2, shared_tiles=t0 if i == 0 else None)
        terr.append((t0, m0, t1, m1, p))
    table(b"".join(b.ptr(x) for x in e) for e in terr)

    # ---------------------------------------------------------------- overworld
    ow_pal_tags = [0x1100 + i for i in range(4)]
    owp = [b.put(palette(1700 + i)) for i in range(4)]
    table(b.ptr(owp[i]) + ow_pal_tags[i].to_bytes(2, "little") + b"\0\0" for i in range(4))
    b.put(bytes(8))  # {NULL, 0} terminator
    infos = []
    for g in range(12):
        w, h = ((16, 32), (32, 32), (16, 16))[g % 3]
        size = w * h // 2
        nframes = 3 + g % 4
        frames = [b.put(sprite_tiles(1800 + g * 10 + f, w // 8, h // 8)) for f in range(nframes)]
        images = b.put(b"".join(b.ptr(f) + size.to_bytes(2, "little") + b"\0\0" for f in frames))
        info = b.put((0xFFFF).to_bytes(2, "little") + ow_pal_tags[g % 4].to_bytes(2, "little") +
                     (0x1111).to_bytes(2, "little") + size.to_bytes(2, "little") + w.to_bytes(2, "little") +
                     h.to_bytes(2, "little") + bytes([0x02, 0, 0, 0]) + b.ptr(oams[(2, 2)]) +
                     b"\0\0\0\0" + b.ptr(anim_cmds) + b.ptr(images) + b.ptr(anim_cmds))
        infos.append(info)
    table(b.ptr(i) for i in infos)

    # ---------------------------------------------------------------- tilesets
    def tileset(seed, secondary, ntiles, compressed=True):
        rng = np.random.default_rng(seed)
        raw = encode_tiles(rng.integers(0, 16, (ntiles, 8, 8), dtype=np.uint8) & 0x3)
        tiles = b.lz(raw) if compressed else b.put(raw)
        pals = b.put(b"".join(palette(seed * 16 + j) for j in range(16)))
        nmeta = 40
        entries = []
        for m in range(nmeta):
            for j in range(8):
                if secondary and j % 2:
                    t = NPT + int(rng.integers(0, ntiles))
                    bank = 7 + int(rng.integers(0, 3))
                else:
                    t = int(rng.integers(0, 64))
                    bank = int(rng.integers(0, 3))
                entries.append(t | (bank << 12))
        metatiles = b.put(b"".join(e.to_bytes(2, "little") for e in entries))
        attrs = b.put(bytes(4 * nmeta))
        struct = bytes([1 if compressed else 0, 1 if secondary else 0, 0, 0]) + b.ptr(tiles) + b.ptr(pals) + \
            b.ptr(metatiles) + (b.u32(BASE + 0x501) if secondary else b"\0\0\0\0") + b.ptr(attrs)
        return b.put(struct)

    prim = [tileset(30 + i, False, 64) for i in range(2)]
    sec = [tileset(40 + i, True, 48, compressed=(i == 0)) for i in range(2)]
    lay.tilesets = prim + sec
    border = b.put(bytes(8))
    mapdata = b.put(bytes(32))
    for p, s in ((prim[0], sec[0]), (prim[1], sec[1]), (prim[0], sec[0])):
        b.put(b.u32(4) + b.u32(4) + b.ptr(border) + b.ptr(mapdata) + b.ptr(p) + b.ptr(s))

    # ---------------------------------------------------------------- unidentified
    lay.unknown_blob = b.lz(sprite_tiles(1900, 4, 4))
    b.code_ref(lay.unknown_blob)
    return bytes(b.rom), lay

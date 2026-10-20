"""Turn discovered tables into a catalog of named, de-duplicated assets.

Data model
----------
* A **blob** is one contiguous piece of ROM data (LZ77-compressed or raw).
* An **asset** is something a person or an AI edits: one file in the project
  (a PNG or a JASC .pal). Assets reference blobs; several assets may share a
  blob (the build merges their edits tile-by-tile / colour-by-colour).
* Assets that would be byte-identical views of the same data are collapsed
  into one asset with ``aliases``.
"""

from __future__ import annotations

import bisect
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import lz77, text
from .discovery import ANIM_TAG_START, Discovery
from .gfx import Layout, looks_like_palette
from .rom import Rom

UNOWN_FORMS = list("BCDEFGHIJKLMNOPQRSTUVWXYZ") + ["exclamation", "question"]


@dataclass
class Blob:
    offset: int
    compression: str  # "lz77" | "none"
    raw_size: int
    stored_size: int
    unit: int = 32  # granularity used when merging edits from several assets

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Asset:
    id: str
    kind: str  # "sprite" | "palette" | "tileset" | "bg"
    category: str
    label: str
    blobs: dict
    layout: dict = field(default_factory=dict)
    palettes: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    sha1: str = ""
    path: str = ""  # explicit file (source-tree mode); ROM projects derive it from the id

    @property
    def file(self) -> str:
        if self.path:
            return self.path
        return "graphics/" + self.id + (".pal" if self.kind == "palette" else ".png")

    def blob_offsets(self) -> list[int]:
        out = []
        for v in self.blobs.values():
            out.extend(v if isinstance(v, list) else [v])
        return out

    def to_dict(self) -> dict:
        d = asdict(self)
        d["file"] = self.file
        if not self.path:
            d.pop("path")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Asset":
        d = dict(d)
        d.pop("file", None)
        return cls(**d)


class CatalogBuilder:
    def __init__(self, rom: Rom, disc: Discovery):
        self.rom = rom
        self.disc = disc
        self.blobs: dict[int, Blob] = {}
        self.raw: dict[int, bytes] = {}
        self.assets: dict[str, Asset] = {}
        self._by_key: dict[tuple, str] = {}
        self.warnings: list[str] = []

    # ------------------------------------------------------------------ blobs
    def lz(self, off: int | None, unit: int = 32) -> int | None:
        if off is None:
            return None
        if off in self.blobs:
            return off
        res = lz77.try_decompress(self.rom.data, off)
        if res is None:
            return None
        raw, used = res
        self.blobs[off] = Blob(off, "lz77", len(raw), used, unit)
        self.raw[off] = raw
        return off

    def plain(self, off: int | None, size: int, unit: int = 32) -> int | None:
        if off is None or off + size > len(self.rom.data) or size <= 0:
            return None
        b = self.blobs.get(off)
        if b is not None:
            return off if b.raw_size >= size else None
        self.blobs[off] = Blob(off, "none", size, size, unit)
        self.raw[off] = bytes(self.rom.data[off:off + size])
        return off

    # ------------------------------------------------------------------ assets
    def add(self, asset: Asset, key: tuple | None = None) -> str:
        """Add an asset, or alias it to an existing asset with the same key. Returns the canonical id."""
        if key is not None and key in self._by_key:
            canon = self._by_key[key]
            if asset.id != canon:
                self.assets[canon].aliases.append(asset.id)
            return canon
        if asset.id in self.assets:
            raise ValueError(f"duplicate asset id {asset.id}")
        self.assets[asset.id] = asset
        if key is not None:
            self._by_key[key] = asset.id
        return asset.id

    def palette(self, aid: str, off: int | None, category: str, label: str, compressed: bool = True,
                size: int = 32, meta: dict | None = None) -> str | None:
        blob = self.lz(off, unit=2) if compressed else self.plain(off, size, unit=2)
        if blob is None:
            return None
        b = self.blobs[blob]
        if b.raw_size % 2 or b.raw_size == 0:
            return None
        return self.add(Asset(aid, "palette", category, label, {"data": blob},
                              {"colors": b.raw_size // 2}, meta=meta or {}), ("palette", blob))

    def sprite(self, aid: str, blob: int | None, layout: Layout, palette: str | None, category: str,
               label: str, meta: dict | None = None, frames: list[int] | None = None) -> str | None:
        if frames is not None:
            blobs = {"frames": frames}
        elif blob is not None:
            blobs = {"tiles": blob}
        else:
            return None
        links = [{"palette": palette, "png_bank": 0, "pal_bank": 0}] if palette else []
        key = ("sprite", tuple(frames) if frames else blob, palette)
        return self.add(Asset(aid, "sprite", category, label, blobs, layout.to_dict(), links, meta or {}), key)

    # ------------------------------------------------------------------ categories
    def species_label(self, i: int) -> str:
        names = self.disc.species_names
        if i < len(names) and names[i].strip("? "):
            return names[i]
        if i == 0:
            return "none"
        if len(names) == 412 and i == 412:
            return "egg"
        if len(names) == 412 and 413 <= i < 413 + len(UNOWN_FORMS):
            return f"unown_{UNOWN_FORMS[i - 413]}"
        return f"species_{i}"

    def build_pokemon(self) -> None:
        d = self.disc
        count = max([len(t.entries) for t in d.mon_pic_tables.values()] or [0])
        for i in range(count):
            name = self.species_label(i)
            base = f"pokemon/{i:03d}_{text.slug(name)}"
            meta = {"species": i, "name": name}
            normal = shiny = None
            if d.mon_palettes and i < len(d.mon_palettes.entries):
                normal = self.palette(f"{base}/normal", d.mon_palettes.entries[i], "pokemon",
                                      f"{name} palette", meta=meta)
            if d.mon_shiny_palettes and i < len(d.mon_shiny_palettes.entries):
                shiny = self.palette(f"{base}/shiny", d.mon_shiny_palettes.entries[i], "pokemon",
                                     f"{name} shiny palette", meta=meta)
            for kind, table in d.mon_pic_tables.items():
                if i >= len(table.entries):
                    continue
                blob = self.lz(table.entries[i])
                if blob is None:
                    continue
                n = self.blobs[blob].raw_size // 32
                layout = Layout.frames_of(n, 8, 8) if n % 64 == 0 else Layout.sheet(n, 8)
                self.sprite(f"{base}/{kind}", blob, layout, normal, "pokemon", f"{name} {kind.replace('_', ' ')}",
                            {**meta, "table": kind, "alt_palettes": [shiny] if shiny else []})
            if d.mon_icons and i < len(d.mon_icons):
                pal = None
                if d.mon_icon_palette_indices and d.mon_icon_palettes:
                    pi = d.mon_icon_palette_indices[i]
                    pal = self.palette(f"pokemon/_icon_palettes/{pi}", d.mon_icon_palettes.entries[pi], "pokemon",
                                       f"icon palette {pi}", compressed=False)
                blob = self.plain(d.mon_icons[i], 0x400)
                if blob is not None:
                    self.sprite(f"{base}/icon", blob, Layout.frames_of(32, 4, 4), pal, "pokemon", f"{name} icon",
                                {**meta, "table": "icon"})

    def build_trainers(self) -> None:
        d = self.disc
        if not d.trainer_pics:
            return
        for i, off in enumerate(d.trainer_pics.entries):
            blob = self.lz(off)
            if blob is None:
                continue
            base = f"trainers/{i:03d}"
            pal = None
            if d.trainer_palettes and i < len(d.trainer_palettes.entries):
                pal = self.palette(f"{base}/palette", d.trainer_palettes.entries[i], "trainers", f"trainer {i} palette")
            n = self.blobs[blob].raw_size // 32
            layout = Layout.frames_of(n, 8, 8) if n % 64 == 0 else Layout.sheet(n, 8)
            self.sprite(f"{base}/front", blob, layout, pal, "trainers", f"trainer pic {i}", {"trainer_pic": i})

    def build_items(self) -> None:
        d = self.disc
        for i, (gfx, palp) in enumerate(d.item_icons):
            name = d.item_names[i] if i < len(d.item_names) and d.item_names[i].strip("? ") else (
                "none" if i == 0 else f"item_{i}")
            base = f"items/{i:03d}_{text.slug(name)}"
            pal = self.palette(f"{base}/palette", palp, "items", f"{name} icon palette")
            blob = self.lz(gfx)
            if blob is not None:
                self.sprite(f"{base}/icon", blob, Layout.frames_of(self.blobs[blob].raw_size // 32, 3, 3), pal,
                            "items", f"{name} icon", {"item": i, "name": name})

    def build_battle_anims(self) -> None:
        d = self.disc
        pals = {}
        if d.anim_palettes:
            for i, off in enumerate(d.anim_palettes.entries):
                tag = ANIM_TAG_START + i
                pals[tag] = self.palette(f"battle_anims/sprites/{tag}/palette", off, "battle_anims",
                                         f"anim tag {tag} palette", meta={"tag": tag})
        if d.anim_pics:
            for i, off in enumerate(d.anim_pics.entries):
                tag = ANIM_TAG_START + i
                blob = self.lz(off)
                if blob is None:
                    continue
                n = self.blobs[blob].raw_size // 32
                dims = d.anim_frame_dims.get(tag)
                if dims and n % (dims[0] * dims[1]) == 0:
                    layout = Layout.frames_of(n, dims[0], dims[1])
                else:
                    f = next((f for f in (8, 4, 2, 1) if f * f <= n and n % (f * f) == 0), 1)
                    layout = Layout.frames_of(n, f, f)
                self.sprite(f"battle_anims/sprites/{tag}/gfx", blob, layout, pals.get(tag), "battle_anims",
                            f"anim tag {tag}", {"tag": tag, "frame_dims_from_oam": bool(dims)})
        for i, (tiles, pal, tmap) in enumerate(d.anim_backgrounds):
            base = f"battle_anims/backgrounds/{i:02d}"
            self.bg(f"{base}/bg", tiles, tmap, pal, f"{base}/palette", "battle_anims", f"anim background {i}",
                    {"anim_bg": i})
        for i, (t0, m0, t1, m1, pal) in enumerate(d.battle_backgrounds):
            base = f"battle_backgrounds/{i:02d}"
            self.bg(f"{base}/scene", t0, m0, pal, f"{base}/palette", "battle_backgrounds", f"battle terrain {i}",
                    {"terrain": i})
            self.bg(f"{base}/entry", t1, m1, pal, f"{base}/palette", "battle_backgrounds",
                    f"battle terrain {i} entry", {"terrain": i})

    def bg(self, aid, tiles_off, map_off, pal_off, pal_id, category, label, meta) -> None:
        tiles = self.lz(tiles_off)
        tmap = self.lz(map_off, unit=2)
        if tiles is None or tmap is None:
            return
        pal = self.palette(pal_id, pal_off, category, f"{label} palette")
        raw_map = self.raw[tmap]
        entries = [raw_map[i] | (raw_map[i + 1] << 8) for i in range(0, len(raw_map) - 1, 2)]
        nbanks = max(1, self.blobs[self.lz(pal_off, unit=2)].raw_size // 32) if pal else 1
        used = sorted({e >> 12 for e in entries})
        base = used[0] if used and used[-1] - used[0] < nbanks else 0
        base = min(base, 16 - min(nbanks, 16))
        if len(raw_map) == 0x1000:
            mw, mh = 64, 32
        else:
            mw, mh = 32, len(entries) // 32
        links = []
        if pal:
            for b in range(16):
                if b in used or base <= b < base + nbanks:
                    links.append({"palette": pal, "png_bank": b, "pal_bank": (b - base) % nbanks})
        layout = {"map_w": mw, "map_h": mh, "tiles": self.blobs[tiles].raw_size // 32, "bank_base": base}
        self.add(Asset(aid, "bg", category, label, {"tiles": tiles, "map": tmap}, layout, links, meta),
                 ("bg", tiles, tmap, pal))

    def build_overworld(self) -> None:
        d = self.disc
        pals = {}
        for tag, off in sorted(d.ow_palettes.items()):
            pals[tag] = self.palette(f"overworld/palettes/{tag:04X}", off, "overworld", f"overworld palette {tag:04X}",
                                     compressed=False, meta={"tag": tag})
        for g in sorted(d.ow_graphics, key=lambda g: (d.ow_graphics_ids.get(g.offset, 1 << 30), g.offset)):
            gid = d.ow_graphics_ids.get(g.offset)
            name = f"{gid:03d}" if gid is not None else f"x{g.offset:06X}"
            frames = []
            for f in g.frames:
                b = self.plain(f, g.size)
                if b is None:
                    frames = []
                    break
                frames.append(b)
            if not frames:
                continue
            fw, fh = g.width // 8, g.height // 8
            layout = Layout(fw, fh, len(frames), len(frames), len(frames) * fw * fh)
            self.sprite(f"overworld/{name}/sprite", None, layout, pals.get(g.palette_tag), "overworld",
                        f"overworld graphics {name}",
                        {"graphics_id": gid, "palette_tag": g.palette_tag, "info_offset": g.offset}, frames=frames)

    def build_tilesets(self) -> None:
        d = self.disc
        info = self.rom.info
        npt, npp, npm = info.num_primary_tiles, info.num_primary_tile_palettes, info.num_primary_metatiles
        pidx = self.rom.pointers
        mcounts: dict[int, int] = {}
        ids: dict[int, str] = {}
        counters = Counter()
        votes: dict[int, list[Counter]] = {}
        tile_blob: dict[int, int] = {}
        for t in sorted(d.tilesets, key=lambda t: t.offset):
            kind = "secondary" if t.secondary else "primary"
            cap = (1024 - npt if t.secondary else npt) * 32
            if t.compressed:
                blob = self.lz(t.tiles)
            else:
                nxt = pidx.next_target_after(t.tiles) or len(self.rom.data)
                size = min(cap, (nxt - t.tiles) // 32 * 32)
                blob = self.plain(t.tiles, size)
            if blob is None:
                continue
            ids[t.offset] = f"tilesets/{kind}_{counters[kind]:02d}"
            counters[kind] += 1
            tile_blob[t.offset] = blob
            votes[t.offset] = [Counter() for _ in range(self.blobs[blob].raw_size // 32)]
        for t in d.tilesets:
            if t.offset not in ids:
                continue
            mcap = (1024 - npm) if t.secondary else npm
            nxt = pidx.next_target_after(t.metatiles) or len(self.rom.data)
            count = max(0, min(mcap, (nxt - t.metatiles) // 16))
            primary = d.tileset_pairs.get(t.offset) if t.secondary else None
            for m in range(count):
                for j in range(8):
                    v = self.rom.u16(t.metatiles + m * 16 + j * 2)
                    tile, bank = v & 0x3FF, v >> 12
                    if tile < npt:
                        owner, local = (primary if t.secondary else t.offset), tile
                    else:
                        owner, local = (t.offset if t.secondary else None), tile - npt
                    if owner in votes and local < len(votes[owner]):
                        votes[owner][local][bank] += 1
            mcounts[t.offset] = count
        for t in sorted(d.tilesets, key=lambda t: t.offset):
            if t.offset not in ids:
                continue
            tid = ids[t.offset]
            pal_id = self.palette(f"{tid}/palettes", t.palettes, "tilesets", f"{tid} palettes",
                                  compressed=False, size=512)
            primary = d.tileset_pairs.get(t.offset) if t.secondary else None
            ppal = None
            if primary is not None and primary in ids:
                ppal = self.assets.get(f"{ids[primary]}/palettes")
                ppal = ppal.id if ppal else None
            links = []
            for b in range(16):
                src = ppal if (t.secondary and ppal and b < npp) else pal_id
                if src:
                    links.append({"palette": src, "png_bank": b, "pal_bank": b})
            default_bank = npp if t.secondary else 0
            banks = "".join("%x" % (v.most_common(1)[0][0] if v else default_bank) for v in votes[t.offset])
            n = len(votes[t.offset])
            self.add(Asset(f"{tid}/tiles", "tileset", "tilesets", f"{tid} tiles", {"tiles": tile_blob[t.offset]},
                           {"tiles_wide": 16, "count": n, "banks": banks}, links,
                           {"struct_offset": t.offset, "secondary": t.secondary, "compressed": t.compressed,
                            "metatiles_offset": t.metatiles, "metatile_count": mcounts.get(t.offset, 0),
                            "primary": ids.get(primary) if primary is not None else None}),
                     ("tileset", tile_blob[t.offset]))

    def build_unknown(self) -> None:
        claimed = sorted((b.offset, b.offset + b.stored_size) for b in self.blobs.values())
        starts = [c[0] for c in claimed]

        def inside_claimed(off):
            i = bisect.bisect_right(starts, off) - 1
            return i >= 0 and claimed[i][0] <= off < claimed[i][1]

        last_end = -1
        for off in self.disc.lz_blobs:
            if off < last_end or off in self.blobs or inside_claimed(off):
                continue
            res = lz77.try_decompress(self.rom.data, off)
            if res is None:
                continue
            raw, used = res
            if len(raw) < 32 or len(raw) % 32:
                continue
            last_end = off + used
            if looks_like_palette(raw) and len(raw) <= 0x200:
                self.palette(f"unknown/palettes/{off:06X}", off, "unknown", f"unidentified palette @{off:06X}",
                             meta={"guess": "palette"})
            else:
                blob = self.lz(off)
                n = self.blobs[blob].raw_size // 32
                self.sprite(f"unknown/{off:06X}", blob, Layout.sheet(n, 16), None, "unknown",
                            f"unidentified graphics @{off:06X}", {"guess": "tiles"})

    def build(self) -> tuple[dict[str, Asset], dict[int, Blob], dict[int, bytes]]:
        for step in (self.build_pokemon, self.build_trainers, self.build_items, self.build_battle_anims,
                     self.build_overworld, self.build_tilesets, self.build_unknown):
            step()
        self._mark_bg_modes()
        return self.assets, self.blobs, self.raw

    def _mark_bg_modes(self) -> None:
        users = Counter()
        for a in self.assets.values():
            for off in a.blob_offsets():
                users[off] += 1
        for a in self.assets.values():
            if a.kind != "bg":
                continue
            tiles, tmap = a.blobs["tiles"], a.blobs["map"]
            raw = self.raw[tmap]
            n = self.blobs[tiles].raw_size // 32
            in_range = all(((raw[i] | (raw[i + 1] << 8)) & 0x3FF) < n for i in range(0, len(raw) - 1, 2))
            a.layout["retile"] = users[tiles] == 1 and users[tmap] == 1 and in_range


def build_catalog(rom: Rom, disc: Discovery):
    cb = CatalogBuilder(rom, disc)
    assets, blobs, raw = cb.build()
    return assets, blobs, raw, cb.warnings

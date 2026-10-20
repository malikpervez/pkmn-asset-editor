"""Metatile & tileset editing for pokeemerald-style source trees.

A metatile is 8 tile entries (2 layers x 2x2, each: tile id, h/v flip, palette
bank) in ``metatiles.bin`` plus an attribute word in ``metatile_attributes.bin``
(behavior + layer type; FRLG layouts use 32-bit attributes with more fields).
Tile ids below NUM_TILES_IN_PRIMARY address the primary tileset's ``tiles.png``,
the rest the secondary's. This module edits those files, adds tiles and
metatiles within the engine's limits, and turns artwork into new metatiles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from .decomp import DecompError, strip_comments
from .gfx import Layout, image_to_tiles, load_png, pad_palette, quantize_new_palette, read_jasc, remap_to_palette, \
    tiles_to_image, write_jasc

LAYER_TYPES = ["METATILE_LAYER_TYPE_NORMAL", "METATILE_LAYER_TYPE_COVERED", "METATILE_LAYER_TYPE_SPLIT"]


def _flip(t: np.ndarray, h: int, v: int) -> np.ndarray:
    if h:
        t = t[:, ::-1]
    if v:
        t = t[::-1, :]
    return t


@dataclass
class Side:
    """One tileset (primary or secondary) as seen from a layout."""
    symbol: str
    tiles_file: str
    metatiles_file: str
    attrs_file: str
    palette_files: list[str]
    tile_base: int  # global id of this tileset's tile 0
    metatile_base: int
    tile_limit: int
    metatile_limit: int


class TilesetEditor:
    def __init__(self, maps):
        self.maps = maps
        self.dp = maps.dp
        self.root: Path = maps.root
        self._behaviors: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ layout context
    def sides(self, layout: dict) -> tuple[Side, Side, str]:
        version = layout.get("layout_version", "emerald")
        npm, npt, _npp = self.maps.consts(version)
        out = []
        for key, base_t, base_m, lim_t, lim_m in (("primary_tileset", 0, 0, npt, npm),
                                                  ("secondary_tileset", npt, npm, 1024 - npt, 1024 - npm)):
            ts = self.maps.tilesets.get(layout[key])
            if ts is None or not ts.tiles or not ts.metatiles:
                raise DecompError(f"tileset {layout[key]} could not be resolved from src/data/tilesets")
            attrs = ts.attributes or str(Path(ts.metatiles).with_name("metatile_attributes.bin"))
            out.append(Side(ts.symbol, ts.tiles, ts.metatiles, attrs, ts.palettes, base_t, base_m, lim_t, lim_m))
        return out[0], out[1], version

    def _attr_size(self, version: str) -> int:
        return 4 if version == "frlg" else 2

    # ------------------------------------------------------------------ raw file access
    def _read_metas(self, side: Side) -> np.ndarray:
        p = self.root / side.metatiles_file
        raw = p.read_bytes() if p.exists() else b""
        return np.frombuffer(raw[: len(raw) // 16 * 16], dtype="<u2").astype(np.int32).reshape(-1, 8).copy()

    def _read_attrs(self, side: Side, version: str, count: int) -> list[int]:
        p = self.root / side.attrs_file
        raw = p.read_bytes() if p.exists() else b""
        size = self._attr_size(version)
        vals = [int.from_bytes(raw[i:i + size], "little") for i in range(0, len(raw) - size + 1, size)]
        return (vals + [0] * count)[:count]

    def _write_metas(self, side: Side, version: str, metas: np.ndarray, attrs: list[int]) -> None:
        for f in (side.metatiles_file, side.attrs_file):
            self.dp._backup(f)
        (self.root / side.metatiles_file).write_bytes(np.ascontiguousarray(metas, dtype="<u2").tobytes())
        size = self._attr_size(version)
        (self.root / side.attrs_file).write_bytes(b"".join(int(a).to_bytes(size, "little") for a in attrs))
        self.dp._changed = None

    def _tile_count(self, side: Side) -> int:
        """Tiles the build actually emits for this tileset (honours -num_tiles)."""
        n = self._num_tiles_arg(side)
        if n is not None:
            return n
        with Image.open(self.root / side.tiles_file) as im:
            return (im.width // 8) * (im.height // 8)

    def _num_tiles_arg(self, side: Side) -> int | None:
        for r in self.dp.refs_by_file.get(side.tiles_file, []):
            m = re.search(r"-num_tiles\s+(\d+)", r.args)
            if m:
                return int(m.group(1))
        return None

    def read_tiles(self, side: Side) -> tuple[np.ndarray, int]:
        img = load_png(self.root / side.tiles_file)
        if img.indices is None:
            raise DecompError(f"{side.tiles_file} is not an indexed PNG (run `pae fix`)")
        w = img.width // 8
        lay = Layout.sheet(w * (img.height // 8), w)
        return image_to_tiles(img.indices & 0x0F, lay), w

    def _write_tiles(self, side: Side, tiles: np.ndarray, width_tiles: int, count: int) -> None:
        """Write tiles.png (keeping its palette) and keep -num_tiles in step with ``count``."""
        rows = -(-len(tiles) // width_tiles)
        padded = np.zeros((rows * width_tiles, 8, 8), np.uint8)
        padded[: len(tiles)] = tiles
        lay = Layout.sheet(len(padded), width_tiles)
        cur = load_png(self.root / side.tiles_file)
        self.dp._write_png(side.tiles_file, tiles_to_image(padded, lay), cur.palette or [(i * 16,) * 3 for i in range(16)], cur)
        old = self._num_tiles_arg(side)
        if old is not None and old != count:
            self._set_num_tiles_arg(side, count)

    def _set_num_tiles_arg(self, side: Side, count: int) -> None:
        for r in self.dp.refs_by_file.get(side.tiles_file, []):
            if "-num_tiles" not in r.args:
                continue
            src = self.root / r.src
            text = src.read_text()
            pat = re.compile(r'(INCGFX_\w+\(\s*"' + re.escape(side.tiles_file) + r'"[^)]*-num_tiles\s+)(\d+)')
            new, n = pat.subn(lambda m: m.group(1) + str(count), text)
            if n:
                self.dp._backup(r.src)
                src.write_text(new)
                r.args = re.sub(r"-num_tiles\s+\d+", f"-num_tiles {count}", r.args)

    # ------------------------------------------------------------------ queries
    def behaviors(self, version: str) -> list[str]:
        """Behavior constant names in enum order (index == value)."""
        key = "frlg" if version == "frlg" else "rse"
        if key not in self._behaviors:
            names: list[str] = []
            f = self.root / "include" / "constants" / ("metatile_behaviors_frlg.h" if key == "frlg" else "metatile_behaviors.h")
            if f.exists():
                body = re.search(r"enum\s*\w*\s*\{(.*?)\}", strip_comments(f.read_text()), re.S)
                value = 0
                for item in (body.group(1).split(",") if body else []):
                    item = item.strip()
                    if not item:
                        continue
                    name, _, val = item.partition("=")
                    name = name.strip()
                    if val.strip():
                        try:
                            value = int(val.strip(), 0)
                        except ValueError:
                            pass
                    while len(names) < value:
                        names.append(f"0x{len(names):02X}")
                    names.append(name)
                    value += 1
            self._behaviors[key] = names
        return self._behaviors[key]

    def info(self, map_name: str) -> dict:
        layout = self.maps.layout_of(map_name)
        prim, sec, version = self.sides(layout)
        npm, npt, npp = self.maps.consts(version)
        out = {"version": version, "num_primary_metatiles": npm, "num_primary_tiles": npt, "num_primary_palettes": npp}
        for name, side in (("primary", prim), ("secondary", sec)):
            out[name] = {"symbol": side.symbol, "tiles_file": side.tiles_file, "metatiles_file": side.metatiles_file,
                         "metatiles": len(self._read_metas(side)), "metatile_limit": side.metatile_limit,
                         "tiles": self._tile_count(side), "tile_limit": side.tile_limit,
                         "num_tiles_arg": self._num_tiles_arg(side)}
        return out

    def _side_of_metatile(self, layout: dict, mid: int) -> tuple[Side, int, str]:
        prim, sec, version = self.sides(layout)
        side = prim if mid < sec.metatile_base else sec
        return side, mid - side.metatile_base, version

    def get_metatile(self, map_name: str, mid: int) -> dict:
        layout = self.maps.layout_of(map_name)
        side, local, version = self._side_of_metatile(layout, mid)
        metas = self._read_metas(side)
        if not 0 <= local < len(metas):
            raise DecompError(f"metatile 0x{mid:03X} does not exist")
        attr = self._read_attrs(side, version, len(metas))[local]
        return {"id": mid, "tileset": side.symbol,
                "entries": [{"tile": int(v) & 0x3FF, "hflip": bool(v >> 10 & 1), "vflip": bool(v >> 11 & 1),
                             "palette": int(v) >> 12} for v in metas[local]],
                **self._unpack_attr(attr, version), "raw_attr": attr}

    def _unpack_attr(self, attr: int, version: str) -> dict:
        if version == "frlg":
            return {"behavior": attr & 0x1FF, "layer_type": (attr >> 29) & 3}
        return {"behavior": attr & 0xFF, "layer_type": (attr >> 12) & 0xF}

    def _pack_attr(self, old: int, behavior: int, layer_type: int, version: str) -> int:
        if version == "frlg":
            return (old & ~0x1FF & ~(3 << 29)) | (behavior & 0x1FF) | ((layer_type & 3) << 29)
        return (old & ~0xFF & ~0xF000) | (behavior & 0xFF) | ((layer_type & 0xF) << 12)

    # ------------------------------------------------------------------ edits
    def set_metatile(self, map_name: str, mid: int, entries: list[dict] | None = None,
                     behavior: int | str | None = None, layer_type: int | str | None = None) -> None:
        layout = self.maps.layout_of(map_name)
        side, local, version = self._side_of_metatile(layout, mid)
        metas = self._read_metas(side)
        if not 0 <= local < len(metas):
            raise DecompError(f"metatile 0x{mid:03X} does not exist")
        attrs = self._read_attrs(side, version, len(metas))
        if entries is not None:
            if len(entries) != 8:
                raise DecompError("a metatile has exactly 8 tile entries (bottom layer 0-3, top layer 4-7)")
            for i, e in enumerate(entries):
                t = int(e.get("tile", 0))
                pal = int(e.get("palette", 0))
                if not (0 <= t < 1024 and 0 <= pal < 16):
                    raise DecompError("tile must be 0-1023 and palette 0-15")
                metas[local, i] = t | (bool(e.get("hflip")) << 10) | (bool(e.get("vflip")) << 11) | (pal << 12)
        cur = self._unpack_attr(attrs[local], version)
        b = self._behavior_value(behavior, version) if behavior is not None else cur["behavior"]
        lt = self._layer_value(layer_type) if layer_type is not None else cur["layer_type"]
        attrs[local] = self._pack_attr(attrs[local], b, lt, version)
        self._write_metas(side, version, metas, attrs)

    def _behavior_value(self, behavior, version) -> int:
        if isinstance(behavior, int) or str(behavior).isdigit():
            return int(behavior)
        names = self.behaviors(version)
        if behavior not in names:
            raise DecompError(f"unknown behavior {behavior}")
        return names.index(behavior)

    @staticmethod
    def _layer_value(layer) -> int:
        if isinstance(layer, int) or str(layer).isdigit():
            return int(layer)
        name = str(layer).upper()
        for i, n in enumerate(LAYER_TYPES):
            if name in (n, n.removeprefix("METATILE_LAYER_TYPE_")):
                return i
        raise DecompError(f"unknown layer type {layer}")

    def add_metatiles(self, map_name: str, count: int = 1, copy_from: int | None = None,
                      where: str = "secondary") -> list[int]:
        layout = self.maps.layout_of(map_name)
        prim, sec, version = self.sides(layout)
        side = sec if where == "secondary" else prim
        metas = self._read_metas(side)
        attrs = self._read_attrs(side, version, len(metas))
        if len(metas) + count > side.metatile_limit:
            raise DecompError(f"{side.symbol} has {len(metas)} metatiles; the limit is {side.metatile_limit}")
        if copy_from is not None:
            src_side, src_local, _ = self._side_of_metatile(layout, copy_from)
            src_metas = self._read_metas(src_side)
            row = src_metas[src_local].copy()
            attr = self._read_attrs(src_side, version, len(src_metas))[src_local]
        else:
            row = np.full(8, self._blank_tile(layout), dtype=np.int32)
            attr = self._pack_attr(0, 0, 0, version)
        new = np.vstack([metas, np.tile(row, (count, 1))]) if len(metas) else np.tile(row, (count, 1))
        self._write_metas(side, version, new, attrs + [attr] * count)
        return [side.metatile_base + len(metas) + i for i in range(count)]

    def _blank_tile(self, layout: dict) -> int:
        """Global id of an all-transparent tile (primary tile 0 is blank in every vanilla tileset)."""
        prim, sec, _ = self.sides(layout)
        for side in (prim, sec):
            tiles, _ = self.read_tiles(side)
            hits = np.nonzero(~tiles[: self._tile_count(side)].reshape(len(tiles[: self._tile_count(side)]), -1).any(1))[0]
            if len(hits):
                return side.tile_base + int(hits[0])
        return self.add_tiles(None, 1, "secondary", layout)[0]

    def add_tiles(self, map_name: str | None, count: int = 1, where: str = "secondary",
                  layout: dict | None = None, pixels: list[np.ndarray] | None = None) -> list[int]:
        """Append tiles (blank, or the given 8x8 index arrays) and return their global ids."""
        layout = layout or self.maps.layout_of(map_name)
        prim, sec, _ = self.sides(layout)
        side = sec if where == "secondary" else prim
        tiles, width = self.read_tiles(side)
        n = self._tile_count(side)
        if n + count > side.tile_limit:
            raise DecompError(f"{side.symbol} has {n} tiles; the limit is {side.tile_limit}")
        need = n + count
        if len(tiles) < need:
            tiles = np.concatenate([tiles, np.zeros((need - len(tiles), 8, 8), np.uint8)])
        for i in range(count):
            tiles[n + i] = pixels[i] if pixels is not None else 0
        width = width or 16
        self._write_tiles(side, tiles[: max(need, len(tiles))], width, need)
        return [side.tile_base + n + i for i in range(count)]

    def set_tile(self, map_name: str, tile: int, pixels) -> None:
        layout = self.maps.layout_of(map_name)
        prim, sec, _ = self.sides(layout)
        side = prim if tile < sec.tile_base else sec
        local = tile - side.tile_base
        tiles, width = self.read_tiles(side)
        if not 0 <= local < self._tile_count(side):
            raise DecompError(f"tile {tile} does not exist")
        px = np.array(pixels, dtype=np.uint8).reshape(8, 8)
        if px.max() > 15:
            raise DecompError("tile pixels are colour indices 0-15")
        tiles[local] = px
        self._write_tiles(side, tiles, width, self._tile_count(side))

    def get_tile(self, map_name: str, tile: int) -> list[list[int]]:
        layout = self.maps.layout_of(map_name)
        prim, sec, _ = self.sides(layout)
        side = prim if tile < sec.tile_base else sec
        tiles, _ = self.read_tiles(side)
        local = tile - side.tile_base
        if not 0 <= local < len(tiles):
            raise DecompError(f"tile {tile} does not exist")
        return tiles[local].tolist()

    # ------------------------------------------------------------------ rendering helpers
    def palettes(self, map_name: str) -> list[list[tuple[int, int, int]]]:
        layout = self.maps.layout_of(map_name)
        prim, sec, version = self.sides(layout)
        _npm, _npt, npp = self.maps.consts(version)
        out = []
        for b in range(16):
            side = prim if b < npp else sec
            try:
                out.append(pad_palette(read_jasc(self.root / side.palette_files[b]), 16)[:16])
            except (OSError, ValueError, IndexError):
                out.append([(0, 0, 0)] * 16)
        return out

    def tile_sheet(self, map_name: str, bank: int, scale: int = 1, transparent: bool = False) -> Image.Image:
        """Every tile the layout can use (primary, then secondary from NUM_TILES_IN_PRIMARY) in palette ``bank``."""
        layout = self.maps.layout_of(map_name)
        prim, sec, _ = self.sides(layout)
        pal = np.array(self.palettes(map_name)[bank], np.uint8)
        ptiles, _ = self.read_tiles(prim)
        stiles, _ = self.read_tiles(sec)
        total = sec.tile_base + self._tile_count(sec)
        allt = np.zeros((total, 8, 8), np.uint8)
        n = min(self._tile_count(prim), sec.tile_base)
        allt[:n] = ptiles[:n]
        m = self._tile_count(sec)
        allt[sec.tile_base:sec.tile_base + m] = stiles[:m]
        img = tiles_to_image(allt, Layout.sheet(total, 16))
        if transparent:
            rgba = np.dstack([pal[img], np.where(img == 0, 0, 255).astype(np.uint8)])
            im = Image.fromarray(rgba, "RGBA")
        else:
            im = Image.fromarray(pal[img], "RGB")
        return im.resize((im.width * scale, im.height * scale), Image.NEAREST) if scale > 1 else im

    # ------------------------------------------------------------------ artwork -> metatiles
    def import_image(self, map_name: str, src, base_metatile: int | None = None, bank: int | None = None,
                     new_palette: bool = False, behavior: int | str = 0, resize_to: tuple[int, int] | None = None) -> dict:
        """Turn artwork into new tiles + metatiles in the secondary tileset and return a paintable stamp.

        * ``bank``: palette to use (default: the one that represents the art best). With ``new_palette`` the
          art's colours are written into that bank of the secondary tileset (other tiles using it change).
        * ``base_metatile``: its bottom layer goes under the art (e.g. grass), so transparent pixels show
          ground; the art goes on the top layer with layer type COVERED (drawn below the player).
          Without a base, the art is the bottom layer of a NORMAL metatile.
        """
        layout = self.maps.layout_of(map_name)
        prim, sec, version = self.sides(layout)
        _npm, npt, npp = self.maps.consts(version)
        with Image.open(src) as im:
            im = im.convert("RGBA")
            if resize_to:
                im = im.resize(resize_to, Image.NEAREST if im.width <= resize_to[0] else Image.LANCZOS)
            w, h = -(-im.width // 16) * 16, -(-im.height // 16) * 16
            canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            canvas.paste(im, (0, h - im.height))  # bottom-aligned, like a building on the ground
            rgba = np.array(canvas, np.uint8)
        notes: list[str] = []
        pals = self.palettes(map_name)
        own_banks = [b for b in range(npp, 16) if b < len(sec.palette_files)]
        if new_palette:
            if bank is None:
                bank = own_banks[-1] if own_banks else npp
            if bank < npp:
                raise DecompError(f"bank {bank} belongs to the primary tileset; pick {npp}-{own_banks[-1] if own_banks else 12}")
            idx, cols = quantize_new_palette(rgba, 16)
            self.dp._backup(sec.palette_files[bank])
            write_jasc(self.root / sec.palette_files[bank], cols)
            notes.append(f"wrote the art's colours into palette {bank} ({sec.palette_files[bank]}); "
                         f"other tiles drawn with palette {bank} change too")
        else:
            candidates = list(range(13)) if bank is None else [bank]  # 13 BG palettes are free for map tiles
            best = None
            for b in candidates:
                idx_b, bad = remap_to_palette(rgba, pals[b])
                if best is None or bad < best[2]:
                    best = (b, idx_b, bad)
            bank, idx, bad = best
            if bad:
                notes.append(f"{bad} pixels were approximated to palette {bank} (use new_palette to keep exact colours)")
        # tiles: reuse identical ones (with flips) from both tilesets, otherwise add to the secondary
        lookup: dict[bytes, tuple[int, int, int]] = {}
        for side in (prim, sec):
            tiles, _ = self.read_tiles(side)
            for t in range(min(self._tile_count(side), len(tiles))):
                for hf in (0, 1):
                    for vf in (0, 1):
                        lookup.setdefault(_flip(tiles[t], hf, vf).tobytes(), (side.tile_base + t, hf, vf))
        new_pixels: list[np.ndarray] = []
        first_new = sec.tile_base + self._tile_count(sec)
        cells: list[tuple[int, int, int]] = []
        for ty in range(0, h, 8):
            for tx in range(0, w, 8):
                px = idx[ty:ty + 8, tx:tx + 8]
                hit = lookup.get(px.tobytes())
                if hit is None:
                    gid = first_new + len(new_pixels)
                    new_pixels.append(px.copy())
                    for hf in (0, 1):
                        for vf in (0, 1):
                            lookup.setdefault(_flip(px, hf, vf).tobytes(), (gid, hf, vf))
                    hit = (gid, 0, 0)
                cells.append(hit)
        if new_pixels:
            self.add_tiles(None, len(new_pixels), "secondary", layout, new_pixels)
            notes.append(f"added {len(new_pixels)} new tile(s) to {sec.symbol}")
        # metatiles
        tw = w // 8
        if base_metatile is not None:
            base = self.get_metatile(map_name, base_metatile)["entries"]
            bottom = [base[i] for i in range(4)]
            layer_type = 1  # COVERED: both layers below sprites, ground shows through transparent pixels
        else:
            bottom = None
            layer_type = 0
        blank = self._blank_tile(layout)
        sec_metas = self._read_metas(sec)
        sec_attrs = self._read_attrs(sec, version, len(sec_metas))
        existing = {tuple(int(v) for v in row): sec.metatile_base + i for i, row in enumerate(sec_metas)}
        bval = self._behavior_value(behavior, version)
        attr = self._pack_attr(0, bval, layer_type, version)
        stamp, rows_new, attrs_new = [], [], []
        for my in range(h // 16):
            for mx in range(w // 16):
                quad = []
                for qy in (0, 1):
                    for qx in (0, 1):
                        t, hf, vf = cells[(my * 2 + qy) * tw + mx * 2 + qx]
                        quad.append(t | (hf << 10) | (vf << 11) | (bank << 12))
                if bottom is not None:
                    row = [e["tile"] | (e["hflip"] << 10) | (e["vflip"] << 11) | (e["palette"] << 12) for e in bottom] + quad
                else:
                    row = quad + [blank] * 4
                key = tuple(row)
                if key in existing and sec_attrs[existing[key] - sec.metatile_base] == attr:
                    stamp.append(existing[key])
                    continue
                mid = sec.metatile_base + len(sec_metas) + len(rows_new)
                rows_new.append(row)
                attrs_new.append(attr)
                existing[key] = mid
                stamp.append(mid)
        if len(sec_metas) + len(rows_new) > sec.metatile_limit:
            raise DecompError(f"the art needs {len(rows_new)} new metatiles but {sec.symbol} only has room for "
                              f"{sec.metatile_limit - len(sec_metas)}")
        if rows_new:
            metas = np.vstack([sec_metas, np.array(rows_new, np.int32)]) if len(sec_metas) else np.array(rows_new, np.int32)
            self._write_metas(sec, version, metas, sec_attrs + attrs_new)
            notes.append(f"added {len(rows_new)} metatile(s) to {sec.symbol}")
        return {"w": w // 16, "h": h // 16, "ids": stamp, "bank": bank, "notes": notes}

    # ------------------------------------------------------------------ validation
    def check(self) -> list[tuple[str, str, str]]:
        """Problems in tilesets touched by the current edits: (level, file, message)."""
        changed = self.dp.changed_files()
        issues = []
        seen = set()
        for lay in self.maps.layouts.values():
            try:
                prim, sec, version = self.sides(lay)
            except DecompError:
                continue
            for side in (prim, sec):
                if side.symbol in seen or not any(f in changed for f in (side.tiles_file, side.metatiles_file,
                                                                          side.attrs_file)):
                    continue
                seen.add(side.symbol)
                metas = self._read_metas(side)
                if len(metas) > side.metatile_limit:
                    issues.append(("error", side.metatiles_file, f"{len(metas)} metatiles, limit {side.metatile_limit}"))
                n_attr = ((self.root / side.attrs_file).stat().st_size // self._attr_size(version)
                          if (self.root / side.attrs_file).exists() else 0)
                if n_attr != len(metas):
                    issues.append(("error", side.attrs_file, f"{n_attr} attribute entries for {len(metas)} metatiles"))
                ntiles = self._tile_count(side)
                if ntiles > side.tile_limit:
                    issues.append(("error", side.tiles_file, f"{ntiles} tiles, limit {side.tile_limit}"))
                with Image.open(self.root / side.tiles_file) as im:
                    in_png = (im.width // 8) * (im.height // 8)
                if self._num_tiles_arg(side) is not None and self._num_tiles_arg(side) > in_png:
                    issues.append(("error", side.tiles_file, f"-num_tiles {self._num_tiles_arg(side)} but the PNG "
                                                             f"only has {in_png} tiles"))
                total_p = prim.tile_base + self._tile_count(prim)
                total_s = sec.tile_base + self._tile_count(sec)
                head = self.dp.original_bytes(side.metatiles_file) or b""
                head_rows = np.frombuffer(head[: len(head) // 16 * 16], dtype="<u2").reshape(-1, 8)
                bad = 0
                for i, row in enumerate(metas):
                    if i < len(head_rows) and np.array_equal(head_rows[i], row):
                        continue  # unchanged since the last commit: not this edit's problem
                    for v in row:
                        t = int(v) & 0x3FF
                        if not (t < total_p or sec.tile_base <= t < total_s):
                            bad += 1
                if bad:
                    issues.append(("warning", side.metatiles_file,
                                   f"{bad} tile entries point at tiles that don't exist (with {lay['id']}'s tilesets)"))
        return issues

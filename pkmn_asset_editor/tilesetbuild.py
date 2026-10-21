"""Tilesets from scratch: create a tileset, build it from artwork, paint a map with it.

* :func:`new_tileset` adds an empty primary or secondary tileset (files under ``data/tilesets/`` and its entries in
  ``graphics.h``, ``metatiles.h``, ``headers.h`` and ``include/tilesets.h``).
* :func:`build` turns block artwork into tiles, palettes and metatiles, as a JSON spec says (see BUILD_SPEC), and
  writes ``names.json``: each metatile's (or stamp's) id, for painting.
* :func:`paint` fills a map from a text grid: one character per block, a legend from characters to metatile names
  or stamps (a stamp's character fills its whole rectangle), collision and elevation.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

from .decomp import DecompError
from .gfx import read_jasc, write_jasc

BUILD_SPEC = """\
{
  "version": "hns",                         // layout version the tilesets are for (emerald, frlg, hns)
  "primary": "gTileset_X",                  // made with `pae tileset new` (either may be left out)
  "secondary": "gTileset_Y",
  "palettes": {"primary": ["a.pal", ...],   // JASC files (or lists of 16 [r,g,b]), bank by bank: the primary's
               "secondary": ["c.pal", ...]},//  start at bank 0, the secondary's after the primary's (bank 7 on FRLG)
  "reserve": {"primary": [[434, 441]]},     // global tile ids to leave blank (e.g. door animation slots)
  "animations": [                           // animated tiles: every metatile drawn from "image" animates; the
    {"name": "Water", "image": "water.png", //  frames are images laid out like it (frame 0 is the image itself)
     "frames": ["water_f1.png", "water_f2.png"], "speed": 16}],  // game frames per animation frame
  "field_effects": {                        // the tall grass drawn over the player's feet on this tileset's maps:
    "tall_grass": {"image": "grass_fx.png", //  five 16x16 frames side by side (as the game's), in the colours
                   "bank": 0, "tileset": "primary"}},  //  of one bank (usually the grass tiles' own)
  "metatiles": [
    {"name": "grass", "tileset": "primary", "image": "ground.png", "x": 0, "y": 0, "behavior": "MB_NORMAL"},
    {"name": "house", "tileset": "secondary", "image": "house.png", "x": 0, "y": 0, "w": 5, "h": 4,
     "ground": "grass",                     // what it shows goes under the art (transparent pixels show it)
     "over_rows": 1,                        // the top rows are drawn over the player (layer NORMAL)
     "behaviors": {"2,3": "MB_ANIMATED_DOOR"}, "solid": true, "open": ["2,3"],  // a door: walkable
     "banks": [7, 8], "row_banks": {"3": [1]}}  // palettes it may use (all its rows / one row)
  ]
}
Edge variants for `pae map paint`: name metatiles "path#0" ... "path#15" (the number says which neighbours are path
too: 1 north, 2 east, 4 south, 8 west) and use {"auto": "path"} in the legend; {"auto": "pond", "joins": "~",
"with": "lily"} counts as "~" for its neighbours and theirs, and draws "pond#15+lily" where that exists
("path#15~1", "path#15~2"... are
variants of a piece, spread by position; "path#15.1" is the piece for an inside corner, the diagonal neighbour not
path while both sides are (1 NE, 2 SE, 4 SW, 8 NW), used when drawn; a piece may be a stamp, e.g. 2x2 trees
"forest#0".."forest#15", and then its neighbours are the cells of its size); {"pick": ["grass", "grass2"]}
spreads variants; {"tile": "forest"} repeats a stamp as a pattern (block x, y shows its piece x mod w, y mod h)."""


def _snake(symbol: str) -> str:
    base = symbol.removeprefix("gTileset_")
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", base).lower()


def new_tileset(dp, symbol: str, secondary: bool, folder: str | None = None) -> dict:
    """Create an empty tileset ``symbol`` (gTileset_Name) and register it in the source."""
    if not re.fullmatch(r"gTileset_\w+", symbol):
        raise DecompError("a tileset is named gTileset_Something")
    root = Path(dp.root)
    headers = root / "src/data/tilesets/headers.h"
    if re.search(rf"\bstruct Tileset\s+{symbol}\b", headers.read_text()):
        raise DecompError(f"{symbol} already exists")
    name = symbol.removeprefix("gTileset_")
    folder = folder or f"data/tilesets/{'secondary' if secondary else 'primary'}/{_snake(symbol)}"
    d = root / folder
    if d.exists() and any(d.iterdir()):
        raise DecompError(f"{folder} already exists")
    (d / "palettes").mkdir(parents=True, exist_ok=True)
    blank = Image.new("P", (128, 8), 0)
    blank.putpalette([0, 0, 0] * 16 + [0, 0, 0] * 240)
    blank.save(d / "tiles.png")
    for i in range(16):
        write_jasc(d / "palettes" / f"{i:02d}.pal", [(0, 0, 0)] * 16)
    # one blank metatile so the binaries are never empty (the build replaces it)
    (d / "metatiles.bin").write_bytes(b"\0" * 16)
    (d / "metatile_attributes.bin").write_bytes(b"\0" * 4)
    edits = {
        "src/data/tilesets/graphics.h":
            f'\nconst u32 gTilesetTiles_{name}[] = INCBIN_U32("{folder}/tiles.4bpp.fastSmol");\n\n'
            f"const u16 gTilesetPalettes_{name}[][16] =\n{{\n"
            + "".join(f'    INCBIN_U16("{folder}/palettes/{i:02d}.gbapal"),\n' for i in range(16)) + "};\n",
        "src/data/tilesets/metatiles.h":
            f'\nconst u16 gMetatiles_{name}[] = INCBIN_U16("{folder}/metatiles.bin");\n'
            f'const u16 gMetatileAttributes_{name}[] = INCBIN_U16("{folder}/metatile_attributes.bin");\n',
        "src/data/tilesets/headers.h":
            f"\nconst struct Tileset {symbol} =\n{{\n    .isCompressed = TRUE,\n"
            f"    .isSecondary = {'TRUE' if secondary else 'FALSE'},\n    .tiles = gTilesetTiles_{name},\n"
            f"    .palettes = gTilesetPalettes_{name},\n    .metatiles = gMetatiles_{name},\n"
            f"    .metatileAttributes = gMetatileAttributes_{name},\n    .callback = NULL,\n}};\n",
    }
    for rel, text in edits.items():
        dp._backup(rel)
        p = root / rel
        p.write_text(p.read_text().rstrip("\n") + "\n" + text)
    inc = root / "include/tilesets.h"
    if inc.exists():
        t = inc.read_text()
        line = f"extern const struct Tileset {symbol};\n"
        m = list(re.finditer(r"^extern const struct Tileset \w+;\n", t, re.M))
        dp._backup("include/tilesets.h")
        inc.write_text(t[:m[-1].end()] + line + t[m[-1].end():] if m else t + line)
    dp._changed = None
    return {"symbol": symbol, "folder": folder, "secondary": secondary}


# ---------------------------------------------------------------------- build
def _palette(spec_dir: Path, p) -> list[tuple[int, int, int]]:
    cols = read_jasc(spec_dir / p) if isinstance(p, str) else [tuple(c) for c in p]
    cols = [tuple(int(v) for v in c[:3]) for c in cols][:16]
    return cols + [(0, 0, 0)] * (16 - len(cols))


def _gba(c) -> tuple[int, int, int]:
    """The colour as the GBA shows it (5 bits per channel)."""
    return tuple((v >> 3) << 3 for v in c)


class _Tiles:
    """Tile store of one tileset: dedupes tiles (with flips), honours reserved slots."""

    def __init__(self, base: int, limit: int, reserved: set[int], also: "_Tiles | None" = None):
        self.base, self.limit, self.reserved, self.also = base, limit, reserved, also
        self.pixels: list[np.ndarray] = [np.zeros((8, 8), np.uint8)]  # tile 0: blank
        self.lookup: dict[bytes, tuple[int, int, int]] = {}
        self._index(0)

    def _index(self, local: int) -> None:
        px = self.pixels[local]
        for hf in (0, 1):
            for vf in (0, 1):
                t = px[:, ::-1] if hf else px
                t = t[::-1, :] if vf else t
                self.lookup.setdefault(t.tobytes(), (self.base + local, hf, vf))

    def get(self, px: np.ndarray) -> tuple[int, int, int]:
        key = px.tobytes()
        if self.also is not None and key in self.also.lookup:
            return self.also.lookup[key]
        if key in self.lookup:
            return self.lookup[key]
        while self.base + len(self.pixels) in self.reserved:
            self.pixels.append(np.zeros((8, 8), np.uint8))
        if len(self.pixels) >= self.limit:
            raise DecompError(f"out of tiles: the tileset holds {self.limit}")
        self.pixels.append(px.copy())
        self._index(len(self.pixels) - 1)
        return self.lookup[key]


def build(maps, spec_path) -> dict:
    """Build the tilesets a spec describes (see BUILD_SPEC); returns the names map (also written as names.json)."""
    spec_path = Path(spec_path)
    spec = json.loads(re.sub(r"^\s*//.*$", "", spec_path.read_text(), flags=re.M))
    sdir = spec_path.parent
    version = spec.get("version", "emerald")
    npm, npt, npp = maps.consts(version)
    te = maps.tileset_editor
    sides = {}
    for key in ("primary", "secondary"):
        sym = spec.get(key)
        if sym:
            ts = maps.tilesets.get(sym)
            if ts is None:
                raise DecompError(f"unknown tileset {sym}; make it with `pae tileset new`")
            if ts.secondary != (key == "secondary"):
                raise DecompError(f"{sym} is {'a secondary' if ts.secondary else 'a primary'} tileset")
            sides[key] = ts
    if not sides:
        raise DecompError("the spec names no tileset (primary / secondary)")
    pals: dict[int, list] = {}
    for key, first in (("primary", 0), ("secondary", npp)):
        for i, p in enumerate(spec.get("palettes", {}).get(key, [])):
            bank = first + i
            if key == "primary" and bank >= npp or bank >= 13:
                raise DecompError(f"too many {key} palettes (banks {first}-{(npp if key == 'primary' else 13) - 1})")
            pals[bank] = _palette(sdir, p)
    reserved = {key: {t for a, b in spec.get("reserve", {}).get(key, []) for t in range(a, b + 1)}
                for key in ("primary", "secondary")}
    tiles = {"primary": _Tiles(0, npt, reserved["primary"])}
    tiles["secondary"] = _Tiles(npt, 1024 - npt, reserved["secondary"], also=tiles["primary"])
    metas: dict[str, list] = {"primary": [], "secondary": []}
    attrs: dict[str, list] = {"primary": [], "secondary": []}
    names: dict[str, dict] = {}
    images: dict[str, np.ndarray] = {}
    anims = []
    for a in spec.get("animations", []):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", a.get("name", "")):
            raise DecompError("animation names may use letters, digits and _ (e.g. Water)")
        if not a.get("frames"):
            raise DecompError(f"animation {a['name']}: give its frames (images laid out like {a.get('image')})")
        anims.append({"name": a["name"], "frames": [a["image"]] + list(a["frames"]), "speed": int(a.get("speed", 16)),
                      "where": None, "tiles": [], "keys": {}})
    anim_of = {a["frames"][0]: a for a in anims}

    def image(rel: str) -> np.ndarray:
        if rel not in images:
            with Image.open(sdir / rel) as im:
                images[rel] = np.array(im.convert("RGBA"), np.uint8)
        return images[rel]

    def to_tile(rgba: np.ndarray, banks: list[int]) -> tuple[int, np.ndarray]:
        """8x8 RGBA -> (bank, indices): the bank whose colours fit best (0 = transparent)."""
        opaque = rgba[..., 3] >= 128
        if not opaque.any():
            return banks[0], np.zeros((8, 8), np.uint8)
        rgb = rgba[..., :3].astype(np.int32)
        best = None
        for b in banks:
            pal = np.array([_gba(c) for c in pals[b][1:]], np.int32)  # index 0 is transparent
            d = ((rgb[..., None, :] - pal[None, None, :, :]) ** 2).sum(-1)
            idx = d.argmin(-1)
            err = int(np.where(opaque, d.min(-1), 0).sum())
            if best is None or err < best[0]:
                best = (err, b, np.where(opaque, idx + 1, 0).astype(np.uint8))
        return best[1], best[2]

    def _covers(e) -> bool:
        """Whether a layer entry is a tile with no transparent pixel."""
        if isinstance(e, tuple):  # an animated tile: its first frame
            a = next(x for x in anims if x["name"] == e[1])
            return bool(a["tiles"][e[2]][0].all())
        if not e:
            return False
        tid = e & 0x3FF
        for st in tiles.values():
            if st.base <= tid < st.base + len(st.pixels):
                px = st.pixels[tid - st.base]
                return bool(px.all())
        return False

    def entry(where: str, rgba: np.ndarray, banks: list[int]) -> int:
        bank, px = to_tile(rgba, banks)
        if not px.any():
            return 0
        tid, hf, vf = tiles[where].get(px)
        return tid | (hf << 10) | (vf << 11) | (bank << 12)

    def anim_entry(a: dict, where: str, ax: int, ay: int, banks: list[int]):
        """An animated tile: its pixels in every frame (frame 0's bank for all). Placed after the loop."""
        bank, px0 = to_tile(image(a["frames"][0])[ay:ay + 8, ax:ax + 8], banks)
        pxs = [px0]
        for f in a["frames"][1:]:
            fr = image(f)
            if fr.shape != image(a["frames"][0]).shape:
                raise DecompError(f"animation {a['name']}: {f} must be the size of {a['frames'][0]}")
            pxs.append(to_tile(fr[ay:ay + 8, ax:ax + 8], [bank])[1])
        if not any(p.any() for p in pxs):
            return 0
        if a["where"] not in (None, where):
            raise DecompError(f"animation {a['name']}: its metatiles must all be in one tileset")
        a["where"] = where
        key = b"".join(p.tobytes() for p in pxs)
        if key not in a["keys"]:
            a["keys"][key] = len(a["tiles"])
            a["tiles"].append(pxs)
        return ("anim", a["name"], a["keys"][key], bank)

    for m in spec.get("metatiles", []):
        where = m.get("tileset", "secondary" if "secondary" in sides else "primary")
        if where not in sides:
            raise DecompError(f"{m.get('name')}: the spec has no {where} tileset")
        w, h = int(m.get("w", 1)), int(m.get("h", 1))
        x0, y0 = int(m.get("x", 0)), int(m.get("y", 0))
        art = image(m["image"])[y0:y0 + h * 16, x0:x0 + w * 16]
        if art.shape[:2] != (h * 16, w * 16):
            raise DecompError(f"{m.get('name')}: {m['image']} has no {w * 16}x{h * 16} area at ({x0},{y0})")
        own = [b for b in pals if (b < npp) == (where == "primary")]
        banks = [int(b) for b in m.get("banks", own + ([b for b in pals if b < npp] if where == "secondary" else []))]
        row_banks = {int(k): [int(b) for b in v] for k, v in m.get("row_banks", {}).items()}
        missing = [b for b in banks + [b for v in row_banks.values() for b in v] if b not in pals]
        if missing or not banks:
            raise DecompError(f"{m.get('name')}: no palette in bank(s) {missing or banks}")
        ground = m.get("ground")
        if ground is not None:
            g = names.get(ground)
            if g is None or g["w"] != 1:
                raise DecompError(f"{m.get('name')}: ground {ground} must be a 1x1 metatile listed before it")
            gw = metas["primary" if g["ids"][0] < npm else "secondary"][g["ids"][0] - (0 if g["ids"][0] < npm else npm)]
            # what the ground shows: in each quarter its top layer where that covers it (the water of a pond,
            # the boards of a dock), else its bottom layer
            gw = [gw[4 + i] if _covers(gw[4 + i]) else gw[i] for i in range(4)]
        ids = []
        behaviors = m.get("behaviors", {})
        for by in range(h):
            for bx in range(w):
                cell = art[by * 16:(by + 1) * 16, bx * 16:(bx + 1) * 16]
                anim = anim_of.get(m["image"])
                if anim is not None:
                    quad = [anim_entry(anim, where, x0 + bx * 16 + qx * 8, y0 + by * 16 + qy * 8,
                                       row_banks.get(by, banks)) for qy in (0, 1) for qx in (0, 1)]
                else:
                    quad = [entry(where, cell[qy * 8:qy * 8 + 8, qx * 8:qx * 8 + 8], row_banks.get(by, banks))
                            for qy in (0, 1) for qx in (0, 1)]
                if ground is not None:
                    row = list(gw[:4]) + quad
                    layer = 0 if by < int(m.get("over_rows", 0)) else 1  # NORMAL: top over sprites; COVERED
                else:
                    row = quad + [0, 0, 0, 0]
                    layer = 0
                if m.get("layer") is not None:
                    layer = te._layer_value(m["layer"])
                beh = behaviors.get(f"{bx},{by}", m.get("behavior", 0))
                attr = te._pack_attr(0, te._behavior_value(beh, version), layer, version)
                metas[where].append(row)
                attrs[where].append(attr)
                base = 0 if where == "primary" else npm
                ids.append(base + len(metas[where]) - 1)
        if m.get("name") in names:
            raise DecompError(f"two metatiles are named {m['name']}")
        open_ = [int(a) + int(b) * w for a, b in (c.split(",") for c in m.get("open", []))]
        names[m.get("name", f"m{len(names)}")] = {"ids": ids, "w": w, "h": h, "solid": bool(m.get("solid", False)),
                                                  "elevation": m.get("elevation"), "open": open_}
    # animated tiles go after the others, in one run (the game copies each frame over the run)
    placed = {}
    for a in anims:
        if not a["tiles"]:
            raise DecompError(f"animation {a['name']}: no metatile is drawn from {a['frames'][0]}")
        st = tiles[a["where"]]
        n = len(a["tiles"])
        start = len(st.pixels)
        while any(st.base + t in st.reserved for t in range(start, start + n)):
            start += 1
        if start + n > st.limit:
            raise DecompError(f"out of tiles: animation {a['name']} needs {n} more, the tileset holds {st.limit}")
        st.pixels += [np.zeros((8, 8), np.uint8)] * (start - len(st.pixels)) + [p[0].copy() for p in a["tiles"]]
        a["first"] = st.base + start
        placed[a["name"]] = a
    for where in metas:
        for row in metas[where]:
            for i, v in enumerate(row):
                if isinstance(v, tuple):
                    a = placed[v[1]]
                    row[i] = (a["first"] + v[2]) | (v[3] << 12)
    limits = {"primary": npm, "secondary": 1024 - npm}
    for where, ts in sides.items():
        if len(metas[where]) > limits[where]:
            raise DecompError(f"{ts.symbol}: {len(metas[where])} metatiles, the limit is {limits[where]}")
        folder = Path(ts.tiles).parent
        rows = np.array(metas[where] or [[0] * 8], dtype="<u2")
        size = 4 if version == "frlg" else 2
        for rel, data in ((ts.metatiles, rows.tobytes()),
                          (ts.attributes or str(folder / "metatile_attributes.bin"),
                           b"".join(int(a).to_bytes(size, "little") for a in (attrs[where] or [0])))):
            maps.dp._backup(rel)
            (maps.root / rel).write_bytes(data)
        px = tiles[where].pixels
        n = -(-len(px) // 16) * 16
        sheet = np.zeros((n // 16 * 8, 128), np.uint8)
        for i, t in enumerate(px):
            sheet[(i // 16) * 8:(i // 16) * 8 + 8, (i % 16) * 8:(i % 16) * 8 + 8] = t
        im = Image.fromarray(sheet, "P")
        first = 0 if where == "primary" else npp
        shown = pals.get(first, [(i * 16,) * 3 for i in range(16)])
        im.putpalette([v for c in shown for v in c] + [0] * (768 - 48))
        maps.dp._backup(ts.tiles)
        im.save(maps.root / ts.tiles)
        for b, cols in pals.items():
            if (b < npp) == (where == "primary"):
                rel = ts.palettes[b] if b < len(ts.palettes) else str(folder / "palettes" / f"{b:02d}.pal")
                rel = rel if rel.endswith(".pal") else re.sub(r"\.gbapal$", ".pal", rel)
                maps.dp._backup(rel)
                write_jasc(maps.root / rel, cols)
    anim_out = [_write_animation(maps, a, sides, version, pals) for a in anims]
    for effect, fx in spec.get("field_effects", {}).items():
        _write_field_effect(maps, effect, fx, sides, pals, image)
    out = {"version": version, "tilesets": {k: v.symbol for k, v in sides.items()}, "metatiles": names,
           "tiles": {k: len(tiles[k].pixels) for k in sides}, "metatile_counts": {k: len(metas[k]) for k in sides}}
    if anim_out:
        out["animations"] = anim_out
    (sdir / "names.json").write_text(json.dumps(out, indent=1) + "\n")
    maps.dp._changed = None
    return out


def _write_field_effect(maps, effect: str, fx: dict, sides: dict, pals: dict, image) -> None:
    from . import fieldeffects
    if effect != "tall_grass":
        raise DecompError(f"unknown field effect {effect} (pae draws: tall_grass)")
    side = fx.get("tileset", "primary")
    if side not in sides:
        raise DecompError(f"field effect {effect}: the spec builds no {side} tileset")
    bank = int(fx.get("bank", 0))
    if bank not in pals:
        raise DecompError(f"field effect {effect}: the spec gives no palette for bank {bank}")
    rgba = image(fx["image"])
    if rgba.shape[:2] != (16, 16 * fieldeffects.FRAMES):
        raise DecompError(f"field effect {effect}: {fx['image']} must be {16 * fieldeffects.FRAMES}x16 "
                          f"({fieldeffects.FRAMES} frames of 16x16)")
    pal = np.array([_gba(c) for c in pals[bank][1:]], np.int32)
    d = ((rgba[..., :3].astype(np.int32)[..., None, :] - pal[None, None]) ** 2).sum(-1)
    idx = np.where(rgba[..., 3] >= 128, d.argmin(-1) + 1, 0).astype(np.uint8)
    frames = [idx[:, 16 * k:16 * k + 16] for k in range(fieldeffects.FRAMES)]
    fieldeffects.set_tall_grass(maps, sides[side].symbol, frames, pals[bank])


def _write_animation(maps, a: dict, sides: dict, version: str, pals: dict) -> dict:
    """Write an animation's frame PNGs; add its code to tileset_anims.c the first time, else keep the code in step
    (where the tiles are, how many, the frames and the speed)."""
    import tempfile

    from .gfx import Layout, tiles_to_image
    from .tileset_anims import TilesetAnims, _camel

    ts = sides[a["where"]]
    n, nframes = len(a["tiles"]), len(a["frames"])
    w = max(d for d in range(1, min(n, 16) + 1) if n % d == 0)
    bank = next((b for b in sorted(pals) if (b < maps.consts(version)[2]) == (a["where"] == "primary")), 0)
    plte = [v for c in pals.get(bank, [(i * 16,) * 3 for i in range(16)]) for v in c]

    def frame_image(k: int) -> Image.Image:
        im = Image.fromarray(tiles_to_image(np.array([t[k] for t in a["tiles"]]), Layout.sheet(n, w)), "P")
        im.putpalette(plte + [0] * (768 - len(plte)))
        return im
    ta = TilesetAnims(maps)
    base = f"{re.sub(r'^gTileset_', '', ts.symbol)}_{_camel(a['name'])}"
    data = ta.parse()
    existing = data["anims"].get(base)
    prim = sides.get("primary").symbol if "primary" in sides else None
    sec = sides.get("secondary").symbol if "secondary" in sides else None
    if existing is None:
        with tempfile.TemporaryDirectory() as tmp:
            files = []
            for k in range(nframes):
                f = Path(tmp) / f"{k}.png"
                frame_image(k).save(f)
                files.append(str(f))
            res = ta.create((prim, sec, version), a["name"], a["first"], n, period=a["speed"], frame_files=files)
        return {"name": base, "first_tile": a["first"], "tiles": n, "frames": nframes, "notes": res["notes"]}
    while len(existing.frames) < nframes:  # the spec has more frames now: add them, then draw them below
        ta.add_frame(base, copy_of=0)
        existing = ta.get(base)
    while len(existing.frames) > nframes:
        ta.remove_frame(base, len(existing.frames) - 1)
        existing = ta.get(base)
    if existing.period != a["speed"]:
        existing = ta.set_speed(base, a["speed"])
    for k, sym in enumerate(existing.frames):
        rel = existing.pngs.get(sym)
        maps.dp._backup(rel)
        frame_image(k).save(maps.root / rel)
    if existing.count != n or [s.dest for s in existing.slots] != [a["first"]]:
        ta.retarget(base, a["first"], n, version)
    return {"name": base, "first_tile": a["first"], "tiles": n, "frames": nframes, "notes": []}


# ---------------------------------------------------------------------- paint
def paint(maps, map_name: str, grid: str, legend: dict, names: dict, which: str = "blockdata") -> np.ndarray:
    """Blocks from a text grid. ``legend``: char -> metatile/stamp name, or {"name", "solid", "elevation"}.
    A stamp's char must fill whole w x h rectangles (placed left to right, top to bottom)."""
    rows = [r for r in grid.splitlines() if r.strip() and not r.startswith("#")]
    width = max(len(r) for r in rows)
    rows = [r.ljust(width) for r in rows]
    h = len(rows)
    out = np.zeros((h, width), np.int32)
    done = np.zeros((h, width), bool)
    meta = names["metatiles"]

    def group(c: str) -> str:  # a character joins another's group with {"joins": "~"} (for edge variants)
        it = legend.get(c)
        return it.get("joins", c) if isinstance(it, dict) else c
    for y in range(h):
        for x in range(width):
            if done[y, x]:
                continue
            ch = rows[y][x]
            item = legend.get(ch)
            if item is None:
                raise DecompError(f"({x},{y}): '{ch}' is not in the legend")
            item = {"name": item} if isinstance(item, str) else dict(item)
            pattern = "tile" in item
            if "auto" in item:  # edge variant from the neighbours with the same character (map edges count as same)
                # pieces that are stamps (e.g. 2x2 trees) take their variant from the neighbouring cells of their size
                first = meta.get(f"{item['auto']}#15") or meta.get(item["auto"]) or {"w": 1, "h": 1}
                cw, chh = first["w"], first["h"]
                x0, y0 = x - x % cw, y - y % chh

                def same(cx: int, cy: int) -> bool:
                    return all(not (0 <= yy < h and 0 <= xx < width) or group(rows[yy][xx]) == group(ch)
                               for yy in range(cy, cy + chh) for xx in range(cx, cx + cw))
                mask = 0
                for bit, (dx, dy) in ((1, (0, -1)), (2, (1, 0)), (4, (0, 1)), (8, (-1, 0))):
                    if same(x0 + dx * cw, y0 + dy * chh):
                        mask |= bit
                pattern = pattern or cw > 1 or chh > 1
                # inside corners: both sides the same but the diagonal not (1 NE, 2 SE, 4 SW, 8 NW); a variant
                # "path#15.1" is used when someone drew it
                corners = 0
                for bit, (dx, dy), sides in ((1, (1, -1), 1 | 2), (2, (1, 1), 2 | 4), (4, (-1, 1), 4 | 8),
                                             (8, (-1, -1), 8 | 1)):
                    if mask & sides == sides and not same(x0 + dx * cw, y0 + dy * chh):
                        corners |= bit
                # a variant nobody drew falls back to the plain one (all neighbours the same)
                # {"with": "lily"}: the piece "pond#15+lily" where someone drew it (else the plain one)
                with_ = f"{item['auto']}#{mask}+{item['with']}" if "with" in item else None
                name = next((n for n in (with_, f"{item['auto']}#{mask}.{corners}" if corners else None,
                                         f"{item['auto']}#{mask}", f"{item['auto']}#15", item["auto"])
                             if n and n in meta), item["auto"])
                # variants "name~1", "name~2"... are spread by position, like "pick"
                variants = [name] + [f"{name}~{k}" for k in range(1, 64) if f"{name}~{k}" in meta]
                item["name"] = variants[(x * 7919 + y * 104729 + (x * y) % 13) % len(variants)]
            elif "pick" in item:  # a variant chosen by position (the same every time)
                item["name"] = item["pick"][(x * 7919 + y * 104729 + (x * y) % 13) % len(item["pick"])]
            elif "tile" in item:  # a stamp repeated as a pattern: each block shows the piece at its position
                item["name"] = item["tile"]
            m = meta.get(item["name"])
            if m is None:
                raise DecompError(f"legend '{ch}': unknown metatile {item['name']}")
            solid = item.get("solid", m.get("solid", False))
            elev = item.get("elevation", m.get("elevation"))
            elev = 3 if elev is None else int(elev)
            if pattern:
                k = (y % m["h"]) * m["w"] + x % m["w"]
                blocked = bool(solid) and k not in m.get("open", [])
                out[y, x] = m["ids"][k] | (int(blocked) << 10) | (elev << 12)
                done[y, x] = True
                continue
            for j in range(m["h"]):
                for i in range(m["w"]):
                    yy, xx = y + j, x + i
                    if yy >= h or xx >= width or rows[yy][xx] != ch or done[yy, xx]:
                        raise DecompError(f"({x},{y}): stamp '{ch}' ({item['name']}) needs a whole "
                                          f"{m['w']}x{m['h']} area of '{ch}'")
                    k = j * m["w"] + i
                    blocked = bool(solid) and k not in m.get("open", [])
                    out[yy, xx] = m["ids"][k] | (int(blocked) << 10) | (elev << 12)
                    done[yy, xx] = True
    lay = maps.layout_of(map_name)
    if which == "blockdata" and (width, h) != (lay["width"], lay["height"]):
        maps.resize(map_name, width, h)
    if which == "border" and (width, h) != (lay.get("border_width", 2), lay.get("border_height", 2)):
        maps.set_geometry(map_name, border_width=width, border_height=h)
    maps.write_blocks(map_name, out, which)
    return out

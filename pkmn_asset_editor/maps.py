"""Maps, events and scripts of a pokeemerald-style source tree.

Everything here edits the same source files Porymap edits:

* ``data/maps/map_groups.json``   – which maps exist and their group
* ``data/maps/<Map>/map.json``    – header, connections and all events
* ``data/maps/<Map>/scripts.inc`` (or ``scripts.pory``) – the map's scripts and text
* ``data/layouts/layouts.json``   – layout size and tilesets
* ``data/layouts/<Layout>/map.bin`` / ``border.bin`` – the blocks (metatile, collision, elevation)

Generated files (``events.inc``, ``groups.inc``, ``map_groups.h``, ``layouts.h``) are
rebuilt by ``make`` from these, so nothing else has to be touched.
"""

from __future__ import annotations

import io
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from .decomp import DecompError, strip_comments
from .gfx import Layout, image_to_tiles, load_png, pad_palette, read_jasc

UNDEFINED = 0x3FF
EVENT_KINDS = ("object_events", "warp_events", "coord_events", "bg_events")
DEFAULT_EVENTS = {
    "object_events": {"graphics_id": "OBJ_EVENT_GFX_BOY_1", "x": 0, "y": 0, "elevation": 3,
                      "movement_type": "MOVEMENT_TYPE_FACE_DOWN", "movement_range_x": 0, "movement_range_y": 0,
                      "trainer_type": "TRAINER_TYPE_NONE", "trainer_sight_or_berry_tree_id": "0",
                      "script": "0x0", "flag": "0"},
    "warp_events": {"x": 0, "y": 0, "elevation": 0, "dest_map": "MAP_NONE", "dest_warp_id": "0"},
    "coord_events": {"type": "trigger", "x": 0, "y": 0, "elevation": 3, "var": "VAR_TEMP_1", "var_value": "0",
                     "script": "0x0"},
    "bg_events": {"type": "sign", "x": 0, "y": 0, "elevation": 0, "player_facing_dir": "BG_EVENT_PLAYER_FACING_ANY",
                  "script": "0x0"},
}
LABEL_RE = re.compile(r"^([A-Za-z_]\w*)::?", re.M)


@dataclass
class TilesetInfo:
    symbol: str
    secondary: bool
    tiles: str | None
    palettes: list[str]
    metatiles: str | None
    attributes: str | None


@dataclass
class Atlas:
    image: np.ndarray  # (rows*16, 128, 4) RGBA, metatile id m at row m//8, column m%8
    num_primary: int
    num_secondary: int
    behaviors: list[int] = field(default_factory=list)

    @property
    def count(self) -> int:
        return self.num_primary + self.num_secondary

    def tiles(self) -> np.ndarray:
        rows = self.image.shape[0] // 16
        return self.image.reshape(rows, 16, 8, 16, 4).transpose(0, 2, 1, 3, 4).reshape(rows * 8, 16, 16, 4)


def _upper_snake(name: str) -> str:
    # same convention as the existing ids: PetalburgCity_House1 -> PETALBURG_CITY_HOUSE1, SSTidal -> SS_TIDAL
    name = re.sub(r"_Frlg$", "", name)  # FRLG maps in expansion keep their original ids
    s = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").upper()


def _dump(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class MapEditor:
    def __init__(self, dp):
        self.dp = dp
        self.root: Path = dp.root
        self._atlas_cache: dict[tuple, tuple[tuple, Atlas]] = {}
        self._sprite_cache: dict[str, Image.Image | None] = {}
        self._constants: dict[str, list[str]] | None = None
        self._labels: dict[str, str] | None = None
        self.reload()

    @property
    def tileset_editor(self):
        if not hasattr(self, "_tse"):
            from .metatiles import TilesetEditor
            self._tse = TilesetEditor(self)
        return self._tse

    @property
    def tileset_anims(self):
        if not hasattr(self, "_tsa"):
            from .tileset_anims import TilesetAnims
            self._tsa = TilesetAnims(self)
        return self._tsa

    # ------------------------------------------------------------------ index
    def reload(self) -> None:
        root = self.root
        gpath = root / "data" / "maps" / "map_groups.json"
        if not gpath.exists():
            raise DecompError("data/maps/map_groups.json not found")
        self.groups_json = json.loads(gpath.read_text())
        self.group_order: list[str] = self.groups_json.get("group_order", [])
        self.layouts_json = json.loads((root / "data" / "layouts" / "layouts.json").read_text())
        self.layouts = {l["id"]: l for l in self.layouts_json["layouts"] if "id" in l}
        self.maps: dict[str, dict] = {}
        for g in self.group_order:
            for name in self.groups_json.get(g, []):
                p = root / "data" / "maps" / name / "map.json"
                if not p.exists():
                    continue
                try:
                    head = json.loads(p.read_text())
                except ValueError:
                    head = {}
                self.maps[name] = {"name": name, "group": g, "id": head.get("id", ""), "layout": head.get("layout", ""),
                                   "map_type": head.get("map_type", "")}
        self.map_by_id = {m["id"]: n for n, m in self.maps.items() if m["id"]}
        self._parse_tilesets()
        self._labels = None

    def _parse_tilesets(self) -> None:
        self.tilesets: dict[str, TilesetInfo] = {}
        headers = self.root / "src" / "data" / "tilesets" / "headers.h"
        if not headers.exists():
            return
        sf = self.dp.symbol_file
        pal_files: dict[str, list[tuple[int, str]]] = {}
        for r in self.dp.refs:
            m = re.match(r"(\w+)\[(\d+)\]$", r.symbol)
            if m:
                pal_files.setdefault(m.group(1), []).append((int(m.group(2)), r.file))
        for m in re.finditer(r"struct Tileset\s+(\w+)\s*=\s*\{(.*?)\};", strip_comments(headers.read_text()), re.S):
            body = m.group(2)
            field_ = lambda k: (re.search(rf"\.{k}\s*=\s*(\w+)", body) or [None, None])[1]
            pals = [f for _, f in sorted(pal_files.get(field_("palettes") or "", []))]
            self.tilesets[m.group(1)] = TilesetInfo(
                m.group(1), (field_("isSecondary") or "FALSE") == "TRUE", sf.get(field_("tiles") or ""), pals,
                sf.get(field_("metatiles") or ""), sf.get(field_("metatileAttributes") or ""))

    def consts(self, version: str) -> tuple[int, int, int]:
        d = self.dp.defines
        sfx = "_FRLG" if version == "frlg" else ""
        dm, dt, dpl = (640, 640, 7) if version == "frlg" else (512, 512, 6)
        g = lambda k, dv: int(d.get(k + sfx, dv)) if str(d.get(k + sfx, dv)).isdigit() else dv
        return g("NUM_METATILES_IN_PRIMARY", dm), g("NUM_TILES_IN_PRIMARY", dt), g("NUM_PALS_IN_PRIMARY", dpl)

    def get_map(self, name: str) -> dict:
        name = self.resolve(name)
        return json.loads((self.root / "data" / "maps" / name / "map.json").read_text())

    def resolve(self, name: str) -> str:
        if name in self.maps:
            return name
        if name in self.map_by_id:
            return self.map_by_id[name]
        low = name.lower().replace("_", "")
        for n in self.maps:
            if n.lower().replace("_", "") == low:
                return n
        raise DecompError(f"unknown map '{name}'")

    def layout_of(self, name: str) -> dict:
        lid = self.get_map(name).get("layout")
        if lid not in self.layouts:
            raise DecompError(f"map {name} uses unknown layout {lid}")
        return self.layouts[lid]

    def list_maps(self) -> list[dict]:
        return [dict(m) for m in self.maps.values()]

    # ------------------------------------------------------------------ blocks
    def read_blocks(self, layout: dict, which: str = "blockdata") -> np.ndarray:
        path = self.root / layout[f"{which}_filepath"]
        if which == "border":
            w, h = layout.get("border_width", 2), layout.get("border_height", 2)
        else:
            w, h = layout["width"], layout["height"]
        raw = path.read_bytes() if path.exists() else b""
        arr = np.frombuffer(raw[: w * h * 2].ljust(w * h * 2, b"\0"), dtype="<u2").astype(np.int32)
        return arr.reshape(h, w)

    def write_blocks(self, name: str, blocks: np.ndarray, which: str = "blockdata") -> None:
        layout = self.layout_of(name)
        if which == "blockdata" and blocks.shape != (layout["height"], layout["width"]):
            raise DecompError(f"expected {layout['width']}x{layout['height']} blocks, got "
                              f"{blocks.shape[1]}x{blocks.shape[0]} (resize the layout first)")
        path = self.root / layout[f"{which}_filepath"]
        self.dp._backup(str(path.relative_to(self.root)))
        path.write_bytes(np.ascontiguousarray(blocks, dtype="<u2").tobytes())
        self.dp._changed = None

    def resize(self, name: str, width: int, height: int, fill: int | None = None) -> list[str]:
        if not (1 <= width <= 1024 and 1 <= height <= 1024):
            raise DecompError("layouts must be between 1 and 1024 blocks wide/high")
        layout = self.layout_of(name)
        users = [n for n, m in self.maps.items() if m["layout"] == layout["id"]]
        old = self.read_blocks(layout)
        if fill is None:
            border = self.read_blocks(layout, "border")
            fill = int(border[0, 0]) if border.size else 0
        new = np.full((height, width), fill, dtype=np.int32)
        h, w = min(height, old.shape[0]), min(width, old.shape[1])
        new[:h, :w] = old[:h, :w]
        layout["width"], layout["height"] = width, height
        self._save_layouts()
        self.write_blocks(name, new)
        notes = []
        if len(users) > 1:
            notes.append(f"layout {layout['id']} is shared by {', '.join(users)}; all of them were resized")
        ev = self.get_map(name)
        outside = [e for k in EVENT_KINDS for e in ev.get(k, []) if e.get("x", 0) >= width or e.get("y", 0) >= height]
        if outside:
            notes.append(f"{len(outside)} event(s) are now outside the map")
        return notes

    def _save_layouts(self) -> None:
        path = self.root / "data" / "layouts" / "layouts.json"
        self.dp._backup(str(path.relative_to(self.root)))
        _dump(path, self.layouts_json)
        self.dp._changed = None

    # ------------------------------------------------------------------ rendering
    def _tiles_of(self, ts: TilesetInfo | None) -> np.ndarray:
        if ts is None or not ts.tiles or not (self.root / ts.tiles).exists():
            return np.zeros((0, 8, 8), np.uint8)
        img = load_png(self.root / ts.tiles)
        idx = img.indices if img.indices is not None else np.zeros((img.height, img.width), np.uint8)
        lay = Layout.sheet((img.width // 8) * (img.height // 8), img.width // 8)
        return image_to_tiles(idx & 0x0F, lay)

    def _metatiles_of(self, ts: TilesetInfo | None) -> np.ndarray:
        if ts is None or not ts.metatiles or not (self.root / ts.metatiles).exists():
            return np.zeros((0, 8), np.int32)
        raw = (self.root / ts.metatiles).read_bytes()
        arr = np.frombuffer(raw[: len(raw) // 16 * 16], dtype="<u2").astype(np.int32)
        return arr.reshape(-1, 8)

    def _attrs_of(self, ts: TilesetInfo | None, version: str) -> list[int]:
        if ts is None or not ts.attributes or not (self.root / ts.attributes).exists():
            return []
        raw = (self.root / ts.attributes).read_bytes()
        if version == "frlg":
            return [int(v) & 0x1FF for v in np.frombuffer(raw[: len(raw) // 4 * 4], dtype="<u4")]
        return [int(v) & 0xFF for v in np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<u2")]

    def _palettes(self, prim: TilesetInfo | None, sec: TilesetInfo | None, npp: int) -> np.ndarray:
        out = np.zeros((16, 16, 3), np.uint8)
        for b in range(16):
            src = prim if b < npp else sec
            if src is None or b >= len(src.palettes):
                continue
            try:
                out[b] = np.array(pad_palette(read_jasc(self.root / src.palettes[b]), 16)[:16], np.uint8)
            except (OSError, ValueError):
                pass
        return out

    def atlas(self, primary: str, secondary: str, version: str = "emerald") -> Atlas:
        prim, sec = self.tilesets.get(primary), self.tilesets.get(secondary)
        files = [f for t in (prim, sec) if t for f in [t.tiles, t.metatiles, t.attributes, *t.palettes] if f]
        stamp = tuple((f, (self.root / f).stat().st_mtime) for f in files if (self.root / f).exists())
        key = (primary, secondary, version)
        hit = self._atlas_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        npm, npt, npp = self.consts(version)
        ptiles = self._tiles_of(prim)
        tiles = np.zeros((1024, 8, 8), np.uint8)
        tiles[: min(npt, len(ptiles))] = ptiles[:npt]
        stiles = self._tiles_of(sec)
        n2 = min(len(stiles), 1024 - npt)
        tiles[npt:npt + n2] = stiles[:n2]
        pm, sm = self._metatiles_of(prim)[:npm], self._metatiles_of(sec)[: 1024 - npm]
        metas = np.zeros((npm + len(sm), 8), np.int32)
        metas[: len(pm)] = pm
        metas[npm:] = sm
        pal = self._palettes(prim, sec, npp)
        n = len(metas)
        rows = -(-n // 8)
        img = np.zeros((rows * 16, 128, 4), np.uint8)
        backdrop = pal[0, 0]
        for m in range(n):
            if npm > len(pm) and len(pm) <= m < npm:
                continue  # unused primary slots stay transparent
            y0, x0 = (m // 8) * 16, (m % 8) * 16
            cell = np.zeros((16, 16, 4), np.uint8)
            cell[..., :3] = backdrop
            cell[..., 3] = 255
            for layer in (0, 1):
                for q in range(4):
                    v = int(metas[m, layer * 4 + q])
                    t, hf, vf, bank = v & 0x3FF, (v >> 10) & 1, (v >> 11) & 1, (v >> 12) & 0xF
                    px = tiles[t]
                    if hf:
                        px = px[:, ::-1]
                    if vf:
                        px = px[::-1, :]
                    ys, xs = (q // 2) * 8, (q % 2) * 8
                    mask = px != 0
                    cell[ys:ys + 8, xs:xs + 8, :3][mask] = pal[bank][px[mask]]
            img[y0:y0 + 16, x0:x0 + 16] = cell
        behaviors = self._attrs_of(prim, version)[:npm]
        behaviors += [0] * (npm - len(behaviors)) + self._attrs_of(sec, version)[: len(sm)]
        atlas = Atlas(img, npm, len(sm), behaviors)
        self._atlas_cache[key] = (stamp, atlas)
        return atlas

    def layout_atlas(self, layout: dict) -> Atlas:
        return self.atlas(layout["primary_tileset"], layout["secondary_tileset"], layout.get("layout_version", "emerald"))

    def render(self, name: str, events: bool = True, grid: bool = False, scale: int = 1) -> Image.Image:
        layout = self.layout_of(name)
        return self._compose(layout, self.read_blocks(layout), self.get_map(name) if events else None, grid, scale)

    def render_head(self, name: str, events: bool = True, scale: int = 1) -> Image.Image | None:
        """The map as it is in the last commit (None for a map that is new)."""
        raw = self.dp.original_bytes(f"data/maps/{name}/map.json")
        lays = self.dp.original_bytes("data/layouts/layouts.json")
        if raw is None or lays is None:
            return None
        data = json.loads(raw)
        layout = next((l for l in json.loads(lays)["layouts"] if l.get("id") == data.get("layout")), None)
        if layout is None:
            return None
        blk = self.dp.original_bytes(layout["blockdata_filepath"]) or b""
        w, h = layout["width"], layout["height"]
        blocks = np.frombuffer(blk[: w * h * 2].ljust(w * h * 2, b"\0"), dtype="<u2").astype(np.int32).reshape(h, w)
        return self._compose(layout, blocks, data if events else None, False, scale)

    def _compose(self, layout: dict, blocks: np.ndarray, data: dict | None, grid: bool, scale: int) -> Image.Image:
        atlas = self.layout_atlas(layout)
        tiles = atlas.tiles()
        ids = blocks & 0x3FF
        ok = ids < len(tiles)
        safe = np.where(ok, ids, 0)
        h, w = blocks.shape
        img = tiles[safe].transpose(0, 2, 1, 3, 4).reshape(h * 16, w * 16, 4).copy()
        if not ok.all():
            bad = np.repeat(np.repeat(~ok, 16, 0), 16, 1)
            img[bad] = (255, 0, 255, 255)
        im = Image.fromarray(img, "RGBA")
        if data is not None:
            self._draw_events(im, data)
        if grid:
            from PIL import ImageDraw
            d = ImageDraw.Draw(im)
            for x in range(0, w * 16, 16):
                d.line([(x, 0), (x, h * 16)], fill=(0, 0, 0, 60))
            for y in range(0, h * 16, 16):
                d.line([(0, y), (w * 16, y)], fill=(0, 0, 0, 60))
        if scale > 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        return im

    def _draw_events(self, im: Image.Image, data: dict) -> None:
        from PIL import ImageDraw
        d = ImageDraw.Draw(im)
        colors = {"warp_events": (230, 40, 40), "coord_events": (40, 160, 230), "bg_events": (200, 60, 200)}
        for kind in ("warp_events", "coord_events", "bg_events"):
            for i, e in enumerate(data.get(kind, [])):
                x, y = int(e.get("x", 0)) * 16, int(e.get("y", 0)) * 16
                d.rectangle([x + 1, y + 1, x + 14, y + 14], outline=colors[kind] + (255,), width=2)
                d.text((x + 4, y + 2), {"warp_events": "W", "coord_events": "T", "bg_events": "S"}[kind] + str(i),
                       fill=colors[kind] + (255,))
        for i, e in enumerate(data.get("object_events", [])):
            x, y = int(e.get("x", 0)) * 16, int(e.get("y", 0)) * 16
            spr = self.object_sprite(e.get("graphics_id", ""))
            if spr is not None:
                im.alpha_composite(spr, (x + (16 - spr.width) // 2, max(0, y + 16 - spr.height)))
            else:
                d.ellipse([x + 2, y + 2, x + 13, y + 13], outline=(250, 200, 0, 255), width=2)
            d.text((x + 1, y + 1), str(i), fill=(255, 255, 0, 255))

    # ------------------------------------------------------------------ object sprites
    def _ow_tables(self):
        if hasattr(self, "_ow"):
            return self._ow
        root = self.root
        texts = []
        for f in list((root / "src").glob("*.c")) + list((root / "src" / "data").rglob("*.h")):
            try:
                t = f.read_text(errors="replace")
            except OSError:
                continue
            if "OBJ_EVENT_GFX_" in t or "SpriteFrameImage" in t or "OBJ_EVENT_PAL_TAG" in t:
                texts.append(strip_comments(t))
        ptr, infos, tables, tag_pal = {}, {}, {}, {}
        for t in texts:
            for m in re.finditer(r"\[(OBJ_EVENT_GFX_\w+)\]\s*=\s*&(\w+)", t):
                ptr.setdefault(m.group(1), m.group(2))
            for m in re.finditer(r"struct ObjectEventGraphicsInfo\s+(\w+)\s*=\s*\{(.*?)\};", t, re.S):
                b = m.group(2)
                g = lambda k: (re.search(rf"\.{k}\s*=\s*(\w+)", b) or [None, None])[1]
                infos[m.group(1)] = {"images": g("images"), "paletteTag": g("paletteTag"),
                                     "width": int(g("width") or 16), "height": int(g("height") or 32)}
            for m in re.finditer(r"struct SpriteFrameImage\s+(\w+)\[\]\s*=\s*\{(.*?)\};", t, re.S):
                first = re.search(r"\(\s*(\w+)\s*,\s*\d+\s*,\s*\d+\s*(?:,\s*(\d+))?", m.group(2)) or \
                    re.search(r"\b(g\w+)\b()", m.group(2))
                if first:
                    tables[m.group(1)] = (first.group(1), int(first.group(2) or 0))
            for m in re.finditer(r"\{\s*(g\w+)\s*,\s*(OBJ_EVENT_PAL_TAG_\w+)\s*\}", t):
                tag_pal.setdefault(m.group(2), m.group(1))
        self._ow = (ptr, infos, tables, tag_pal)
        return self._ow

    def object_sprite(self, gfx: str) -> Image.Image | None:
        if gfx in self._sprite_cache:
            return self._sprite_cache[gfx]
        out = None
        try:
            ptr, infos, tables, tag_pal = self._ow_tables()
            info = infos.get(ptr.get(gfx, ""))
            if info and info["images"] in tables:
                pic_sym, frame = tables[info["images"]]
                files = [r.file for r in self.dp.refs if r.symbol == pic_sym and r.file.endswith(".png")]
                if files:
                    img = load_png(self.root / files[0])
                    w, h = info["width"], info["height"]
                    per_row = max(1, img.width // w)
                    fx, fy = (frame % per_row) * w, (frame // per_row) * h
                    idx = img.indices[fy:fy + h, fx:fx + w] if img.indices is not None else None
                    pal_file = self.dp.symbol_file.get(tag_pal.get(info["paletteTag"] or "", ""))
                    pal = read_jasc(self.root / pal_file) if pal_file and pal_file.endswith(".pal") else img.palette
                    if idx is not None and pal:
                        rgb = np.array(pad_palette(pal, 256), np.uint8)[idx]
                        alpha = np.where(idx == 0, 0, 255).astype(np.uint8)
                        out = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
        except (OSError, ValueError, KeyError):
            out = None
        self._sprite_cache[gfx] = out
        return out

    # ------------------------------------------------------------------ events & header
    def save_map(self, name: str, data: dict) -> list[str]:
        name = self.resolve(name)
        problems = self.validate_map_data(name, data)
        errors = [p for lvl, p in problems if lvl == "error"]
        if errors:
            raise DecompError("; ".join(errors[:5]))
        path = self.root / "data" / "maps" / name / "map.json"
        self.dp._backup(str(path.relative_to(self.root)))
        _dump(path, data)
        self.dp._changed = None
        if data.get("layout") != self.maps[name]["layout"]:
            self.maps[name]["layout"] = data.get("layout")
        return [p for lvl, p in problems if lvl == "warning"]

    def add_event(self, name: str, kind: str, x: int, y: int, **fields) -> int:
        if kind not in EVENT_KINDS:
            raise DecompError(f"event kind must be one of {', '.join(EVENT_KINDS)}")
        data = self.get_map(name)
        ev = dict(DEFAULT_EVENTS[kind])
        ev.update({"x": int(x), "y": int(y)})
        ev.update(fields)
        data.setdefault(kind, []).append(ev)
        self.save_map(name, data)
        return len(data[kind]) - 1

    def link_warps(self, map_a: str, warp_a: int, map_b: str, warp_b: int) -> None:
        """Make warp ``warp_a`` of map A lead to warp ``warp_b`` of map B and back."""
        a, b = self.resolve(map_a), self.resolve(map_b)
        da, db = self.get_map(a), (self.get_map(b) if b != a else None)
        db = db if db is not None else da
        if warp_a >= len(da.get("warp_events", [])) or warp_b >= len(db.get("warp_events", [])):
            raise DecompError("warp index out of range")
        da["warp_events"][warp_a]["dest_map"] = db["id"]
        da["warp_events"][warp_a]["dest_warp_id"] = str(warp_b)
        db["warp_events"][warp_b]["dest_map"] = da["id"]
        db["warp_events"][warp_b]["dest_warp_id"] = str(warp_a)
        self.save_map(a, da)
        if b != a:
            self.save_map(b, db)

    # ------------------------------------------------------------------ scripts
    def script_path(self, name: str) -> Path:
        d = self.root / "data" / "maps" / self.resolve(name)
        pory = d / "scripts.pory"
        return pory if pory.exists() else d / "scripts.inc"

    def read_script(self, name: str) -> str:
        p = self.script_path(name)
        return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""

    def write_script(self, name: str, text: str) -> None:
        p = self.script_path(name)
        self.dp._backup(str(p.relative_to(self.root)))
        p.write_text(text, encoding="utf-8")
        self.dp._changed = None
        self._labels = None

    def labels(self) -> dict[str, str]:
        """Every script label in the project -> file defining it."""
        if self._labels is None:
            out: dict[str, str] = {}
            for base in ("data/maps", "data/scripts", "data/text"):
                for p in (self.root / base).rglob("*"):
                    if p.suffix in (".inc", ".s", ".pory") and p.is_file():
                        try:
                            text = p.read_text(errors="replace")
                        except OSError:
                            continue
                        rel = str(p.relative_to(self.root))
                        pat = LABEL_RE if p.suffix != ".pory" else re.compile(r"^\s*(?:script|text|movement|mart)\s+(?:\w+\s+)?(\w+)", re.M)
                        for m in pat.finditer(text):
                            out.setdefault(m.group(1), rel)
            for p in (self.root / "data").glob("*.s"):
                for m in LABEL_RE.finditer(p.read_text(errors="replace")):
                    out.setdefault(m.group(1), str(p.relative_to(self.root)))
            self._labels = out
        return self._labels

    def new_script(self, name: str, kind: str = "npc", text: str = "Hello!", label: str | None = None) -> str:
        """Append a ready-to-edit script (and its text) to the map's scripts and return its label."""
        name = self.resolve(name)
        p = self.script_path(name)
        if p.suffix == ".pory":
            raise DecompError("this map uses Poryscript; add the script in scripts.pory")
        own = str(p.relative_to(self.root))
        existing = {k for k, f in self.labels().items() if f != own}  # other files: cached index
        existing |= set(LABEL_RE.findall(self.read_script(name)))      # this map: always re-read
        base = label or f"{name}_EventScript_{ {'npc': 'Npc', 'sign': 'Sign', 'trigger': 'Trigger'}.get(kind, 'Event')}"
        lbl, n = base, 1
        while lbl in existing:
            n += 1
            lbl = f"{base}{n}"
        text_lbl = lbl.replace("_EventScript_", "_Text_")
        safe = text.replace('"', "'").replace("\n", "\\n")
        if kind == "npc":
            body = f"\tmsgbox {text_lbl}, MSGBOX_NPC\n\tend\n"
        elif kind == "sign":
            body = f"\tmsgbox {text_lbl}, MSGBOX_SIGN\n\tend\n"
        else:
            body = f"\tlockall\n\tmsgbox {text_lbl}, MSGBOX_DEFAULT\n\treleaseall\n\tend\n"
        block = f"\n{lbl}::\n{body}\n{text_lbl}:\n\t.string \"{safe}$\"\n"
        cur = self.read_script(name)
        self.write_script(name, cur.rstrip("\n") + "\n" + block)
        return lbl

    # ------------------------------------------------------------------ new maps
    def duplicate_map(self, source: str, new_name: str, group: str | None = None, copy_layout: bool = True,
                      map_id: str | None = None) -> list[str]:
        """Create a new map (e.g. a house interior) as a copy of an existing one."""
        src = self.resolve(source)
        if not re.fullmatch(r"[A-Z][A-Za-z0-9_]*", new_name):
            raise DecompError("map names must start with a capital letter and use only letters, digits and _")
        if new_name in self.maps or (self.root / "data" / "maps" / new_name).exists():
            raise DecompError(f"map {new_name} already exists")
        group = group or self.maps[src]["group"]
        if group not in self.groups_json:
            raise DecompError(f"unknown map group {group}")
        notes = []
        sdir, ddir = self.root / "data" / "maps" / src, self.root / "data" / "maps" / new_name
        shutil.copytree(sdir, ddir)
        data = json.loads((ddir / "map.json").read_text())
        new_id = map_id or "MAP_" + _upper_snake(new_name)
        if new_id in self.map_by_id:
            shutil.rmtree(ddir)
            raise DecompError(f"map id {new_id} is already used by {self.map_by_id[new_id]}")
        old_id = data.get("id")
        data["id"], data["name"] = new_id, new_name
        script = ""
        for f in ("scripts.inc", "scripts.pory"):
            if (ddir / f).exists():
                script = (ddir / f).read_text(errors="replace")
        # rename the copied scripts and local ids so nothing is defined twice
        renames = {}
        for m in LABEL_RE.finditer(script):
            lbl = m.group(1)
            if lbl.startswith(src + "_"):
                renames[lbl] = new_name + lbl[len(src):]
        for ev in data.get("object_events", []):
            lid = ev.get("local_id")
            if lid and lid.startswith("LOCALID_"):
                renames[lid] = f"LOCALID_{_upper_snake(new_name)}_{lid[len('LOCALID_'):]}"
        if old_id:
            renames[old_id] = new_id

        def apply(s: str) -> str:
            if not renames:
                return s
            pat = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(renames, key=len, reverse=True)) + r")\b")
            return pat.sub(lambda m: renames[m.group(1)], s)

        data = json.loads(apply(json.dumps(data)))
        for f in ("scripts.inc", "scripts.pory"):
            if (ddir / f).exists():
                (ddir / f).write_text(apply((ddir / f).read_text(errors="replace")))
        if copy_layout:
            lay = dict(self.layouts[data["layout"]])
            lname = new_name
            lay["id"] = "LAYOUT_" + _upper_snake(new_name)
            lay["name"] = f"{lname}_Layout"
            ldir = self.root / "data" / "layouts" / lname
            if ldir.exists() or lay["id"] in self.layouts:
                raise DecompError(f"layout {lay['id']} already exists")
            ldir.mkdir(parents=True)
            for key in ("blockdata_filepath", "border_filepath"):
                srcf = self.root / lay[key]
                dst = ldir / Path(lay[key]).name
                if srcf.exists():
                    shutil.copyfile(srcf, dst)
                lay[key] = str(dst.relative_to(self.root))
            self.layouts_json["layouts"].append(lay)
            self.layouts[lay["id"]] = lay
            data["layout"] = lay["id"]
            self._save_layouts()
        _dump(ddir / "map.json", data)
        self.groups_json[group].append(new_name)
        gpath = self.root / "data" / "maps" / "map_groups.json"
        self.dp._backup(str(gpath.relative_to(self.root)))
        _dump(gpath, self.groups_json)
        self.dp._changed = None
        self.reload()
        flags = sorted({e.get("flag") for e in data.get("object_events", []) if e.get("flag") not in (None, "0")})
        if flags:
            notes.append(f"the copied NPCs still use the original's flags ({', '.join(flags[:4])}…); "
                         f"change them if this map should behave independently")
        warps = data.get("warp_events", [])
        if warps:
            notes.append(f"its {len(warps)} warp(s) still lead where {src}'s did; use link_warps to connect the "
                         f"new map to its entrance")
        return notes

    # ------------------------------------------------------------------ constants & validation
    def constants(self, prefix: str) -> list[str]:
        if prefix == "MAP_":
            return sorted(["MAP_NONE"] + list(self.map_by_id))
        if prefix == "LAYOUT_":
            return sorted(self.layouts)
        if self._constants is None:
            names: set[str] = set()
            for f in (self.root / "include" / "constants").glob("*.h"):
                try:
                    names.update(re.findall(r"^\s*#define\s+([A-Z][A-Z0-9_]+)", f.read_text(errors="replace"), re.M))
                except OSError:
                    pass
            for f in (self.root / "include" / "constants").glob("*.h"):
                try:
                    text = strip_comments(f.read_text(errors="replace"))
                except OSError:
                    continue
                for body in re.findall(r"enum\s*\w*\s*\{(.*?)\}", text, re.S):
                    names.update(re.findall(r"\b([A-Z][A-Z0-9_]{3,})\b\s*(?:=|,|$)", body))
            ptr = self._ow_tables()[0]
            names.update(ptr)
            buckets: dict[str, list[str]] = {}
            for n in names:
                p = n.split("_")[0] + "_"
                buckets.setdefault(p, []).append(n)
            self._constants = {k: sorted(v) for k, v in buckets.items()}
        pre = prefix.split("_")[0] + "_"
        return [n for n in self._constants.get(pre, []) if n.startswith(prefix)]

    def validate_map_data(self, name: str, data: dict) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        layout = self.layouts.get(data.get("layout"))
        if layout is None:
            out.append(("error", f"unknown layout {data.get('layout')}"))
            return out
        w, h = layout["width"], layout["height"]
        labels = self.labels()
        gfx = set(self._ow_tables()[0])
        for kind in EVENT_KINDS:
            evs = data.get(kind, [])
            if not isinstance(evs, list):
                out.append(("error", f"{kind} must be a list"))
                continue
            for i, e in enumerate(evs):
                where = f"{kind}[{i}]"
                try:
                    x, y = int(e.get("x")), int(e.get("y"))
                except (TypeError, ValueError):
                    out.append(("error", f"{where}: x/y must be numbers"))
                    continue
                if not (0 <= x < w and 0 <= y < h):
                    out.append(("warning", f"{where} at ({x},{y}) is outside the {w}x{h} map"))
                script = e.get("script")
                if script and script not in ("0x0", "0", "NULL") and script not in labels:
                    out.append(("warning", f"{where}: script {script} is not defined in any .inc/.pory file yet"))
                if kind == "object_events":
                    g = e.get("graphics_id", "")
                    if gfx and g not in gfx and not g.startswith(("OBJ_EVENT_GFX_VAR_", "OBJ_EVENT_GFX_SPECIES")) \
                            and not g.isdigit():
                        out.append(("warning", f"{where}: unknown graphics_id {g}"))
                if kind == "warp_events":
                    dest = e.get("dest_map")
                    if dest not in self.map_by_id and dest not in ("MAP_NONE", "MAP_DYNAMIC"):
                        out.append(("error", f"{where}: unknown destination map {dest}"))
                    elif dest in self.map_by_id:
                        try:
                            n_dest = len(self.get_map(self.map_by_id[dest]).get("warp_events", []))
                            wid = int(str(e.get("dest_warp_id", "0")), 0)
                            if wid >= n_dest and dest != data.get("id"):
                                out.append(("warning", f"{where}: {dest} has no warp {wid} (it has {n_dest})"))
                        except (ValueError, DecompError):
                            pass
        lids = [e.get("local_id") for e in data.get("object_events", []) if e.get("local_id")]
        dup = {x for x in lids if lids.count(x) > 1}
        if dup:
            out.append(("error", f"duplicate local_id {', '.join(sorted(dup))}"))
        for c in data.get("connections") or []:
            if c.get("map") not in self.map_by_id:
                out.append(("error", f"connection to unknown map {c.get('map')}"))
        return out

    def maps_touching(self, files) -> list[str]:
        """Names of maps whose map.json, scripts or blocks are among ``files``."""
        out = set()
        for f in files:
            parts = Path(f).parts
            if len(parts) >= 4 and parts[:2] == ("data", "maps"):
                out.add(parts[2])
            if len(parts) >= 3 and parts[:2] == ("data", "layouts"):
                for n, m in self.maps.items():
                    lay = self.layouts.get(m["layout"])
                    if lay and f in (lay.get("blockdata_filepath"), lay.get("border_filepath")):
                        out.add(n)
        return sorted(n for n in out if n in self.maps)

    def check_changed(self, changed: dict[str, str]) -> list[tuple[str, str, str]]:
        """Validation for edited map files: (level, file, message)."""
        issues = []
        names = set()
        for f in changed:
            parts = Path(f).parts
            if len(parts) >= 3 and parts[0] == "data" and parts[1] == "maps" and parts[-1] in ("map.json", "scripts.inc"):
                names.add(parts[2])
            if len(parts) >= 3 and parts[0] == "data" and parts[1] == "layouts":
                for n, m in self.maps.items():
                    lay = self.layouts.get(m["layout"])
                    if lay and f in (lay.get("blockdata_filepath"), lay.get("border_filepath")):
                        names.add(n)
            if f in ("data/maps/map_groups.json", "data/layouts/layouts.json"):
                try:
                    json.loads((self.root / f).read_text())
                except ValueError as e:
                    issues.append(("error", f, f"invalid JSON: {e}"))
        for n in sorted(names):
            mj = f"data/maps/{n}/map.json"
            try:
                data = self.get_map(n)
            except ValueError as e:
                issues.append(("error", mj, f"invalid JSON: {e}"))
                continue
            except DecompError:
                issues.append(("error", mj, f"map {n} is not listed in map_groups.json"))
                continue
            for lvl, msg in self.validate_map_data(n, data):
                issues.append((lvl, mj, msg))
            lay = self.layouts.get(data.get("layout"))
            if lay:
                p = self.root / lay["blockdata_filepath"]
                size = p.stat().st_size if p.exists() else 0
                if size != lay["width"] * lay["height"] * 2:
                    issues.append(("error", lay["blockdata_filepath"],
                                   f"has {size // 2} blocks but the layout is {lay['width']}x{lay['height']}"))
                else:
                    atlas = self.layout_atlas(lay)
                    ids = self.read_blocks(lay) & 0x3FF
                    bad = int(((ids >= atlas.count) & (ids != UNDEFINED)).sum())
                    if bad:
                        issues.append(("warning", lay["blockdata_filepath"],
                                       f"{bad} block(s) use metatile ids that don't exist in this layout's tilesets"))
        return issues

    # ------------------------------------------------------------------ helpers for AI tools
    def blocks_text(self, name: str, x: int = 0, y: int = 0, w: int | None = None, h: int | None = None) -> dict:
        lay = self.layout_of(name)
        b = self.read_blocks(lay)
        w = w or lay["width"] - x
        h = h or lay["height"] - y
        reg = b[y:y + h, x:x + w]
        return {"map": self.resolve(name), "x": x, "y": y, "w": reg.shape[1], "h": reg.shape[0],
                "format": "per block: metatile id (hex, 3 digits) + collision (0-3) + elevation (hex), e.g. '00110'",
                "rows": [" ".join(f"{v & 0x3FF:03x}{(v >> 10) & 3}{(v >> 12) & 0xF:x}" for v in row) for row in reg]}

    def apply_blocks(self, name: str, x: int, y: int, rows: list[str]) -> int:
        lay = self.layout_of(name)
        b = self.read_blocks(lay).copy()
        n = 0
        for dy, row in enumerate(rows):
            for dx, cell in enumerate(row.split()):
                if cell == ".":
                    continue
                if not re.fullmatch(r"[0-9a-fA-F]{3}([0-3][0-9a-fA-F])?", cell):
                    raise DecompError(f"bad block '{cell}': use mmm (metatile) or mmmCE (metatile, collision, elevation)")
                px, py = x + dx, y + dy
                if not (0 <= px < lay["width"] and 0 <= py < lay["height"]):
                    continue
                m = int(cell[:3], 16)
                if len(cell) == 5:
                    v = m | (int(cell[3]) << 10) | (int(cell[4], 16) << 12)
                else:
                    v = (int(b[py, px]) & ~0x3FF) | m
                b[py, px] = v
                n += 1
        self.write_blocks(name, b)
        return n

    def copy_blocks(self, src: str, x: int, y: int, w: int, h: int, dest: str, dx: int, dy: int) -> list[str]:
        """Copy a rectangle of blocks (e.g. a house) from one map to another."""
        sl, dl = self.layout_of(src), self.layout_of(dest)
        notes = []
        if (sl["primary_tileset"], sl["secondary_tileset"]) != (dl["primary_tileset"], dl["secondary_tileset"]):
            if sl["primary_tileset"] != dl["primary_tileset"]:
                raise DecompError("the two maps use different primary tilesets, so their metatile ids don't match")
            notes.append(f"{dest} uses {dl['secondary_tileset']} instead of {sl['secondary_tileset']}: blocks from "
                         f"the secondary tileset will look different")
        sb = self.read_blocks(sl)[y:y + h, x:x + w]
        db = self.read_blocks(dl).copy()
        hh = min(sb.shape[0], db.shape[0] - dy)
        ww = min(sb.shape[1], db.shape[1] - dx)
        if hh <= 0 or ww <= 0 or dx < 0 or dy < 0:
            raise DecompError("destination is outside the map")
        db[dy:dy + hh, dx:dx + ww] = sb[:hh, :ww]
        self.write_blocks(dest, db)
        return notes

    def metatile_sheet(self, name: str, scale: int = 1) -> Image.Image:
        """Atlas of the map's metatiles with id labels, for choosing what to paint."""
        from PIL import ImageDraw
        atlas = self.layout_atlas(self.layout_of(name))
        im = Image.fromarray(atlas.image, "RGBA")
        if scale > 1:
            im = im.resize((im.width * scale, im.height * scale), Image.NEAREST)
        out = Image.new("RGBA", (im.width + 40, im.height), (30, 30, 36, 255))
        out.alpha_composite(im, (40, 0))
        d = ImageDraw.Draw(out)
        for r in range(atlas.image.shape[0] // 16):
            d.text((2, r * 16 * scale + 2), f"{r * 8:03x}", fill=(220, 220, 220, 255))
        return out

    @staticmethod
    def png_bytes(im: Image.Image) -> bytes:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()

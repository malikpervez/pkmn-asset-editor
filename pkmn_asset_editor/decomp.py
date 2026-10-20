"""Source-tree mode: work directly on a pokeemerald / pokeemerald-expansion / pokefirered repository.

In a decomp every graphic already is a file (``graphics/**.png``, ``*.pal``,
``data/tilesets/**``) and ``make`` turns them into the ROM, so there is nothing
to extract or re-insert. What this module adds on top of the files:

* an index of every graphic, built from the ``INCGFX_*`` / ``INCBIN_*``
  references in ``src/`` (so it knows symbol names, output formats and gbagfx
  arguments) and grouped into meaningful categories;
* the palette each image is *really* drawn with in-game (``normal.pal`` for a
  Pokémon sprite, ``iconPalIndex`` for icons, ``gFooPal_X`` paired with
  ``gFooGfx_X``, …), so previews and edits use the right colours;
* coloured, editable views of map tilesets and tilemap backgrounds;
* validation that catches what would break ``make`` or look wrong in game;
* conversion of arbitrary artwork into valid indexed PNGs;
* git-based status / revert, and ``make`` to build.
"""

from __future__ import annotations

import bisect
import io
import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import formats
from .catalog import Asset
from .gfx import (GRAYSCALE16, RGB, Layout, LoadedImage, decode_tiles, encode_tiles, image_to_tiles, load_png,
                  pad_palette, quantize_new_palette, read_jasc, remap_to_palette, same_color, tiles_to_image,
                  write_jasc)

INC_RE = re.compile(r"\bINC(GFX|BIN)_(?:U8|U16|U32|COMP|32)\s*\(")
DECL_RE = re.compile(r"(\w+)\s*(?:\[[^\]]*\])+\s*=")
STR_RE = re.compile(r'"([^"]*)"')
IDENT_RE = re.compile(r"\b[gs][A-Z]\w*\b")
DEFINE_RE = re.compile(r"^\s*#define\s+(\w+)\s+([^\s/]+)", re.M)

SPRITE_CATEGORIES = {"pokemon", "trainers", "items", "battle_anims", "overworld", "berries", "balls",
                     "decorations", "field_effects", "types"}
PAL_WORDS = ("Palette", "Pal")
GFX_WORDS = ("Gfx", "Pic", "Tiles", "Image", "Icon", "Sprite", "Sheet")
MAP_WORDS = ("Tilemap", "Map")


class DecompError(RuntimeError):
    pass


def is_decomp(root: Path) -> bool:
    root = Path(root)
    return (root / "Makefile").exists() and (root / "src").is_dir() and (root / "graphics").is_dir()


@dataclass
class Ref:
    file: str  # source file the build reads (png / pal / bin), relative to repo
    fmt: str  # output suffix, e.g. ".4bpp.smol", ".gbapal", ".smolTM"
    args: str
    symbol: str
    src: str
    line: int


@dataclass
class Issue:
    level: str  # "error" | "warning"
    file: str
    message: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class CheckReport:
    issues: list[Issue] = field(default_factory=list)
    checked: int = 0
    output: str = ""
    command: str = ""
    returncode: int | None = None
    seconds: float = 0.0

    @property
    def errors(self) -> list[str]:
        return [f"{i.file}: {i.message}" for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[str]:
        return [f"{i.file}: {i.message}" for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors and self.returncode in (None, 0)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checked": self.checked, "errors": self.errors, "warnings": self.warnings,
                "command": self.command, "returncode": self.returncode, "output": self.output[-6000:],
                "seconds": round(self.seconds, 1), "changed_assets": [], "in_place": [], "relocated": [],
                "patches": []}


def _source_of(built: str, root: Path) -> str:
    """Map a built artefact path (``x.4bpp.lz``, ``x.gbapal``) back to the file in the repo."""
    p = Path(built)
    if p.suffix in (".png", ".pal", ".bin") and (root / built).exists():
        return built
    name = p.name
    stem = name.split(".")[0]
    rest = name[len(stem):]
    parent = p.parent
    if rest.startswith(".bin"):
        return str(parent / (stem + ".bin"))
    for ext in (".png", ".pal"):
        cand = parent / (stem + ext)
        if (root / cand).exists():
            return str(cand)
    return str(parent / (stem + (".pal" if rest.startswith(".gbapal") else ".png")))


def _mk_rules(root: Path) -> tuple[dict[str, str], set[str]]:
    """gbagfx arguments from ``*.mk`` rules (``$(DIR)/x.4bpp: %.4bpp: %.png`` + ``$(GFX) $< $@ -mwidth 2``),
    and the set of files that rules generate (e.g. palettes concatenated with ``cat``)."""
    out: dict[str, str] = {}
    generated: set[str] = set()
    for mk in list(root.glob("*.mk")) + [root / "Makefile"]:
        try:
            text = mk.read_text(errors="replace")
        except OSError:
            continue
        variables = dict(re.findall(r"^(\w+)\s*:?=\s*(.+?)\s*$", text, re.M))

        def sub(s: str) -> str:
            return re.sub(r"\$\((\w+)\)", lambda m: variables.get(m.group(1), m.group(0)), s)

        for m in re.finditer(r"^(\S[^\n:]*?\.(?:4bpp|8bpp|1bpp|gbapal))\s*:[^\n]*\n\t\$\(GFX\) \$< \$@ ([^\n]+)", text, re.M):
            for target in sub(m.group(1)).split():
                out[_source_of(target, root)] = m.group(2).strip()
        for m in re.finditer(r"^(\$\(\w+\)/\S+|graphics/\S+|data/\S+?)\s*:(?!=)", text, re.M):
            for target in sub(m.group(1)).split():
                if not target.startswith("%"):
                    generated.add(_source_of(target, root))
    return out, generated


_COMMENT_RE = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"', re.S)


def strip_comments(text: str) -> str:
    """Blank out C comments (keeping string literals, offsets and line breaks intact)."""
    def repl(m):
        t = m.group(0)
        return t if t.startswith('"') else re.sub(r"[^\n]", " ", t)
    return _COMMENT_RE.sub(repl, text)


def scan_references(root: Path) -> list[Ref]:
    refs: list[Ref] = []
    files = list((root / "src").rglob("*.[ch]")) + list((root / "include").rglob("*.h"))
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if "INCGFX" not in text and "INCBIN" not in text:
            continue
        text = strip_comments(text)
        rel = str(f.relative_to(root))
        line_starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]
        for m in INC_RE.finditer(text):
            end = text.find(")", m.end())
            if end < 0:
                continue
            strings = STR_RE.findall(text[m.end():end])
            if not strings:
                continue
            stmt_start = text.rfind(";", 0, m.start()) + 1
            decls = list(DECL_RE.finditer(text, stmt_start, m.start()))
            symbol = ""
            if decls:
                d = decls[-1]
                between = text[d.end():m.start()]
                symbol = d.group(1)
                if "{" in between:  # element of an array initializer
                    symbol += f"[{len(INC_RE.findall(between))}]"
            line = bisect.bisect_right(line_starts, m.start())
            if m.group(1) == "GFX":
                path, fmt = strings[0], strings[1] if len(strings) > 1 else ""
                args = strings[2] if len(strings) > 2 else ""
                refs.append(Ref(path, fmt, args, symbol, rel, line))
            else:
                for path in strings:
                    name = Path(path).name
                    fmt = name[len(name.split(".")[0]):]
                    refs.append(Ref(_source_of(path, root), fmt, "", symbol, rel, line))
    return refs


def _defines(root: Path) -> dict[str, str]:
    out = {}
    for f in list((root / "include" / "config").glob("*.h")) + [root / "include" / "fieldmap.h"]:
        try:
            out.update(DEFINE_RE.findall(f.read_text(errors="replace")))
        except OSError:
            pass
    return out


def _eval_int(expr: str, defines: dict[str, str]) -> int | None:
    expr = expr.strip().rstrip(",")
    m = re.fullmatch(r"(\w+)\s*\?\s*(\w+)\s*:\s*(\w+)", expr)
    if m:
        cond = defines.get(m.group(1), "FALSE")
        expr = m.group(2) if cond not in ("FALSE", "0") else m.group(3)
    expr = defines.get(expr, expr)
    try:
        return int(expr, 0)
    except ValueError:
        return None


def _symbol_parts(sym: str) -> tuple[str, str]:
    base = sym.split("[")[0]
    if "_" in base:
        pre, suf = base.split("_", 1)
        return pre, suf
    return base, ""


def _is_pal_symbol(sym: str) -> bool:
    pre = _symbol_parts(sym)[0]
    return any(w in pre for w in PAL_WORDS)


class DecompProject:
    kind = "decomp"

    def __init__(self, root: Path, log=None):
        self.root = Path(root).resolve()
        if not is_decomp(self.root):
            raise DecompError(f"{self.root} does not look like a pokeemerald/pokefirered source tree")
        self.log = log or (lambda *a: None)
        self.state_dir = self.root / ".pae"
        self.config = {"game": {"name": f"{self.root.name} (source tree)", "code": "decomp"},
                       "build": {"command": os.environ.get("PAE_MAKE", f"make -j{os.cpu_count() or 2}")}}
        cfg = self.state_dir / "config.json"
        if cfg.exists():
            try:
                self.config["build"].update(json.loads(cfg.read_text()).get("build", {}))
            except (ValueError, OSError):
                pass
        self._git = None
        self._head_cache: dict[str, bytes | None] = {}
        self._changed: tuple[float, dict[str, str]] | None = None
        self.reindex()

    # ------------------------------------------------------------------ indexing
    def reindex(self) -> None:
        t0 = time.time()
        root = self.root
        self.defines = _defines(root)
        self.refs = scan_references(root)
        self.mk_args, self.generated = _mk_rules(root)
        self.refs_by_file: dict[str, list[Ref]] = defaultdict(list)
        for r in self.refs:
            self.refs_by_file[r.file].append(r)
        self.symbol_file: dict[str, str] = {}
        for r in self.refs:
            if r.symbol:
                self.symbol_file.setdefault(r.symbol, r.file)
        files = set()
        for top in ("graphics", "data/tilesets"):
            base = root / top
            if base.exists():
                for p in base.rglob("*"):
                    if p.suffix in (".png", ".pal") and p.is_file():
                        files.add(str(p.relative_to(root)))
        files |= {f for f in self.refs_by_file if f.endswith((".png", ".pal")) and (root / f).exists()}
        self.assets: dict[str, Asset] = {}
        self.alias_of: dict[str, str] = {}
        icon_pals = self._icon_palettes()
        links = self._palette_links(files)
        for f in sorted(files):
            self.assets[f] = self._make_asset(f, links, icon_pals)
        self._add_tileset_views()
        self._add_bg_views()
        self.log(f"indexed {len(self.assets)} assets from {len(self.refs)} references in {time.time() - t0:.1f}s")

    def _category(self, rel: str) -> str:
        parts = rel.split("/")
        if parts[0] == "data" and len(parts) > 1 and parts[1] == "tilesets":
            return "tilesets"
        if parts[0] == "graphics" and len(parts) > 2:
            return {"object_events": "overworld", "battle_environment": "battle_backgrounds"}.get(parts[1], parts[1])
        return parts[0]

    def _label(self, rel: str) -> str:
        p = Path(rel)
        parts = p.parts
        if len(parts) > 2 and parts[1] == "pokemon":
            species = parts[2].replace("_", " ").title()
            forms = [x.replace("_", " ") for x in parts[3:-1]]
            return " · ".join([species, *forms, p.stem.replace("_", " ")])
        return " · ".join(x.replace("_", " ") for x in parts[2:-1] + (p.stem,)) or p.stem

    def _icon_palettes(self) -> dict[str, int]:
        """icon symbol -> icon palette index, from the species info files."""
        out: dict[str, int] = {}
        pat = re.compile(r"\.iconSprite(Female)?\s*=\s*(\w+)\s*,\s*(?://[^\n]*\n\s*)?\.iconPalIndex(?:Female)?\s*=\s*([^,\n]+)")
        for f in (self.root / "src" / "data" / "pokemon" / "species_info").glob("*.h"):
            for m in pat.finditer(strip_comments(f.read_text(errors="replace"))):
                v = _eval_int(m.group(3), self.defines)
                if v is not None:
                    out.setdefault(m.group(2), v)
        return out

    def _icon_palette_files(self) -> list[str]:
        refs = [r for r in self.refs if r.symbol.startswith("gMonIconPalettes[")]
        refs.sort(key=lambda r: int(r.symbol.split("[")[1].rstrip("]")))
        return [r.file for r in refs]

    def _palette_links(self, files: set[str]) -> dict[str, tuple[str | None, list[str], str]]:
        """image -> (in-game palette file or None for 'own PNG palette', alternative palettes, how it was found)."""
        root = self.root
        links: dict[str, tuple[str | None, list[str], str]] = {}
        pal_syms: dict[str, list[str]] = defaultdict(list)  # (src, suffix) -> palette symbols
        by_src_suffix: dict[tuple[str, str], list[Ref]] = defaultdict(list)
        for r in self.refs:
            if r.symbol:
                by_src_suffix[(r.src, _symbol_parts(r.symbol)[1])].append(r)
        png_self = {r.file for r in self.refs if r.file.endswith(".png") and r.fmt.startswith(".gbapal")}
        # block co-occurrence: an initializer {…} mentioning exactly one image symbol and some palette symbols
        cooc: dict[str, set[str]] = defaultdict(set)
        sym_kind = {}
        for r in self.refs:
            if not r.symbol:
                continue
            if r.fmt.startswith(".gbapal"):
                sym_kind[r.symbol] = "pal"
            elif r.file.endswith(".png"):
                sym_kind[r.symbol] = "gfx"
        for f in {r.src for r in self.refs} | {str(p.relative_to(root)) for p in (root / "src" / "data").rglob("*.h")}:
            try:
                text = strip_comments((root / f).read_text(errors="replace"))
            except OSError:
                continue
            idents = [(m.start(), m.group(0)) for m in IDENT_RE.finditer(text) if m.group(0) in sym_kind]
            if not idents:
                continue
            pos = [p for p, _ in idents]
            stack = []
            for i, ch in enumerate(text):
                if ch == "{":
                    stack.append(i)
                elif ch == "}" and stack:
                    s = stack.pop()
                    if i - s > 1500:
                        continue
                    lo, hi = bisect.bisect_left(pos, s), bisect.bisect_right(pos, i)
                    syms = {name for _, name in idents[lo:hi]}
                    gfx = [x for x in syms if sym_kind[x] == "gfx"]
                    pals = [x for x in syms if sym_kind[x] == "pal"]
                    if len(gfx) == 1 and pals:
                        cooc[gfx[0]].update(pals)
        icon_files = self._icon_palette_files()
        icon_pal_of = self._icon_palettes()
        ow = self._ow_palette_links()
        for f in files:
            if not f.endswith(".png"):
                continue
            p = Path(f)
            parts = p.parts
            if any(r.fmt.startswith(".1bpp") for r in self.refs_by_file.get(f, [])) or "footprint" in p.stem:
                continue  # 1bpp art (footprints, fonts) has no palette of its own
            # 1) Pokémon conventions
            if len(parts) > 3 and parts[1] == "pokemon" and parts[2] != "icon_palettes":
                stem = p.stem
                gba = stem.endswith("_gba")
                base = stem.removesuffix("_gba").rstrip("f") if stem not in ("iconf",) else "icon"
                if base.startswith("icon"):
                    syms = [r.symbol for r in self.refs_by_file.get(f, []) if r.symbol]
                    idx = next((icon_pal_of[s] for s in syms if s in icon_pal_of), None)
                    if idx is not None and idx < len(icon_files):
                        links[f] = (icon_files[idx], [], "species iconPalIndex")
                    elif icon_files:
                        # forms defined through macros: recognise the icon palette by the PNG's own colours
                        match = self._match_palette(f, icon_files)
                        if match:
                            links[f] = (match, [], "icon palette matching the PNG colours")
                    continue
                if base.startswith("overworld"):
                    names = ("overworld_normal.pal", "overworld_shiny.pal")
                elif base.startswith(("anim_front", "front", "back")):
                    sfx = "_gba" if gba else ""
                    names = (f"normal{sfx}.pal", f"shiny{sfx}.pal")
                else:
                    names = None
                if names:
                    found = []
                    for n in names:
                        d = p.parent
                        hit = None
                        while len(d.parts) >= 3:
                            if (root / d / n).exists():
                                hit = str(d / n)
                                break
                            d = d.parent
                        found.append(hit)
                    if found[0]:
                        links[f] = (found[0], [x for x in found[1:] if x], "pokemon folder convention")
                        continue
            # 2) overworld sprites: pic -> pic table -> graphics info .paletteTag -> palette table
            if f in ow:
                links[f] = (ow[f], [], "object event paletteTag")
                continue
            # 3) the PNG is its own palette source
            if f in png_self:
                links[f] = (None, [], "palette built from this PNG")
                continue
            syms = [r.symbol for r in self.refs_by_file.get(f, []) if r.symbol]
            # 3) symbols that appear together in one initializer
            cands = [pf for s in syms for x in sorted(cooc.get(s, ())) if (pf := self.symbol_file.get(x))]
            # 4) same suffix in the same source file: gFooGfx_Bar <-> gFooPal_Bar
            if not cands:
                for r in self.refs_by_file.get(f, []):
                    if not r.symbol:
                        continue
                    pre, suf = _symbol_parts(r.symbol)
                    if not suf:
                        continue
                    pals = [x for x in by_src_suffix[(r.src, suf)] if x.fmt.startswith(".gbapal") and x.file != f]
                    pals.sort(key=lambda x: ("Shiny" in x.symbol, -len(os.path.commonprefix([pre, _symbol_parts(x.symbol)[0]]))))
                    cands.extend(x.file for x in pals)
            cands = [c for c in dict.fromkeys(cands) if (root / c).exists() and c.endswith((".pal", ".png"))]
            # prefer the palette whose file name matches the image (one icon can be shared by several items)
            cands.sort(key=lambda c: (Path(c).stem != p.stem, -len(os.path.commonprefix([Path(c).stem, p.stem]))))
            if cands:
                main = [c for c in cands if "shiny" not in c.lower()] or cands
                alts = [c for c in cands if c != main[0]]
                links[f] = (None if main[0] == f else main[0], alts, "paired symbols in the source")
                continue
            # 5) a .pal next to it with the same name
            sib = p.with_suffix(".pal")
            if str(sib) in files:
                links[f] = (str(sib), [], "same-name .pal file")
        return links

    def _match_palette(self, png: str, candidates: list[str]) -> str | None:
        try:
            with Image.open(self.root / png) as im:
                raw = im.getpalette() or []
        except OSError:
            return None
        cols = [tuple(raw[i:i + 3]) for i in range(0, min(len(raw), 48), 3)]
        best, best_score = None, -1
        for c in candidates:
            try:
                pal = self.palette_colors(c)
            except (OSError, ValueError):
                continue
            score = sum(1 for x, y in zip(cols, pal) if same_color(x, y))
            if score > best_score:
                best, best_score = c, score
        return best if best_score >= 12 else None

    def _ow_palette_links(self) -> dict[str, str]:
        root = self.root
        texts = []
        for f in list((root / "src").glob("*.c")) + list((root / "src" / "data").rglob("*.h")):
            try:
                t = f.read_text(errors="replace")
            except OSError:
                continue
            if "OBJ_EVENT_PAL_TAG" in t or "SpriteFrameImage" in t:
                texts.append(strip_comments(t))
        tag_pal: dict[str, str] = {}
        tables: dict[str, list[str]] = {}
        info: list[tuple[str, str]] = []
        for t in texts:
            for m in re.finditer(r"\{\s*(g\w+)\s*,\s*(OBJ_EVENT_PAL_TAG_\w+)\s*\}", t):
                tag_pal.setdefault(m.group(2), m.group(1))
            for m in re.finditer(r"struct SpriteFrameImage\s+(\w+)\[\]\s*=\s*\{(.*?)\};", t, re.S):
                tables[m.group(1)] = re.findall(r"\bg\w+", m.group(2))
            for m in re.finditer(r"struct ObjectEventGraphicsInfo\s+\w+\s*=\s*\{(.*?)\};", t, re.S):
                tag = re.search(r"\.paletteTag\s*=\s*(\w+)", m.group(1))
                img = re.search(r"\.images\s*=\s*(\w+)", m.group(1))
                if tag and img:
                    info.append((img.group(1), tag.group(1)))
        files_of_symbol: dict[str, list[str]] = defaultdict(list)
        for r in self.refs:
            if r.symbol:
                files_of_symbol[r.symbol].append(r.file)
        out: dict[str, str] = {}
        for table, tag in info:
            pal = self.symbol_file.get(tag_pal.get(tag, ""))
            if not pal:
                continue
            for sym in tables.get(table, []):
                for f in files_of_symbol.get(sym, []):
                    if f.endswith(".png"):
                        out.setdefault(f, pal)
        return out

    def _make_asset(self, f: str, links, icon_pals) -> Asset:
        refs = self.refs_by_file.get(f, [])
        cat = self._category(f)
        meta = {"references": [{"symbol": r.symbol, "format": r.fmt, "args": r.args, "source": f"{r.src}:{r.line}"}
                               for r in refs][:12]}
        if not refs:
            meta["unreferenced"] = True
        if f.endswith(".pal"):
            return Asset(f, "palette", cat, self._label(f), {}, {}, [], meta, [], "", path=f)
        fmts = [r.fmt for r in refs if not r.fmt.startswith(".gbapal")]
        bpp = 8 if any(x.startswith(".8bpp") for x in fmts) else 1 if any(x.startswith(".1bpp") for x in fmts) else 4
        args = " ".join(r.args for r in refs) + " " + self.mk_args.get(f, "")
        mw = re.search(r"-mwidth\s+(\d+)", args)
        mh = re.search(r"-mheight\s+(\d+)", args)
        nt = re.search(r"-num_tiles\s+(\d+)", args)
        layout = {"bpp": bpp}
        if mw and mh:
            layout.update({"frame_w": int(mw.group(1)), "frame_h": int(mh.group(1))})
        if nt:
            layout["num_tiles"] = int(nt.group(1))
        pal, alts, how = links.get(f, (None, [], "this PNG's own palette"))
        lk = [{"palette": pal, "png_bank": 0, "pal_bank": 0}] if pal else []
        meta.update({"palette_source": how, "alt_palettes": alts})
        if "anim_front" in Path(f).stem:
            layout.update({"frame_w": 8, "frame_h": 8})
        return Asset(f, "sprite" if bpp != 8 else "image", cat, self._label(f), {}, layout, lk, meta, [], "", path=f)

    def _add_tileset_views(self) -> None:
        root = self.root
        ts_dirs = sorted({str(Path(f).parent) for f in self.assets if f.startswith("data/tilesets/")
                          and f.endswith("/tiles.png")})
        # symbol names -> dirs, to pair secondaries with their primary through the map layouts
        dir_of_symbol = {}
        headers = root / "src" / "data" / "tilesets" / "headers.h"
        tile_sym_dir = {r.symbol: str(Path(r.file).parent) for r in self.refs if r.file.endswith("/tiles.png")}
        if headers.exists():
            for m in re.finditer(r"struct Tileset\s+(\w+)\s*=\s*\{(.*?)\};", headers.read_text(errors="replace"), re.S):
                t = re.search(r"\.tiles\s*=\s*(\w+)", m.group(2))
                if t and t.group(1) in tile_sym_dir:
                    dir_of_symbol[m.group(1)] = tile_sym_dir[t.group(1)]
        pairs: dict[str, Counter] = defaultdict(Counter)
        layouts = root / "data" / "layouts" / "layouts.json"
        if layouts.exists():
            try:
                for lay in json.loads(layouts.read_text()).get("layouts", []):
                    p, s = dir_of_symbol.get(lay.get("primary_tileset")), dir_of_symbol.get(lay.get("secondary_tileset"))
                    if p and s:
                        pairs[s][p] += 1
            except ValueError:
                pass
        for d in ts_dirs:
            secondary = "/secondary/" in d + "/"
            primary = pairs[d].most_common(1)[0][0] if pairs.get(d) else None
            frlg = "frlg" in (primary or d)
            npp = int(self.defines.get("NUM_PALS_IN_PRIMARY_FRLG" if frlg else "NUM_PALS_IN_PRIMARY", 7 if frlg else 6))
            npt = int(self.defines.get("NUM_TILES_IN_PRIMARY_FRLG" if frlg else "NUM_TILES_IN_PRIMARY", 640 if frlg else 512))
            links = []
            for b in range(16):
                src_dir = primary if (secondary and primary and b < npp) else d
                pf = f"{src_dir}/palettes/{b:02d}.pal"
                if pf in self.assets:
                    links.append({"palette": pf, "png_bank": b, "pal_bank": 0})
            if not links:
                continue
            aid = d + "@tileset"
            self.assets[aid] = Asset(aid, "tileset", "tilesets", self._label(d + "/tiles.png") + " (coloured view)", {}, {},
                                     links, {"tiles_file": d + "/tiles.png", "metatiles_file": d + "/metatiles.bin",
                                             "secondary": secondary, "primary": primary, "num_primary_tiles": npt,
                                             "num_primary_palettes": npp, "virtual": True}, [], "", path=d + "/tiles.png")

    def _add_bg_views(self) -> None:
        maps = [r for r in self.refs if r.file.endswith(".bin") and (".smolTM" in r.fmt or r.fmt in (".bin", ".bin.lz", ".bin.smolTM"))
                and any(w in _symbol_parts(r.symbol)[0] for w in MAP_WORDS)]
        by_src_suffix: dict[tuple[str, str], list[Ref]] = defaultdict(list)
        for r in self.refs:
            if r.symbol:
                by_src_suffix[(r.src, _symbol_parts(r.symbol)[1])].append(r)
        seen = set()
        for m in maps:
            if m.file in seen or not (self.root / m.file).exists():
                continue
            pre, suf = _symbol_parts(m.symbol)
            group = by_src_suffix.get((m.src, suf), [])
            root_pre = pre
            for w in MAP_WORDS:
                root_pre = root_pre.replace(w, "")
            tiles = [r for r in group if r.file.endswith(".png") and not r.fmt.startswith(".gbapal")]
            tiles.sort(key=lambda r: -len(os.path.commonprefix([root_pre, _symbol_parts(r.symbol)[0]])))
            if not tiles:
                stem = Path(m.file).stem
                for cand in (stem.replace("map", "tiles"), stem, stem.replace("_map", "")):
                    f = str(Path(m.file).parent / (cand + ".png"))
                    if f in self.assets:
                        tiles = [Ref(f, ".4bpp", "", "", m.src, 0)]
                        break
            if not tiles or tiles[0].file not in self.assets:
                continue
            size = (self.root / m.file).stat().st_size
            if size == 0x1000:
                mw, mh = 64, 32
            elif size % 64 == 0 and 0 < size <= 0x1000:
                mw, mh = 32, size // 64
            else:
                continue
            tile_asset = self.assets[tiles[0].file]
            pal_file = tile_asset.palettes[0]["palette"] if tile_asset.palettes else None
            pals = [r.file for r in group if r.fmt.startswith(".gbapal")]
            if pal_file is None and pals:
                pal_file = pals[0]
            if pal_file is None:
                pal_file = tiles[0].file  # PNG's own palette
            seen.add(m.file)
            aid = f"{str(Path(m.file).with_suffix(''))}@bg"
            self.assets[aid] = Asset(aid, "bg", self._category(m.file), self._label(m.file) + " (background)", {},
                                     {"map_w": mw, "map_h": mh}, [{"palette": pal_file, "png_bank": 0, "pal_bank": 0}],
                                     {"tiles_file": tiles[0].file, "map_file": m.file, "virtual": True,
                                      "compressed_map": ".smolTM" in m.fmt or ".lz" in m.fmt}, [], "", path=tiles[0].file)

    # ------------------------------------------------------------------ basic API (shared with the ROM project)
    @property
    def title(self) -> str:
        return self.config["game"]["name"]

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.assets.values():
            out[a.category] = out.get(a.category, 0) + 1
        return out

    def get(self, ident: str) -> Asset:
        ident = ident.strip()
        if ident.startswith(str(self.root)):
            ident = os.path.relpath(ident, self.root)
        if ident in self.assets:
            return self.assets[ident]
        for ext in (".png", ".pal", "@tileset", "@bg"):
            if ident + ext in self.assets:
                return self.assets[ident + ext]
        raise DecompError(f"unknown asset '{ident}'")

    def path(self, asset: Asset) -> Path:
        return self.root / asset.file

    def palette_colors(self, ident: str) -> list[RGB]:
        p = self.root / ident
        if ident.endswith(".png"):
            with Image.open(p) as im:
                raw = im.getpalette() or []
            return [tuple(raw[i:i + 3]) for i in range(0, len(raw) - 2, 3)][:256]
        return read_jasc(p)

    def users_of_palette(self, pid: str) -> list[str]:
        return [a.id for a in self.assets.values() if any(l["palette"] == pid for l in a.palettes)]

    # ------------------------------------------------------------------ git
    def _run_git(self, *args, input_bytes: bytes | None = None) -> subprocess.CompletedProcess | None:
        if self._git is False:
            return None
        try:
            r = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, input=input_bytes, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            self._git = False
            return None
        if self._git is None:
            self._git = r.returncode == 0 or b"not a git repository" not in r.stderr
        return r if self._git else None

    def original_bytes(self, rel: str) -> bytes | None:
        """The committed (HEAD) version of a file, or None if unknown / new."""
        if rel not in self._head_cache:
            r = self._run_git("show", f"HEAD:{rel}")
            self._head_cache[rel] = r.stdout if r is not None and r.returncode == 0 else None
            if self._head_cache[rel] is None:
                backup = self.state_dir / "originals" / rel
                if backup.exists():
                    self._head_cache[rel] = backup.read_bytes()
        return self._head_cache[rel]

    def changed_files(self) -> dict[str, str]:
        if self._changed is not None and time.time() - self._changed[0] < 2.0:
            return self._changed[1]
        out = self._changed_files()
        self._changed = (time.time(), out)
        return out

    def _changed_files(self) -> dict[str, str]:
        r = self._run_git("status", "--porcelain", "-uall", "--", "graphics", "data/tilesets")
        out = {}
        if r is not None and r.returncode == 0:
            for line in r.stdout.decode(errors="replace").splitlines():
                code, path = line[:2], line[3:].strip().strip('"')
                if " -> " in path:
                    path = path.split(" -> ")[1]
                out[path] = "new" if code.strip() == "??" else "deleted" if "D" in code else "modified"
            return out
        base = self.state_dir / "originals"
        if base.exists():
            for p in base.rglob("*"):
                if p.is_file():
                    rel = str(p.relative_to(base))
                    cur = self.root / rel
                    if not cur.exists():
                        out[rel] = "deleted"
                    elif cur.read_bytes() != p.read_bytes():
                        out[rel] = "modified"
        return out

    def _backup(self, rel: str) -> None:
        """Without git, keep a copy of a file before the tool first overwrites it."""
        if self._run_git("rev-parse", "--is-inside-work-tree") is not None:
            return
        dst = self.state_dir / "originals" / rel
        src = self.root / rel
        if not dst.exists() and src.exists():
            from .cli import _exclude_state_dir
            _exclude_state_dir(self.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    def is_touched(self, asset: Asset) -> bool:
        changed = self.changed_files()
        return any(f in changed for f in self._files_of(asset))

    def _files_of(self, asset: Asset) -> list[str]:
        if asset.kind == "tileset":
            return [asset.meta["tiles_file"]]
        if asset.kind == "bg":
            return [asset.meta["tiles_file"], asset.meta["map_file"]]
        return [asset.file]

    def status(self) -> list[dict]:
        changed = self.changed_files()
        return [{"id": f, "state": s, "file": f} for f, s in sorted(changed.items())]

    def revert(self, asset: Asset) -> None:
        for f in self._files_of(asset):
            if self._run_git("rev-parse", "--is-inside-work-tree") is not None:
                r = self._run_git("checkout", "HEAD", "--", f)
                if r is None or r.returncode != 0:
                    raise DecompError(f"could not restore {f}: {r.stderr.decode(errors='replace') if r else 'git error'}")
                self._changed = None
            else:
                b = self.state_dir / "originals" / f
                if not b.exists():
                    raise DecompError(f"no original copy of {f} (use git to be able to revert any edit)")
                (self.root / f).write_bytes(b.read_bytes())
                self._changed = None

    # ------------------------------------------------------------------ pixels
    def _png(self, rel: str) -> LoadedImage:
        return load_png(self.root / rel)

    def _original_png(self, rel: str) -> LoadedImage | None:
        raw = self.original_bytes(rel)
        if raw is None:
            return None
        try:
            return load_png(io.BytesIO(raw))
        except OSError:
            return None

    def _ingame_palette(self, asset: Asset, override: str | None = None) -> list[RGB] | None:
        pid = override or (asset.palettes[0]["palette"] if asset.palettes else None)
        if not pid:
            return None
        try:
            return self.palette_colors(pid)
        except (OSError, ValueError):
            return None

    def _base_palette(self, asset: Asset, img: LoadedImage | None = None) -> list[RGB]:
        """The colours an image is drawn with: its in-game .pal, else its own PNG palette (or the committed one)."""
        pal = self._ingame_palette(asset)
        if pal:
            return pal
        img = img or self._png(asset.file)
        if img.palette:
            return list(img.palette)
        orig = self._original_png(asset.file)
        if orig is not None and orig.palette:
            return list(orig.palette)
        return list(GRAYSCALE16)

    def _tileset_data(self, asset: Asset):
        """(tiles (n,8,8), per-tile bank list, 256-colour PLTE, tiles PNG width in tiles)."""
        m = asset.meta
        img = self._png(m["tiles_file"])
        if img.indices is None:
            raise DecompError(f"{m['tiles_file']} is not an indexed PNG (run `pae fix`)")
        lay = Layout.sheet((img.width // 8) * (img.height // 8), img.width // 8)
        tiles = image_to_tiles(img.indices & 0x0F, lay)
        votes = [Counter() for _ in range(len(tiles))]
        npt = m["num_primary_tiles"]
        for mfile, offset_ok in ((m["metatiles_file"], True),):
            p = self.root / mfile
            if p.exists():
                data = p.read_bytes()
                for i in range(0, len(data) - 1, 2):
                    v = data[i] | (data[i + 1] << 8)
                    t, bank = v & 0x3FF, v >> 12
                    if m["secondary"]:
                        t -= npt
                    if 0 <= t < len(tiles):
                        votes[t][bank] += 1
        default = m["num_primary_palettes"] if m["secondary"] else 0
        banks = [v.most_common(1)[0][0] if v else default for v in votes]
        plte = [formats.FILLER] * 256
        for link in asset.palettes:
            try:
                cols = pad_palette(self.palette_colors(link["palette"]), 16)
            except (OSError, ValueError):
                continue
            b = link["png_bank"] * 16
            plte[b:b + 16] = cols
        return tiles, banks, plte, img.width // 8

    def _bg_data(self, asset: Asset):
        m = asset.meta
        img = self._png(m["tiles_file"])
        if img.indices is None:
            raise DecompError(f"{m['tiles_file']} is not an indexed PNG (run `pae fix`)")
        lay = Layout.sheet((img.width // 8) * (img.height // 8), img.width // 8)
        raw_tiles = encode_tiles(image_to_tiles(img.indices & 0x0F, lay))
        raw_map = (self.root / m["map_file"]).read_bytes()
        cols = self._ingame_palette(asset) or list(GRAYSCALE16)
        nbanks = max(1, len(cols) // 16)
        entries = formats.map_entries(raw_map)
        used = sorted({e >> 12 for e in entries})
        base = used[0] if used and used[-1] - used[0] < nbanks else 0
        plte = [formats.FILLER] * 256
        for b in range(16):
            j = (b - base) % nbanks
            plte[b * 16:b * 16 + 16] = pad_palette(cols[j * 16:j * 16 + 16], 16)
        return raw_tiles, raw_map, plte, img.width // 8, img.palette

    def view(self, asset: Asset, palette_override: str | None = None) -> tuple[np.ndarray, list[RGB], bool]:
        """(indices, PLTE, index-0-is-transparent) as the asset appears in game."""
        if asset.kind == "tileset":
            tiles, banks, plte, w = self._tileset_data(asset)
            lay = Layout.sheet(len(tiles), w)
            shaded = tiles | np.array(banks, dtype=np.uint8)[:, None, None] << 4
            return tiles_to_image(shaded, lay), plte, False
        if asset.kind == "bg":
            raw_tiles, raw_map, plte, _w, _p = self._bg_data(asset)
            return formats.render_bg(asset, raw_tiles, raw_map), plte, False
        img = self._png(asset.file)
        idx = img.indices
        base = self._base_palette(asset, img)
        if idx is None:
            idx, _ = remap_to_palette(img.rgba, pad_palette(base, 16)[:16])
        pal = self._ingame_palette(asset, palette_override) if palette_override else base
        transparent = asset.category in SPRITE_CATEGORIES and asset.layout.get("bpp", 4) == 4
        return idx, pad_palette(pal, max(16, len(pal))), transparent

    def render_image(self, asset: Asset, palette_override: str | None = None, palettes=None) -> Image.Image:
        if asset.kind == "palette":
            cols = self.palette_colors(asset.file)
            n = max(1, len(cols))
            w = min(16, n)
            arr = np.array(pad_palette(cols, -(-n // 16) * 16 if n > 16 else w), dtype=np.uint8).reshape(-1, w, 3)
            return Image.fromarray(arr, "RGB").resize((w * 16, arr.shape[0] * 16), Image.NEAREST)
        idx, plte, transparent = self.view(asset, palette_override)
        im = Image.frombytes("P", (idx.shape[1], idx.shape[0]), np.ascontiguousarray(idx, np.uint8).tobytes())
        im.putpalette([v for c in pad_palette(plte, 256) for v in c[:3]])
        if transparent:
            im.info["transparency"] = 0
        return im.convert("RGBA")

    # ------------------------------------------------------------------ writing
    def _write_png(self, rel: str, idx: np.ndarray, plte: list[RGB], like: LoadedImage | None) -> None:
        """Write an indexed PNG keeping the original palette length (gbagfx copies every entry into .gbapal)."""
        self._backup(rel)
        n = len(like.palette) if like is not None and like.palette else len(plte)
        plte = pad_palette(plte, max(n, int(idx.max(initial=0)) + 1))
        im = Image.frombytes("P", (idx.shape[1], idx.shape[0]), np.ascontiguousarray(idx, np.uint8).tobytes())
        im.putpalette([v for c in plte for v in c[:3]])
        bits = 1 if len(plte) <= 2 else 2 if len(plte) <= 4 else 4 if len(plte) <= 16 else 8
        im.save(self.root / rel, format="PNG", bits=bits)
        self._changed = None

    def write_view(self, asset: Asset, indices: np.ndarray) -> list[str]:
        """Write an edited in-game view (indices into the view's PLTE) back to the real files."""
        notes: list[str] = []
        if asset.kind == "tileset":
            tiles, banks, plte, w = self._tileset_data(asset)
            lay = Layout.sheet(len(tiles), w)
            new = image_to_tiles(indices, lay)
            moved = sum(1 for t in range(len(tiles)) if (new[t] & 0x0F != tiles[t]).any() and
                        np.any(((new[t] >> 4) != banks[t]) & ((new[t] & 0x0F) != 0)))
            if moved:
                notes.append(f"{moved} edited tiles use colours from another palette than their metatiles; "
                             f"only the colour index is stored")
            src = self._png(asset.meta["tiles_file"])
            self._write_png(asset.meta["tiles_file"], tiles_to_image(new & 0x0F, lay), src.palette, src)
            return notes
        if asset.kind == "bg":
            raw_tiles, raw_map, plte, w, png_pal = self._bg_data(asset)
            raw = {1: raw_tiles, 2: raw_map}
            work = Asset(**{**asset.__dict__, "blobs": {"tiles": 1, "map": 2}})
            work.layout = {**asset.layout, "retile": True}
            loaded = LoadedImage(indices.shape[1], indices.shape[0], indices, None, np.zeros((*indices.shape, 4), np.uint8))
            try:
                parsed = formats.parse(work, loaded, raw, {})
            except formats.AssetError as e:
                raise DecompError(str(e).replace(" in its VRAM slot", ""))
            new_tiles = decode_tiles(parsed.blobs[1])
            lay = Layout.sheet(len(new_tiles), w)
            src = self._png(asset.meta["tiles_file"])
            self._write_png(asset.meta["tiles_file"], tiles_to_image(new_tiles, lay), png_pal, src)
            self._backup(asset.meta["map_file"])
            (self.root / asset.meta["map_file"]).write_bytes(parsed.blobs[2])
            self._changed = None
            return notes + parsed.warnings
        img = self._png(asset.file)
        self._write_png(asset.file, indices, self._base_palette(asset, img), img if img.palette else None)
        return notes

    def import_image(self, asset: Asset, src, new_palette: bool = False, resize: bool = False) -> list[str]:
        from .project import _read_palette_source, fit_to_asset
        notes: list[str] = []
        if asset.kind == "palette":
            cols = _read_palette_source(src)
            n = len(self.palette_colors(asset.file))
            if len(cols) < n:
                cols = cols + self.palette_colors(asset.file)[len(cols):]
            self._backup(asset.file)
            write_jasc(self.root / asset.file, cols[:n])
            self._changed = None
            return notes
        idx0, plte, transparent = self.view(asset)
        h, w = idx0.shape
        with Image.open(src) as im:
            im.load()
            if im.size != (w, h):
                if not resize:
                    raise DecompError(f"{src} is {im.size[0]}x{im.size[1]} but {asset.id} is {w}x{h} "
                                      f"(pass --resize to scale it)")
                lay = asset.layout
                frame = (lay["frame_w"] * 8, lay["frame_h"] * 8) if lay.get("frame_w") else None
                im = fit_to_asset(im, (w, h), frame)
                notes.append(f"resized to {w}x{h}" + (" (one frame, repeated)" if frame and im.size != frame
                                                      and _repeated(im, frame) else ""))
            rgba = np.array(im.convert("RGBA"), dtype=np.uint8)
            src_idx = np.array(im, dtype=np.uint8) if im.mode == "P" else None
            src_pal = im.getpalette() if im.mode == "P" else None
        if not transparent:
            rgba[:, :, 3] = 255  # backgrounds/tiles: colour 0 is a real colour, not transparency
        if asset.kind in ("tileset", "bg"):
            if new_palette:
                raise DecompError("--new-palette is not supported for tilesets/backgrounds; edit their .pal files")
            linked = sorted({int(l["png_bank"]) for l in asset.palettes}) if asset.kind == "tileset" else list(range(16))
            out = np.zeros_like(idx0)
            inexact = 0
            for ty in range(0, h, 8):
                for tx in range(0, w, 8):
                    block = rgba[ty:ty + 8, tx:tx + 8]
                    prefer = int(np.bincount((idx0[ty:ty + 8, tx:tx + 8] >> 4).ravel()).argmax())
                    bank, ix, bad = formats._per_tile_remap(block, plte, linked, prefer, idx0[ty:ty + 8, tx:tx + 8] & 15)
                    out[ty:ty + 8, tx:tx + 8] = ix | (bank << 4)
                    inexact += bad
            if inexact:
                notes.append(f"{inexact} pixels were approximated to the existing palettes")
            return notes + self.write_view(asset, out)
        # sprites / plain images
        cur = self._png(asset.file)
        if new_palette:
            idx, pal = quantize_new_palette(rgba, 16)
            target = asset.palettes[0]["palette"] if asset.palettes else None
            if target and target.endswith(".pal"):
                old = self.palette_colors(target)
                n = len(old)
                newcols = pad_palette(pal, max(n, 16))[:max(n, 16)]
                self._backup(target)
                write_jasc(self.root / target, newcols)
                self._changed = None
                for other in self.users_of_palette(target):
                    if other == asset.id:
                        continue
                    oa = self.assets[other]
                    try:
                        oi = self._png(oa.file)
                    except OSError:
                        continue
                    o_idx = oi.indices if oi.indices is not None else remap_to_palette(oi.rgba, old)[0]
                    o_rgba = np.zeros((*o_idx.shape, 4), np.uint8)
                    o_rgba[..., :3] = np.array(pad_palette(old, 256), np.uint8)[o_idx]
                    o_rgba[..., 3] = np.where(o_idx == 0, 0, 255)
                    remapped, _ = remap_to_palette(o_rgba, newcols)
                    self._write_png(oa.file, remapped, newcols, oi)
                    notes.append(f"recoloured {oa.file} to the new palette")
                for alt in asset.meta.get("alt_palettes", []):
                    notes.append(f"{alt} (e.g. the shiny palette) still uses the old colour order; update it too")
            self._write_png(asset.file, idx, pal, cur)
            return notes
        pal = self._base_palette(asset, cur)
        if src_idx is not None and src_pal and int(src_idx.max(initial=0)) < 16 and \
                all(same_color(tuple(src_pal[i * 3:i * 3 + 3]), pal[i]) for i in range(min(16, len(pal), len(src_pal) // 3))):
            idx = src_idx
        else:
            hint = cur.indices
            idx, inexact = remap_to_palette(rgba, pad_palette(pal, 16)[:16] if asset.layout.get("bpp", 4) == 4 else pal,
                                            hint, 0)
            if inexact:
                notes.append(f"{inexact} pixels used colours not in the palette and were approximated "
                             f"(use --new-palette to build a palette from the art)")
        like = cur if cur.palette else self._original_png(asset.file)
        self._write_png(asset.file, idx, pal, like)
        return notes

    def set_palette(self, pid: str, colors: list[RGB]) -> list[str]:
        a = self.get(pid)
        if a.kind != "palette":
            raise DecompError(f"{a.id} is not a palette")
        n = len(self.palette_colors(a.file))
        if len(colors) != n:
            raise DecompError(f"{a.id} needs exactly {n} colours")
        self._backup(a.file)
        write_jasc(self.root / a.file, colors)
        self._changed = None
        return self.sync_palette_views(a.id)

    def sync_palette_views(self, pid: str) -> list[str]:
        """Copy a .pal file's colours into the PNGs drawn with it (pixels untouched), so editors show game colours."""
        cols = self.palette_colors(pid)
        done = []
        for aid in self.users_of_palette(pid):
            a = self.assets[aid]
            if a.kind != "sprite":
                continue
            img = self._png(a.file)
            if img.indices is None or not img.palette:
                continue
            plte = list(img.palette)
            new = pad_palette(cols, 16)[:min(16, len(plte))] + plte[16:]
            if any(not same_color(x, y) for x, y in zip(new, plte)):
                self._write_png(a.file, img.indices, new, img)
                done.append(aid)
        return done

    def palette_from_png(self, asset: Asset) -> str:
        """Copy the PNG's palette into the .pal file the game uses for it."""
        if not asset.palettes:
            raise DecompError(f"{asset.id} already uses its own palette in game")
        target = asset.palettes[0]["palette"]
        img = self._png(asset.file)
        n = len(self.palette_colors(target))
        self._backup(target)
        write_jasc(self.root / target, pad_palette(img.palette or [], n)[:n])
        self._changed = None
        return target

    # ------------------------------------------------------------------ validation & build
    def check(self, only_changed: bool = True) -> CheckReport:
        rep = CheckReport()
        changed = self.changed_files()
        files = set(changed) if only_changed else {f for f in self.refs_by_file if f.endswith((".png", ".pal"))}
        files |= {r.file for r in self.refs if r.file in changed}
        for f, state in changed.items():
            if state == "deleted" and f in self.refs_by_file:
                rep.issues.append(Issue("error", f, "file is deleted but still used by " +
                                        ", ".join(sorted({r.src for r in self.refs_by_file[f]}))))
        if not only_changed:
            for f in self.refs_by_file:
                if f.endswith((".png", ".pal", ".bin")) and not (self.root / f).exists() and f not in self.generated:
                    rep.issues.append(Issue("warning", f, "referenced in source but the file does not exist"))
        for f in sorted(files):
            p = self.root / f
            if not p.exists() or f.endswith(".bin"):
                continue
            rep.checked += 1
            a = self.assets.get(f)
            if f.endswith(".pal"):
                try:
                    cols = read_jasc(p)
                except ValueError as e:
                    rep.issues.append(Issue("error", f, str(e)))
                    continue
                orig = self.original_bytes(f)
                if orig is not None:
                    try:
                        n0 = int(orig.decode(errors="replace").split()[2])
                        if n0 != len(cols):
                            rep.issues.append(Issue("warning", f, f"colour count changed from {n0} to {len(cols)}"))
                    except (IndexError, ValueError):
                        pass
                continue
            self._check_png(f, a, rep)
        return rep

    def _check_png(self, f: str, a: Asset | None, rep: CheckReport) -> None:
        p = self.root / f
        try:
            with Image.open(p) as im:
                mode, size = im.mode, im.size
                raw_pal = im.getpalette() if im.mode == "P" else None
                arr = np.array(im) if im.mode in ("P", "L") else None
        except OSError as e:
            rep.issues.append(Issue("error", f, f"cannot read image: {e}"))
            return
        refs = self.refs_by_file.get(f, [])
        tile_out = [r for r in refs if not r.fmt.startswith(".gbapal")]
        if mode not in ("P", "L", "1"):
            rep.issues.append(Issue("error", f, f"image is {mode}; gbagfx needs an indexed (palette) PNG – run "
                                                 f"`pae fix {f}`"))
            return
        if refs and any(r.fmt.startswith(".gbapal") for r in refs) and mode != "P":
            rep.issues.append(Issue("error", f, "its palette is built from this PNG but the PNG has no palette"))
        w, h = size
        if tile_out and (w % 8 or h % 8):
            rep.issues.append(Issue("error", f, f"size {w}x{h} is not a multiple of 8"))
        if a is not None:
            fw, fh = a.layout.get("frame_w"), a.layout.get("frame_h")
            if fw and fh and (w % (fw * 8) or h % (fh * 8)) and tile_out:
                rep.issues.append(Issue("error", f, f"size {w}x{h} does not split into {fw * 8}x{fh * 8} frames"))
            bpp = a.layout.get("bpp", 4)
            if arr is not None and tile_out:
                mx = int(arr.max(initial=0))
                limit = (1 << bpp) - 1
                orig0 = self._original_png(f) if f in self.changed_files() else None
                # multi-palette art often relies on the wrap on purpose; only flag it when an edit introduced it
                if mx > limit and (f in self.changed_files()) and (orig0 is None or orig0.indices is None or
                                                                  int(orig0.indices.max(initial=0)) <= limit):
                    rep.issues.append(Issue("error" if bpp == 1 else "warning", f,
                                            f"uses colour index {mx} but {bpp}bpp only stores 0-{limit}; "
                                            f"higher indices wrap around (index % {limit + 1})"))
            nt = a.layout.get("num_tiles")
            if nt and (w // 8) * (h // 8) < nt:
                rep.issues.append(Issue("warning", f, f"has {(w // 8) * (h // 8)} tiles but the source expects {nt}"))
        orig = self._original_png(f) if f in self.changed_files() else None
        if orig is not None:
            if (orig.width, orig.height) != (w, h):
                level = "error" if a is not None and a.category in ("pokemon", "trainers", "items") else "warning"
                rep.issues.append(Issue(level, f, f"size changed from {orig.width}x{orig.height} to {w}x{h}"))
            if raw_pal is not None and orig.palette is not None and any(r.fmt.startswith(".gbapal") for r in refs):
                n_new, n_old = len(raw_pal) // 3, len(orig.palette)
                if n_new != n_old:
                    rep.issues.append(Issue("error" if n_new > n_old else "warning", f,
                                            f"PNG palette has {n_new} entries (was {n_old}); its .gbapal is built from "
                                            f"every entry, so extra colours overwrite neighbouring palettes in game"))
            if a is not None and a.palettes and raw_pal is not None and orig.palette is not None:
                cur_pal = [tuple(raw_pal[i:i + 3]) for i in range(0, min(len(raw_pal), 48), 3)]
                png_changed = any(not same_color(x, y) for x, y in zip(cur_pal, orig.palette[:16]))
                pid = a.palettes[0]["palette"]
                if png_changed and pid.endswith(".pal"):
                    game = self.palette_colors(pid)
                    if any(not same_color(x, y) for x, y in zip(cur_pal, game[:16])):
                        rep.issues.append(Issue("warning", f, f"PNG colours were changed but the game draws it with "
                                                              f"{pid}; run `pae palette-from-png {f}` to use them"))

    def build(self, output=None, patches=None, dry_run: bool = False) -> CheckReport:
        rep = self.check()
        if dry_run or rep.errors:
            return rep
        cmd = self.config["build"]["command"]
        rep.command = cmd
        t0 = time.time()
        try:
            r = subprocess.run(shlex.split(cmd), cwd=self.root, capture_output=True, text=True, timeout=3600)
        except FileNotFoundError as e:
            rep.returncode = 127
            rep.output = f"{e}\nIs the build toolchain installed? See INSTALL.md in your repository."
            return rep
        except subprocess.TimeoutExpired:
            rep.returncode = 124
            rep.output = "build timed out"
            return rep
        rep.seconds = time.time() - t0
        rep.returncode = r.returncode
        rep.output = (r.stdout + r.stderr)
        return rep

    def output_rom(self) -> Path | None:
        roms = sorted(self.root.glob("*.gba"), key=lambda p: p.stat().st_mtime)
        return roms[-1] if roms else None

    def fix(self, files: list[str] | None = None) -> list[str]:
        """Convert RGB/RGBA PNGs (as saved by many editors and image generators) into valid indexed PNGs."""
        targets = files or [f for f in self.changed_files() if f.endswith(".png")]
        done = []
        for f in targets:
            a = self.get(f)
            if a.kind not in ("sprite", "image"):
                continue
            with Image.open(self.path(a)) as im:
                if im.mode in ("P", "L", "1"):
                    continue
            self.import_image(a, self.path(a))
            done.append(a.id)
        return done

    def describe(self, asset: Asset) -> dict:
        d = {"id": asset.id, "kind": asset.kind, "category": asset.category, "label": asset.label,
             "file": asset.file, "absolute_path": str(self.path(asset)), "palettes": asset.palettes,
             "aliases": [], "layout": asset.layout, "meta": asset.meta}
        if asset.kind == "palette":
            d["constraints"] = {"colors": len(self.palette_colors(asset.file)),
                                "format": "JASC-PAL; 15-bit colour (channels rounded to multiples of 8)",
                                "used_by": self.users_of_palette(asset.id)}
            return d
        try:
            idx, plte, transparent = self.view(asset)
            hgt, wid = idx.shape
        except (OSError, DecompError) as e:
            d["constraints"] = {"error": str(e)}
            return d
        c = {"width": wid, "height": hgt, "keep_size": True}
        if asset.kind == "sprite":
            c["format"] = f"indexed PNG, {1 << asset.layout.get('bpp', 4)} colours" + \
                          (", index 0 = transparent" if transparent else "")
            c["in_game_palette"] = asset.palettes[0]["palette"] if asset.palettes else "the PNG's own palette"
            if asset.layout.get("frame_w"):
                c["frame_size_px"] = [asset.layout["frame_w"] * 8, asset.layout["frame_h"] * 8]
        elif asset.kind == "tileset":
            c["format"] = "coloured view of tiles.png (value = bank*16 + colour); only the colour index is stored, " \
                          "the palette per tile comes from metatiles.bin"
        elif asset.kind == "bg":
            c["format"] = "coloured view of tiles + tilemap; edits are re-cut into 8x8 tiles automatically"
        d["constraints"] = c
        return d


def _repeated(im: Image.Image, frame: tuple[int, int]) -> bool:
    a = np.array(im)
    return a.shape[0] > frame[1] and np.array_equal(a[:frame[1]], a[frame[1]:2 * frame[1]])


def open_project(path, log=None):
    """Open either a ROM project (``pae extract`` output) or a decomp source tree."""
    from .project import PROJECT_FILE, Project
    path = Path(path)
    if (path / PROJECT_FILE).exists():
        return Project(path)
    if is_decomp(path):
        return DecompProject(path, log=log)
    from .project import ProjectError
    raise ProjectError(f"{path} is neither a pae ROM project nor a pokeemerald-style source tree")

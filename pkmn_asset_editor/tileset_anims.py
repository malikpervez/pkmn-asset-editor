"""Tileset animations (animated water, flowers, waterfalls, lights…) for pokeemerald-style source trees.

They are plain C in ``src/tileset_anims.c``:

* frames: ``const u16 gTilesetAnims_General_Water_Frame0[] = INCGFX_U16("…/anim/water/0.png", ".4bpp");``
* a frame table, played in order: ``const u16 *const gTilesetAnims_General_Water[] = { …Frame0, …Frame1, … };``
* a queue function copying the current frame over a range of tileset tiles:
  ``AppendTilesetAnimToBuffer(gTilesetAnims_General_Water[i], (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(432)), 30 * TILE_SIZE_4BPP);``
* the tileset's per-frame callback deciding when: ``if (timer % 16 == 1) QueueAnimTiles_General_Water(timer / 16);``
  (so a new frame every 16 game frames, on the 2nd frame of each 16)
* ``InitTilesetAnim_*`` (the tileset's ``.callback`` in ``src/data/tilesets/headers.h``) choosing the callback and the
  counter's wrap-around value.

This module reads all of that into :class:`TileAnim` records, renders animated previews of maps with them, and edits
them: frame art, frame order, adding/removing frames, speed, and new animations for your own tiles.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .decomp import DecompError, strip_comments
from .gfx import Layout, image_to_tiles, load_png, pad_palette, remap_to_palette, tiles_to_image

ANIM_FILE = "src/tileset_anims.c"
HEADERS_FILE = "src/data/tilesets/headers.h"
ANIM_HEADER = "include/tileset_anims.h"
DMA_SLOTS = 20  # sTilesetDMA3TransferBuffer: copies beyond this per game frame are dropped

FRAME_RE = re.compile(r'(?:static\s+)?const\s+u16\s+(\w+)\s*\[\s*\]\s*=\s*INC(GFX|BIN)_U16\(([^;]*)\)\s*;')
ARRAY_RE = re.compile(r'(?:static\s+)?(?:const\s+)?u16\s*\*\s*const\s+(\w+)\s*\[\s*\]\s*=\s*\{(.*?)\}\s*;', re.S)
FUNC_RE = re.compile(r'^[ \t]*(?:static\s+)?void\s+(\w+)\s*\(([^)]*)\)\s*\{', re.M)
IF_RE = re.compile(r'\bif\s*\(\s*(\w+)\s*%\s*(\d+)\s*==\s*(\d+)\s*\)')
CALL_RE = re.compile(r'\b((?:QueueAnimTiles|BlendAnimPalette)_\w+)\s*\(([^;]*)\)\s*;')


@dataclass
class Slot:
    """One copy of the animation into VRAM: the frame shown is (timer // period + offset) % len(sequence)."""
    dest: int  # global tile id (0 = primary tile 0, NUM_TILES_IN_PRIMARY = secondary tile 0)
    phase: int  # copied on game frames where timer % period == phase
    offset: int = 0


@dataclass
class TileAnim:
    id: str
    table: str
    queue: str
    frames: list[str]  # distinct frame symbols, first-use order
    pngs: dict[str, str]  # frame symbol -> its PNG (the first one, for frames built from several files)
    sequence: list[int | None]  # table entries as indexes into frames (None = NULL: keep the previous frame)
    count: int  # tiles copied per frame
    slots: list[Slot]
    period: int | None = None
    callback: str | None = None
    inits: list[str] = field(default_factory=list)
    tilesets: list[str] = field(default_factory=list)
    side: str = "primary"
    counter_max: int | str | None = None  # int, "primary" (= the primary tileset's) or None (unknown)
    staggered: bool = False  # several destinations updated on successive frames (Mauville flowers, Rustboro water)
    custom: bool = False  # the queue function has extra logic; the preview is an approximation
    line: int = 0
    notes: list[str] = field(default_factory=list)
    parts: dict[str, list[str]] = field(default_factory=dict)  # frames made of several files, in order

    def as_dict(self) -> dict:
        return {"id": self.id, "table": self.table, "queue": self.queue, "callback": self.callback,
                "inits": self.inits, "tilesets": self.tilesets, "side": self.side,
                "frames": [{"symbol": s, "png": self.pngs.get(s), "files": self.parts.get(s, [self.pngs.get(s)])}
                           for s in self.frames],
                "sequence": self.sequence, "count": self.count, "period": self.period,
                "slots": [{"dest": s.dest, "phase": s.phase, "offset": s.offset} for s in self.slots],
                "counter_max": self.counter_max, "staggered": self.staggered, "custom": self.custom,
                "line": self.line, "notes": self.notes,
                "editable_speed": self.period is not None and not self.staggered}


def _split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
            continue
        depth += ch == "("
        depth -= ch == ")"
        cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _tile_offset(expr: str) -> str | None:
    """The argument of TILE_OFFSET_4BPP(...) in an expression."""
    i = expr.find("TILE_OFFSET_4BPP(")
    if i < 0:
        return None
    i += len("TILE_OFFSET_4BPP(")
    depth = 1
    for j in range(i, len(expr)):
        depth += {"(": 1, ")": -1}.get(expr[j], 0)
        if depth == 0:
            return expr[i:j]
    return None


def _block_end(text: str, open_brace: int) -> int:
    """Index just past the brace matching text[open_brace] == '{'."""
    depth = 0
    for i in range(open_brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    raise DecompError(f"unbalanced braces in {ANIM_FILE}")


def _snake(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower()


def _camel(name: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^A-Za-z0-9]+", name) if p)


class TilesetAnims:
    def __init__(self, maps):
        self.maps = maps
        self.dp = maps.dp
        self.root: Path = maps.root
        self._cache: tuple | None = None

    # ------------------------------------------------------------------ parsing
    @property
    def path(self) -> Path:
        return self.root / ANIM_FILE

    def _stamp(self):
        files = [self.path, self.root / HEADERS_FILE]
        return tuple(f.stat().st_mtime if f.exists() else 0 for f in files)

    def _eval(self, expr: str) -> int:
        d = self.dp.defines
        for _ in range(8):
            new = re.sub(r"\b[A-Za-z_]\w*\b", lambda m: str(d.get(m.group(0), m.group(0))), expr)
            if new == expr:
                break
            expr = new
        expr = re.sub(r"\(\s*u\d+\s*\)", "", expr)
        if not re.fullmatch(r"[\s0-9a-fA-FxX+\-*/()]+", expr):
            raise ValueError(expr)
        try:
            return int(eval(expr.replace("/", "//"), {"__builtins__": {}}))  # digits and operators only
        except (SyntaxError, ZeroDivisionError, TypeError) as e:
            raise ValueError(expr) from e

    def functions(self, text: str) -> dict[str, dict]:
        out = {}
        for m in FUNC_RE.finditer(text):
            end = _block_end(text, m.end() - 1)
            out[m.group(1)] = {"params": m.group(2).strip(), "start": m.start(), "body_start": m.end(),
                               "end": end, "body": text[m.end():end - 1]}
        return out

    def parse(self) -> dict:
        stamp = self._stamp()
        if self._cache and self._cache[0] == stamp:
            return self._cache[1]
        hp = self.root / HEADERS_FILE
        data = self._parse_text(self.path.read_text(errors="replace") if self.path.exists() else "",
                                hp.read_text(errors="replace") if hp.exists() else "")
        self._cache = (stamp, data)
        return data

    def original(self) -> dict:
        """The animations as they are in the last commit."""
        raw = self.dp.original_bytes(ANIM_FILE)
        hdr = self.dp.original_bytes(HEADERS_FILE)
        return self._parse_text((raw or b"").decode(errors="replace"), (hdr or b"").decode(errors="replace"))

    def _parse_text(self, raw: str, headers: str) -> dict:
        text = strip_comments(raw)
        line_of = lambda pos: text.count("\n", 0, pos) + 1
        pngs, parts, frame_pos = {}, {}, {}
        for m in FRAME_RE.finditer(text):
            files = re.findall(r'"([^"]+)"', m.group(3))
            if not files:
                continue
            if m.group(2) == "GFX":
                files = files[:1]
            files = [re.sub(r"\.4bpp$", ".png", f) for f in files if not f.startswith(".")]
            pngs[m.group(1)] = files[0]
            if len(files) > 1:
                parts[m.group(1)] = files
            frame_pos[m.group(1)] = m.start()
        tables, vdests = {}, {}
        for m in ARRAY_RE.finditer(text):
            entries = [e.strip() for e in m.group(2).split(",") if e.strip()]
            if any("TILE_OFFSET_4BPP" in e for e in entries):
                vd = []
                for e in entries:
                    arg = _tile_offset(e)
                    try:
                        vd.append(self._eval(arg) if arg else None)
                    except ValueError:
                        vd.append(None)
                vdests[m.group(1)] = vd
            else:
                tables[m.group(1)] = (entries, line_of(m.start()), m.start())
        funcs = self.functions(text)

        # callbacks: which queue functions run when
        triggers: dict[str, list[tuple[str, int, int, int | None]]] = {}
        callbacks: dict[str, list[str]] = {}
        for fname, f in funcs.items():
            body = f["body"]
            if "QueueAnimTiles_" not in body and "BlendAnimPalette_" not in body or fname.startswith("QueueAnimTiles_"):
                continue
            guarded = []
            for im in IF_RE.finditer(body):
                k = im.end()
                while k < len(body) and body[k].isspace():
                    k += 1
                stop = _block_end(body, k) if body[k:k + 1] == "{" else body.index(";", k) + 1
                guarded.append((im.start(), stop, int(im.group(2)), int(im.group(3))))
            for cm in CALL_RE.finditer(body):
                args = _split_args(cm.group(2))
                g = next((x for x in guarded if x[0] <= cm.start() < x[1]), None)
                pm = re.fullmatch(r"\w+\s*/\s*(\d+)", args[0]) if args else None
                period = int(pm.group(1)) if pm else (g[2] if g else 1)
                phase = g[3] if g else 0
                mod = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
                triggers.setdefault(cm.group(1), []).append((fname, period, phase, mod))
                callbacks.setdefault(fname, []).append(cm.group(1))

        # init functions: counter max + callback for each side
        inits = {}
        for fname, f in funcs.items():
            body = f["body"]
            cbm = re.search(r"s(Primary|Secondary)TilesetAnimCallback\s*=\s*(\w+)\s*;", body)
            if not cbm or "Init" not in fname:
                continue
            mx = re.search(rf"s{cbm.group(1)}TilesetAnimCounterMax\s*=\s*(\w+)\s*;", body)
            cmax: int | str | None = None
            if mx:
                v = mx.group(1)
                cmax = "primary" if v == "sPrimaryTilesetAnimCounterMax" else (int(v, 0) if v[0].isdigit() else None)
            inits[fname] = {"side": cbm.group(1).lower(), "callback": None if cbm.group(2) == "NULL" else cbm.group(2),
                            "counter_max": cmax, "line": line_of(f["start"])}
        tilesets = {}
        for m in re.finditer(r"struct Tileset\s+(\w+)\s*=\s*\{(.*?)\};", strip_comments(headers), re.S):
            cb = re.search(r"\.callback\s*=\s*(\w+)", m.group(2))
            tilesets[m.group(1)] = None if not cb or cb.group(1) == "NULL" else cb.group(1)

        anims: dict[str, TileAnim] = {}
        palette = []
        for fname, f in funcs.items():
            body = f["body"]
            if "AppendTilesetAnimToBuffer" not in body or not fname.startswith("QueueAnimTiles_"):
                if fname.startswith("BlendAnimPalette_") or ("gPlttBuffer" in body and fname in triggers):
                    palette.append({"function": fname, "line": line_of(f["start"]),
                                    "callbacks": [t[0] for t in triggers.get(fname, [])]})
                continue
            staggered_var = re.search(r"\b(\w+)\s*-=\s*(\w+)\s*;", body)
            custom = "else" in body
            per_table: dict[str, TileAnim] = {}
            for cm in re.finditer(r"AppendTilesetAnimToBuffer\s*\(", body):
                stop = cm.end()
                depth = 1
                while depth and stop < len(body):
                    depth += {"(": 1, ")": -1}.get(body[stop], 0)
                    stop += 1
                args = _split_args(body[cm.end():stop - 1])
                if len(args) != 3:
                    continue
                sm = re.fullmatch(r"(\w+)\s*\[(.+)\]", args[0])
                if not sm or sm.group(1) not in tables:
                    custom = True
                    continue
                table, idx = sm.group(1), sm.group(2).strip()
                if re.fullmatch(r"\w+", idx) and "timer" not in idx.lower().replace("timer_div", "timer"):
                    assign = None
                    for am in re.finditer(rf"\b{re.escape(idx)}\s*=\s*([^;]+);", body[:cm.start()]):
                        assign = am.group(1)
                    idx = assign or idx
                om = re.search(r"\(\s*\w+\s*\+\s*(\d+)\s*\)", idx)
                offset = int(om.group(1)) if om else 0
                size = args[2]
                km = re.fullmatch(r"(\d+)\s*\*\s*TILE_SIZE_4BPP", size) or re.fullmatch(r"TILE_SIZE_4BPP\s*\*\s*(\d+)", size)
                try:
                    count = int(km.group(1)) if km else self._eval(size.replace("TILE_SIZE_4BPP", "32")) // 32
                except ValueError:
                    custom = True
                    continue
                dm = _tile_offset(args[1])
                vm = re.fullmatch(r"(\w+)\s*\[\s*\w+\s*\]", args[1])
                trig = triggers.get(fname, [])
                anim = per_table.get(table)
                if anim is None:
                    entries = tables[table][0]
                    frames = list(dict.fromkeys(e for e in entries if e != "NULL"))
                    anim = TileAnim(id=table, table=table, queue=fname, frames=frames,
                                    pngs={s: pngs[s] for s in frames if s in pngs},
                                    parts={s: parts[s] for s in frames if s in parts},
                                    sequence=[None if e == "NULL" else frames.index(e) for e in entries],
                                    count=count, slots=[], line=tables[table][1], custom=custom)
                    per_table[table] = anim
                if dm:
                    try:
                        dest = self._eval(dm)
                    except ValueError:
                        anim.custom = True
                        continue
                    phases = sorted({t[2] for t in trig}) or [0]
                    anim.slots.append(Slot(dest, phases[0], offset))
                    if trig:
                        anim.period = trig[0][1]
                elif vm and vm.group(1) in vdests:
                    anim.staggered = bool(staggered_var)
                    for fn, period, phase, mod in trig:
                        dests = vdests[vm.group(1)]
                        if mod is not None and mod < len(dests) and dests[mod] is not None:
                            anim.slots.append(Slot(dests[mod], phase, -mod if anim.staggered else 0))
                            anim.period = period
                else:
                    anim.custom = True
            for anim in per_table.values():
                anim.callback = next((t[0] for t in triggers.get(fname, [])), None)
                periods = {t[1] for t in triggers.get(fname, [])}
                if len(periods) > 1:
                    anim.custom = True
                if anim.callback is None:
                    anim.notes.append(f"{fname} is never called, so this animation does not run")
                if anim.custom:
                    anim.notes.append(f"{fname} has custom logic; the preview is approximate and only the frames "
                                      "can be edited here")
                anims[anim.id] = anim
        # short ids without the gTilesetAnims_ prefix when unambiguous
        short: dict[str, list[str]] = {}
        for a in anims.values():
            short.setdefault(re.sub(r"^[gs]TilesetAnims_", "", a.table), []).append(a.table)
        renamed = {}
        for s, tables_ in short.items():
            for t in tables_:
                a = anims[t]
                a.id = s if len(tables_) == 1 else re.sub(r"^QueueAnimTiles_", "", a.queue)
                if a.id in renamed or a.id in short and a.id != s:
                    a.id = t
                renamed[a.id] = a
        for a in renamed.values():
            a.inits = [n for n, i in inits.items() if i["callback"] == a.callback and a.callback]
            if a.inits:
                a.side = inits[a.inits[0]]["side"]
                a.counter_max = inits[a.inits[0]]["counter_max"]
            a.tilesets = sorted(t for t, cb in tilesets.items() if cb in a.inits)
        return {"anims": renamed, "palette": palette, "inits": inits, "tilesets": tilesets, "callbacks": callbacks,
                "frame_pos": frame_pos, "tables": tables}

    def list(self, map_name: str | None = None) -> list[dict]:
        anims = self.parse()["anims"]
        if map_name is None:
            return [a.as_dict() for a in anims.values()]
        return [a.as_dict() for a in self.for_map(map_name)]

    def get(self, anim_id: str) -> TileAnim:
        anims = self.parse()["anims"]
        if anim_id in anims:
            return anims[anim_id]
        low = anim_id.lower()
        for a in anims.values():
            if low in (a.id.lower(), a.table.lower(), a.queue.lower()):
                return a
        raise DecompError(f"no tileset animation '{anim_id}' (see `pae anim list`)")

    # ------------------------------------------------------------------ per-map context
    def _layout_tilesets(self, map_name) -> tuple[str, str, str]:
        if isinstance(map_name, tuple):  # (primary, secondary, layout version) without a map
            return map_name
        lay = self.maps.layout_of(map_name)
        return lay["primary_tileset"], lay["secondary_tileset"], lay.get("layout_version", "emerald")

    def for_map(self, map_name: str) -> list[TileAnim]:
        data = self.parse()
        prim, sec, _ = self._layout_tilesets(map_name)
        out = []
        for ts in (prim, sec):
            init = data["tilesets"].get(ts)
            cb = data["inits"].get(init, {}).get("callback") if init else None
            out += [a for a in data["anims"].values() if cb and a.callback == cb]
        return out

    def counter_max(self, map_name: str, anim: TileAnim) -> int:
        data = self.parse()
        if anim.counter_max == "primary":
            prim, _, _ = self._layout_tilesets(map_name)
            pinit = data["inits"].get(data["tilesets"].get(prim) or "", {})
            v = pinit.get("counter_max")
            return v if isinstance(v, int) else 0
        return anim.counter_max if isinstance(anim.counter_max, int) else 256

    def _context(self, map_name: str):
        """(tiles (1024,8,8), metatiles (n,8), palettes (16,16,3), tile -> palette bank) for a map's tilesets."""
        m = self.maps
        prim_s, sec_s, version = self._layout_tilesets(map_name)
        prim, sec = m.tilesets.get(prim_s), m.tilesets.get(sec_s)
        npm, npt, npp = m.consts(version)
        tiles = np.zeros((1024, 8, 8), np.uint8)
        pt = m._tiles_of(prim)[:npt]
        tiles[: len(pt)] = pt
        st = m._tiles_of(sec)[: 1024 - npt]
        tiles[npt:npt + len(st)] = st
        pm, sm = m._metatiles_of(prim)[:npm], m._metatiles_of(sec)[: 1024 - npm]
        metas = np.zeros((npm + len(sm), 8), np.int32)
        metas[: len(pm)] = pm
        metas[npm:] = sm
        pal = m._palettes(prim, sec, npp)
        bank = np.zeros(1024, np.int32)
        flat = metas.ravel()
        t, b = flat & 0x3FF, (flat >> 12) & 0xF
        for tile in np.unique(t):
            bank[tile] = np.bincount(b[t == tile]).argmax()
        bank[npt:][~np.isin(np.arange(npt, 1024), t)] = npp
        return tiles, metas, pal, bank, npt

    def frame_tiles(self, anim: TileAnim, sym: str) -> np.ndarray:
        out = []
        for png in anim.parts.get(sym) or [anim.pngs.get(sym)]:
            if not png or not (self.root / png).exists():
                raise DecompError(f"frame {sym}: {png} not found")
            img = load_png(self.root / png)
            idx = img.indices if img.indices is not None else \
                remap_to_palette(img.rgba, pad_palette(img.palette or [], 16))[0]
            w, h = img.width // 8, img.height // 8
            out.append(image_to_tiles(idx[: h * 8, : w * 8] & 0x0F, Layout.sheet(w * h, w)))
        return np.concatenate(out) if len(out) > 1 else out[0]

    # ------------------------------------------------------------------ rendering
    @staticmethod
    def render_metatiles(tiles: np.ndarray, metas: np.ndarray, pal: np.ndarray) -> np.ndarray:
        """(k,16,16,4) RGBA cells for metatile rows (k,8), as the game draws them (bottom then top layer)."""
        k = len(metas)
        cells = np.zeros((k, 16, 16, 4), np.uint8)
        cells[..., :3] = pal[0, 0]
        cells[..., 3] = 255
        for layer in (0, 1):
            for q in range(4):
                v = metas[:, layer * 4 + q]
                px = tiles[v & 0x3FF]
                hf, vf = ((v >> 10) & 1).astype(bool), ((v >> 11) & 1).astype(bool)
                px = np.where(hf[:, None, None], px[:, :, ::-1], px)
                px = np.where(vf[:, None, None], px[:, ::-1, :], px)
                rgb = pal[((v >> 12) & 0xF)[:, None, None], px]
                ys, xs = (q // 2) * 8, (q % 2) * 8
                mask = px != 0
                region = cells[:, ys:ys + 8, xs:xs + 8, :3]
                region[mask] = rgb[mask]
        return cells

    def _states(self, map_name: str, anims: list[TileAnim], max_states: int = 400):
        """Distinct VRAM states over one full cycle: [(start game frame, {(anim id, slot #): frame symbol})]."""
        cycles = [self.counter_max(map_name, a) or 1 for a in anims]
        cycle = math.lcm(*cycles) if cycles else 1
        cycle = min(cycle, 3600)

        def last_trigger(t: int, s: Slot, period: int, cmax: int) -> int:
            """The last counter value <= t that copied this slot (wrapping into the previous cycle)."""
            if t >= s.phase:
                return t - ((t - s.phase) % period)
            return s.phase + ((cmax - 1 - s.phase) // period) * period

        def shown(a: TileAnim, s: Slot, c: int, cmax: int):
            if not a.sequence or not a.period or cmax <= 0 or s.phase >= cmax:
                return None
            n = len(a.sequence)
            t = last_trigger(c % cmax, s, a.period, cmax)
            for _ in range(n):  # NULL entries keep what was there before
                i = a.sequence[(t // a.period + s.offset) % n]
                if i is not None:
                    return a.frames[i]
                t = last_trigger(t - 1 if t > 0 else cmax - 1, s, a.period, cmax)
            return None

        states, prev = [], None
        for c in range(cycle):
            st = tuple(shown(a, s, c, cm) for a, cm in zip(anims, cycles) for s in a.slots)
            if st != prev:
                states.append((c, st))
                prev = st
        if len(states) > max_states:
            step = -(-len(states) // max_states)
            states = states[::step]
        return cycle, states

    def preview(self, map_name: str, max_states: int = 400) -> tuple[Image.Image | None, dict]:
        """A sheet of every animated metatile in each animation state (rows), for the map editor to play back.

        Returns (sheet image or None, {"metatiles": [ids], "times": [start frame of each row], "cycle": frames,
        "anims": [ids]})."""
        anims = [a for a in self.for_map(map_name) if a.period and a.slots]
        if not anims:
            return None, {"metatiles": [], "times": [], "cycle": 0, "anims": []}
        tiles0, metas, pal, _bank, _npt = self._context(map_name)
        frames = {}
        for a in anims:
            for s in a.frames:
                try:
                    frames[(a.id, s)] = self.frame_tiles(a, s)
                except DecompError:
                    pass
        covered = np.zeros(1024, bool)
        for a in anims:
            for s in a.slots:
                covered[s.dest:s.dest + a.count] = True
        ids = np.nonzero(covered[metas & 0x3FF].any(axis=1))[0]
        cycle, states = self._states(map_name, anims, max_states)
        slots = [(a, s) for a in anims for s in a.slots]
        rows = []
        for _, st in states:
            tiles = tiles0.copy()
            written = np.zeros(1024, bool)
            for (a, s), sym in zip(slots, st):
                ft = frames.get((a.id, sym)) if sym else None
                if ft is None:
                    continue
                n = min(a.count, len(ft), 1024 - s.dest)
                free = ~written[s.dest:s.dest + n]  # where two animations write the same tiles the first one wins
                tiles[s.dest:s.dest + n][free] = ft[:n][free]
                written[s.dest:s.dest + n] = True
            cells = self.render_metatiles(tiles, metas[ids], pal)
            rows.append(cells.transpose(1, 0, 2, 3).reshape(16, len(ids) * 16, 4) if len(ids) else
                        np.zeros((16, 16, 4), np.uint8))
        sheet = Image.fromarray(np.concatenate(rows, axis=0), "RGBA")
        return sheet, {"metatiles": [int(i) for i in ids], "times": [c for c, _ in states], "cycle": cycle,
                       "anims": [a.id for a in anims]}

    def map_frames(self, map_name: str, region: tuple[int, int, int, int] | None = None,
                   max_states: int = 120) -> tuple[list[Image.Image], list[int]]:
        """Whole-map (or region x, y, w, h in blocks) images for each animation state, with display times in ms."""
        lay = self.maps.layout_of(map_name)
        blocks = self.maps.read_blocks(lay) & 0x3FF
        if region:
            x, y, w, h = region
            blocks = blocks[y:y + h, x:x + w]
        base = self.maps._compose(lay, self.maps.read_blocks(lay)[region[1]:region[1] + region[3],
                                                                   region[0]:region[0] + region[2]]
                                  if region else self.maps.read_blocks(lay), None, False, 1)
        sheet, info = self.preview(map_name, max_states)
        if sheet is None:
            return [base], [1000]
        arr = np.array(sheet)
        pos = {m: i for i, m in enumerate(info["metatiles"])}
        times = info["times"] + [info["cycle"]]
        # GIF delays are 1/100 s: fold states shown for under 2 game frames into the next one
        keep = [r for r in range(len(info["times"])) if times[r + 1] - times[r] >= 2] or [0]
        out = []
        for r in keep:
            img = np.array(base)
            for (yy, xx), mid in np.ndenumerate(blocks):
                i = pos.get(int(mid))
                if i is not None:
                    img[yy * 16:yy * 16 + 16, xx * 16:xx * 16 + 16] = arr[r * 16:r * 16 + 16, i * 16:i * 16 + 16]
            out.append(Image.fromarray(img, "RGBA"))
        starts = [times[r] for r in keep] + [info["cycle"]]
        starts[0] = 0
        ms = [max(20, round((starts[i + 1] - starts[i]) * 1000 / 60)) for i in range(len(out))]
        return out, ms

    def frames_image(self, anim_id: str, map_name: str | None = None, scale: int = 3, labels: bool = True) -> Image.Image:
        """Every distinct frame side by side, in the colours the map shows it in, with the play order under it."""
        a = self.get(anim_id)
        ctx_map = map_name or self._some_map(a)
        if ctx_map:
            _, _, pal, bank, _ = self._context(ctx_map)
        else:
            pal, bank = None, np.zeros(1024, np.int32)
        imgs = []
        for sym in a.frames:
            try:
                ft = self.frame_tiles(a, sym)
                png = load_png(self.root / a.pngs[sym])
                w = png.width // 8
            except (DecompError, OSError):
                imgs.append(Image.new("RGBA", (16, 16), (255, 0, 255, 255)))
                continue
            lay = Layout.sheet(len(ft), w)
            rgba = np.zeros((lay.height, lay.width, 4), np.uint8)
            dest = a.slots[0].dest if a.slots else 0
            gray = np.array([(i * 17, i * 17, i * 17) for i in range(16)], np.uint8)
            for k in range(len(ft)):
                b = int(bank[min(dest + k, 1023)])
                x, y = lay.tile_pos(k)
                rgba[y:y + 8, x:x + 8, :3] = (pal[b] if pal is not None else gray)[ft[k]]
                rgba[y:y + 8, x:x + 8, 3] = 255 if k < a.count else 90
            imgs.append(Image.fromarray(rgba, "RGBA"))
        pad = 6
        fw = max(max(i.width for i in imgs) * scale, 44 if labels else 0)
        fh = max(i.height for i in imgs) * scale
        top = 14 if labels else 0
        sheet = Image.new("RGBA", (len(imgs) * (fw + pad) + pad, fh + top + pad * 2 + (14 if labels else 0)),
                          (40, 40, 48, 255))
        d = ImageDraw.Draw(sheet)
        for i, im in enumerate(imgs):
            x = pad + i * (fw + pad)
            sheet.paste(im.resize((im.width * scale, im.height * scale), Image.NEAREST), (x, top + pad))
            if labels:
                d.text((x, 1), f"frame {i}", fill=(230, 230, 230, 255))
        if labels:
            order = " ".join("-" if s is None else str(s) for s in a.sequence)
            speed = f"every {a.period} game frames" if a.period else "custom timing"
            d.text((pad, sheet.height - 13), f"plays {order} · {speed}", fill=(200, 200, 120, 255))
        return sheet

    def _some_map(self, a: TileAnim) -> str | None:
        want = set(a.tilesets)
        for lay in self.maps.layouts.values():
            if lay.get("primary_tileset") in want or lay.get("secondary_tileset") in want:
                for n, m in self.maps.maps.items():
                    if m["layout"] == lay["id"]:
                        return n
        return None

    def animated_metatiles(self, map_name: str, anim_id: str) -> list[int]:
        a = self.get(anim_id)
        _, metas, _, _, _ = self._context(map_name)
        covered = np.zeros(1024, bool)
        for s in a.slots:
            covered[s.dest:s.dest + a.count] = True
        return [int(i) for i in np.nonzero(covered[metas & 0x3FF].any(axis=1))[0]]

    # ------------------------------------------------------------------ editing
    def _read(self) -> str:
        return self.path.read_text()

    def _write(self, text: str, rel: str = ANIM_FILE) -> None:
        self.dp._backup(rel)
        (self.root / rel).write_text(text)
        self.dp._changed = None
        self._cache = None

    def _table_span(self, text: str, table: str) -> tuple[int, int, int, int]:
        """(statement start, '{' + 1, '}', statement end) of a frame table in the raw text."""
        clean = strip_comments(text)
        m = re.search(rf"(?:static\s+)?const\s+u16\s*\*\s*const\s+{re.escape(table)}\s*\[\s*\]\s*=\s*\{{", clean)
        if not m:
            raise DecompError(f"frame table {table} not found in {ANIM_FILE}")
        close = _block_end(clean, m.end() - 1) - 1
        semi = clean.index(";", close) + 1
        return m.start(), m.end(), close, semi

    def set_sequence(self, anim_id: str, sequence: list[int | None]) -> TileAnim:
        """Set the play order, as indexes into the animation's frames (e.g. [0, 1, 0, 2]); None = NULL entry."""
        a = self.get(anim_id)
        if not sequence:
            raise DecompError("an animation needs at least one frame in its play order")
        for i in sequence:
            if i is not None and not (0 <= int(i) < len(a.frames)):
                raise DecompError(f"frame {i} does not exist (this animation has frames 0-{len(a.frames) - 1})")
        self._set_sequence_raw(a, sequence)
        for i, sym in enumerate(a.frames):
            if i not in sequence:
                self._drop_unused_frame(sym)
        return self.get(a.id)

    def _next_frame(self, a: TileAnim) -> tuple[str, str]:
        """(new frame symbol, new PNG path) following the animation's naming."""
        last = a.frames[-1]
        stem = re.sub(r"\d+$", "", last)
        nums = [int(m.group(1)) for s in a.frames if (m := re.search(r"(\d+)$", s))]
        n = max(nums, default=-1) + 1
        known = self.parse()["frame_pos"]
        while f"{stem}{n}" in known:
            n += 1
        png_dir = Path(a.pngs.get(last, "")).parent
        pn = n
        while (self.root / png_dir / f"{pn}.png").exists():
            pn += 1
        return f"{stem}{n}", str(png_dir / f"{pn}.png")

    def _palette_for(self, a: TileAnim, map_name: str | None):
        ctx = map_name or self._some_map(a)
        if not ctx:
            return None, None
        _, _, pal, bank, _ = self._context(ctx)
        return pal, bank

    def _convert(self, a: TileAnim, src, like_png: str, map_name: str | None) -> tuple[np.ndarray, list]:
        """Turn an image into frame indices (per tile, the colours of the palette its destination tile uses)."""
        like = load_png(self.root / like_png)
        with Image.open(src) as im:
            im.load()
            if im.size != (like.width, like.height):
                raise DecompError(f"the frame image must be {like.width}x{like.height} (the size of the other frames), "
                                  f"not {im.size[0]}x{im.size[1]}")
            if im.mode == "P" and max(im.getdata(), default=0) < 16:
                return np.array(im, np.uint8), like.palette or []
            rgba = np.array(im.convert("RGBA"), np.uint8)
        pal, bank = self._palette_for(a, map_name)
        if pal is None:
            idx, _ = remap_to_palette(rgba, pad_palette(like.palette or [], 16)[:16])
            return idx, like.palette or []
        out = np.zeros(rgba.shape[:2], np.uint8)
        w = like.width // 8
        dest = a.slots[0].dest if a.slots else 0
        for k in range((like.width // 8) * (like.height // 8)):
            ty, tx = (k // w) * 8, (k % w) * 8
            b = int(bank[min(dest + k, 1023)])
            block = rgba[ty:ty + 8, tx:tx + 8].copy()
            block[..., 3] = 255
            out[ty:ty + 8, tx:tx + 8], _ = remap_to_palette(block, [tuple(int(v) for v in c) for c in pal[b]])
        return out, like.palette or [tuple(int(v) for v in c) for c in pal[int(bank[dest])]]

    def _save_png(self, rel: str, idx: np.ndarray, palette: list) -> None:
        self.dp._backup(rel)
        (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
        plte = pad_palette(list(palette) or [(i * 17, i * 17, i * 17) for i in range(16)], 16)
        im = Image.frombytes("P", (idx.shape[1], idx.shape[0]), np.ascontiguousarray(idx, np.uint8).tobytes())
        im.putpalette([v for c in plte for v in c[:3]])
        im.save(self.root / rel, format="PNG", bits=4 if len(plte) <= 16 else 8)
        self.dp._changed = None

    def set_frame(self, anim_id: str, frame: int, src, map_name: str | None = None) -> list[str]:
        """Replace a frame's art with an image (same size as the frame)."""
        a = self.get(anim_id)
        if not 0 <= frame < len(a.frames):
            raise DecompError(f"frame {frame} does not exist (this animation has frames 0-{len(a.frames) - 1})")
        png = a.pngs.get(a.frames[frame])
        if not png:
            raise DecompError(f"frame {frame} has no PNG")
        if a.frames[frame] in a.parts:
            raise DecompError(f"frame {frame} is built from {len(a.parts[a.frames[frame]])} images "
                              f"({', '.join(a.parts[a.frames[frame]])}); edit those in the graphics editor")
        idx, palette = self._convert(a, src, png, map_name)
        self._save_png(png, idx, palette)
        users = [t for t, (entries, _, _) in self.parse()["tables"].items() if a.frames[frame] in entries and t != a.table]
        return [f"{png} is also used by {', '.join(users)}"] if users else []

    def add_frame(self, anim_id: str, src=None, copy_of: int | None = None, position: int | None = None,
                  map_name: str | None = None) -> dict:
        """Add a new frame (a copy of frame ``copy_of``, default the last one, or an image) and put it in the play
        order at ``position`` (default: the end)."""
        a = self.get(anim_id)
        if not a.frames:
            raise DecompError("this animation has no frames to copy the size from")
        if a.parts:
            raise DecompError(f"{a.id}'s frames are built from several images each; add frames by hand")
        base = a.frames[copy_of if copy_of is not None else -1]
        like = a.pngs.get(base)
        if not like or not (self.root / like).exists():
            raise DecompError(f"frame {base}: PNG not found")
        sym, png = self._next_frame(a)
        if src is not None:
            idx, palette = self._convert(a, src, like, map_name)
        else:
            img = load_png(self.root / like)
            idx, palette = img.indices, img.palette
        self._save_png(png, idx, palette)
        text = self._read()
        clean = strip_comments(text)
        last_decl = max((m for m in FRAME_RE.finditer(clean) if m.group(1) in a.frames), key=lambda m: m.end())
        static = "static " if clean[last_decl.start():last_decl.end()].lstrip().startswith("static") else ""
        decl = "\n" + self._frame_decl(sym, png, static, clean)
        text = text[:last_decl.end()] + decl + text[last_decl.end():]
        self._write(text)
        seq = list(a.sequence)
        new_index = len(a.frames)
        seq.insert(len(seq) if position is None else max(0, min(position, len(seq))), new_index)
        self._cache = None
        fresh = self.get(a.id)
        fresh.frames.append(sym)
        fresh.pngs[sym] = png
        self._set_sequence_raw(fresh, seq)
        return {"frame": new_index, "symbol": sym, "png": png, "anim": self.get(a.id).as_dict()}

    def _set_sequence_raw(self, a: TileAnim, sequence: list[int | None]) -> None:
        text = self._read()
        _, body_start, close, _ = self._table_span(text, a.table)
        entries = ",\n".join("    " + ("NULL" if i is None else a.frames[int(i)]) for i in sequence)
        self._write(text[:body_start] + "\n" + entries + "\n" + text[close:])

    def remove_frame(self, anim_id: str, frame: int) -> list[str]:
        """Take a frame out of the play order (and its declaration, if nothing else uses it)."""
        a = self.get(anim_id)
        if not 0 <= frame < len(a.frames):
            raise DecompError(f"frame {frame} does not exist (this animation has frames 0-{len(a.frames) - 1})")
        seq = [i for i in a.sequence if i != frame]
        if not [i for i in seq if i is not None]:
            raise DecompError("can't remove the last frame of an animation")
        self._set_sequence_raw(a, seq)
        png = self._drop_unused_frame(a.frames[frame])
        return [f"{png} is no longer used (the file was kept)"] if png else []

    def _drop_unused_frame(self, sym: str) -> str | None:
        """Delete a frame's declaration once no table uses it; returns its PNG."""
        data = self.parse()
        if any(sym in entries for entries, _, _ in data["tables"].values()):
            return None
        text = self._read()
        clean = strip_comments(text)
        m = next((m for m in FRAME_RE.finditer(clean) if m.group(1) == sym), None)
        if not m:
            return None
        start = clean.rfind("\n", 0, m.start()) + 1
        end = m.end() + (1 if clean[m.end():m.end() + 1] == "\n" else 0)
        self._write(text[:start] + text[end:])
        files = re.findall(r'"([^"]+)"', m.group(3))
        return files[0] if files else None

    def set_speed(self, anim_id: str, period: int, phase: int | None = None) -> TileAnim:
        """Show each frame for ``period`` game frames (60 = one second)."""
        a = self.get(anim_id)
        if a.staggered or a.period is None or not a.callback:
            raise DecompError(f"{a.id} uses custom timing; edit {a.callback or a.queue} in {ANIM_FILE} by hand")
        period = int(period)
        if not 1 <= period <= 255:
            raise DecompError("the speed must be between 1 and 255 game frames per animation frame")
        text = self._read()
        clean = strip_comments(text)
        f = self.functions(clean).get(a.callback)
        if f is None:
            raise DecompError(f"callback {a.callback} not found")
        body0 = f["body_start"]
        body = f["body"]
        cm = next((m for m in CALL_RE.finditer(body) if m.group(1) == a.queue), None)
        if cm is None:
            raise DecompError(f"{a.callback} does not call {a.queue}")
        old_phase = a.slots[0].phase if a.slots else 0
        new_phase = (old_phase if phase is None else int(phase)) % period
        call = f"{a.queue}(timer / {period});" if period > 1 else f"{a.queue}(timer);"
        guard = None
        for im in IF_RE.finditer(body):
            k = im.end()
            while body[k].isspace():
                k += 1
            stop = _block_end(body, k) if body[k] == "{" else body.index(";", k) + 1
            if im.start() <= cm.start() < stop:
                guard = (im, k, stop)
        var = guard[0].group(1) if guard else "timer"
        call = call.replace("timer", var)
        new_if = f"if ({var} % {period} == {new_phase})" if period > 1 else None
        others = guard and len([m for m in CALL_RE.finditer(body[guard[1]:guard[2]])]) > 1
        if guard and not others:
            im, k, stop = guard
            stmt = f"{new_if}\n        {call}" if new_if else call
            indent = body[body.rfind("\n", 0, im.start()) + 1:im.start()]
            stmt = stmt.replace("\n        ", "\n" + indent + "    ")
            new_text = text[:body0 + im.start()] + stmt + text[body0 + stop:]
        else:
            # take the call out of its shared block (or unguarded spot) and give it its own if at the end
            raw_body = text[body0:f["end"] - 1]
            line_start = raw_body.rfind("\n", 0, cm.start()) + 1
            line_end = raw_body.find("\n", cm.end())
            line_end = len(raw_body) if line_end < 0 else line_end + 1
            stmt = f"    {new_if}\n        {call}\n" if new_if else f"    {call}\n"
            new_body = (raw_body[:line_start] + raw_body[line_end:]).rstrip() + "\n" + stmt
            new_text = text[:body0] + new_body + text[f["end"] - 1:]
        self._write(new_text)
        return self.get(a.id)

    def create(self, map_name: str, name: str, tile: int, count: int, frames: int = 2, period: int = 16,
               frame_files: list | None = None) -> dict:
        """Animate ``count`` tiles from global tile id ``tile`` (as the metatile editor shows them) on the map's
        tilesets. The frames start as copies of the current tiles (or ``frame_files``), ready to be redrawn."""
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", name or ""):
            raise DecompError("animation names may use letters, digits and _ (e.g. PondWater)")
        if not 1 <= count <= 128:
            raise DecompError("an animation copies 1-128 tiles")
        if not 1 <= period <= 255:
            raise DecompError("the speed must be between 1 and 255 game frames per animation frame")
        if frame_files:
            frames = len(frame_files)
        if not 1 <= frames <= 64:
            raise DecompError("an animation has 1-64 frames")
        if not self.path.exists():
            raise DecompError(f"{ANIM_FILE} not found")
        prim_s, sec_s, version = self._layout_tilesets(map_name)
        _, npt, _ = self.maps.consts(version)
        secondary = tile >= npt
        ts_sym = sec_s if secondary else prim_s
        ts = self.maps.tilesets.get(ts_sym)
        if ts is None or not ts.tiles:
            raise DecompError(f"tileset {ts_sym} has no tiles file")
        rel = tile - npt if secondary else tile
        limit = (1024 - npt) if secondary else npt
        if rel < 0 or rel + count > limit:
            raise DecompError(f"tiles {tile}-{tile + count - 1} are outside {ts_sym}")
        data = self.parse()
        camel = _camel(name)
        ts_name = re.sub(r"^gTileset_", "", ts_sym)
        base = f"{ts_name}_{camel}"
        table = f"gTilesetAnims_{base}"
        queue = f"QueueAnimTiles_{base}"
        if table in data["tables"] or queue in self.functions(strip_comments(self._read())):
            raise DecompError(f"an animation called {base} already exists")
        png_dir = Path(ts.tiles).parent / "anim" / _snake(camel)
        tiles0, _, _, _, _ = self._context(map_name)
        cur = tiles0[tile:tile + count]
        w = max(d for d in range(1, min(count, 16) + 1) if count % d == 0)
        src_png = load_png(self.root / ts.tiles)
        palette = src_png.palette or []
        pngs = []
        for i in range(frames):
            rel_png = str(png_dir / f"{i}.png")
            if (self.root / rel_png).exists():
                raise DecompError(f"{rel_png} already exists")
            pngs.append(rel_png)
        for i, rel_png in enumerate(pngs):
            self._save_png(rel_png, tiles_to_image(cur, Layout.sheet(count, w)), palette)
        stub = TileAnim(id=base, table=table, queue=queue, frames=[f"{table}_Frame{i}" for i in range(frames)],
                        pngs={f"{table}_Frame{i}": p for i, p in enumerate(pngs)}, sequence=list(range(frames)),
                        count=count, slots=[Slot(tile, 0)], period=period)
        if frame_files:
            for i, f in enumerate(frame_files):
                idx, pal = self._convert(stub, f, pngs[i], map_name)
                self._save_png(pngs[i], idx, pal)

        dest = f"NUM_TILES_IN_PRIMARY + {rel}" if secondary else str(rel)
        if version == "frlg" and secondary:
            dest = f"NUM_TILES_IN_PRIMARY_FRLG + {rel}" if "NUM_TILES_IN_PRIMARY_FRLG" in self.dp.defines else str(tile)
        clean = strip_comments(self._read())
        code = "\n".join(self._frame_decl(f"{table}_Frame{i}", p, "", clean) for i, p in enumerate(pngs))
        code += f"\n\nconst u16 *const {table}[] = {{\n" + ",\n".join(f"    {table}_Frame{i}" for i in range(frames))
        code += "\n};\n\n"
        code += (f"static void {queue}(u16 timer)\n{{\n    u16 i = timer % ARRAY_COUNT({table});\n"
                 f"    AppendTilesetAnimToBuffer({table}[i], (u16 *)(BG_VRAM + TILE_OFFSET_4BPP({dest})), "
                 f"{count} * TILE_SIZE_4BPP);\n}}\n")
        notes = []
        text = self._read()
        init = data["tilesets"].get(ts_sym)
        info = data["inits"].get(init) if init else None
        side = "Secondary" if secondary else "Primary"
        used_phases = set()
        if info and info["callback"]:
            cb = info["callback"]
            for t in self._triggers_of(cb):
                used_phases.add(t)
        phase = next((p for p in range(period) if p not in used_phases), 0)
        trigger = f"    if (timer % {period} == {phase})\n        {queue}(timer / {period});\n" if period > 1 \
            else f"    {queue}(timer);\n"
        proto = f"static void {queue}(u16);\n"
        if info and info["callback"]:
            # add to the tileset's existing callback
            cb = info["callback"]
            text = self._insert_prototype(text, proto)
            clean = strip_comments(text)
            f = self.functions(clean)[cb]
            close = f["end"] - 1
            text = text[:close].rstrip() + "\n" + trigger + text[close:]
            text = text.rstrip() + "\n\n" + code
            notes.append(f"added to {cb} (runs on {ts_sym})")
        else:
            cb = f"TilesetAnim_{ts_name}"
            if cb in self.functions(strip_comments(text)):
                raise DecompError(f"{cb} already exists but isn't {ts_sym}'s callback; hook it up by hand")
            text = self._insert_prototype(text, proto + f"static void {cb}(u16);\n")
            text = text.rstrip() + "\n\n" + code + f"\nstatic void {cb}(u16 timer)\n{{\n{trigger}}}\n"
            if info:
                # an Init function exists with a NULL callback (e.g. InitTilesetAnim_Petalburg)
                clean = strip_comments(text)
                f = self.functions(clean)[init]
                body = text[f["body_start"]:f["end"] - 1]
                body = re.sub(rf"s{side}TilesetAnimCallback\s*=\s*NULL\s*;", f"s{side}TilesetAnimCallback = {cb};", body)
                text = text[:f["body_start"]] + body + text[f["end"] - 1:]
                notes.append(f"{init} now runs {cb}")
            else:
                init = f"InitTilesetAnim_{ts_name}"
                text = text.rstrip() + (f"\n\nvoid {init}(void)\n{{\n    s{side}TilesetAnimCounter = 0;\n"
                                        f"    s{side}TilesetAnimCounterMax = 256;\n"
                                        f"    s{side}TilesetAnimCallback = {cb};\n}}\n")
                self._hook_tileset(ts_sym, init)
                notes.append(f"{ts_sym} now animates through the new {init}")
        self._write(text)
        anim = self.get(base)
        cmax = self.counter_max(map_name, anim)
        if cmax and cmax % period:
            notes.append(f"the counter wraps at {cmax}, which is not a multiple of {period}: the animation will "
                         "skip a little once per cycle")
        return {"anim": anim.as_dict(), "notes": notes, "pngs": pngs}

    @staticmethod
    def _frame_decl(sym: str, png: str, static: str, text: str) -> str:
        """A frame's declaration, in the style the file already uses (INCGFX_U16 from the PNG, or INCBIN_U16 of the
        .4bpp that older trees build from it)."""
        styles = {m.group(2) for m in FRAME_RE.finditer(text)}
        if "BIN" in styles and "GFX" not in styles:
            return f'{static}const u16 {sym}[] = INCBIN_U16("{re.sub(r"[.]png$", "", png)}.4bpp");'
        return f'{static}const u16 {sym}[] = INCGFX_U16("{png}", ".4bpp");'

    def retarget(self, anim_id: str, tile: int, count: int, version: str) -> TileAnim:
        """Point an animation made by ``create`` at other tiles (``tile``, global id) and another tile count."""
        a = self.get(anim_id)
        text = self._read()
        f = self.functions(strip_comments(text)).get(a.queue)
        if f is None or len(a.slots) != 1:
            raise DecompError(f"{a.id} isn't a simple animation (one copy); change it by hand")
        _, npt, _ = self.maps.consts(version)
        dest = f"NUM_TILES_IN_PRIMARY + {tile - npt}" if tile >= npt else str(tile)
        body = text[f["body_start"]:f["end"]]
        new = re.sub(r"TILE_OFFSET_4BPP\([^)]*\)", f"TILE_OFFSET_4BPP({dest})", body, count=1)
        new = re.sub(r"\b\d+\s*\*\s*TILE_SIZE_4BPP", f"{count} * TILE_SIZE_4BPP", new, count=1)
        if new == body:
            raise DecompError(f"couldn't find where {a.queue} copies its tiles")
        self._write(text[:f["body_start"]] + new + text[f["end"]:])
        return self.get(anim_id)

    def _triggers_of(self, callback: str) -> list[int]:
        f = self.functions(strip_comments(self._read())).get(callback)
        return [int(m.group(3)) for m in IF_RE.finditer(f["body"])] if f else []

    def _insert_prototype(self, text: str, proto: str) -> str:
        clean = strip_comments(text)
        protos = list(re.finditer(r"^static void \w+\([^)]*\);\s*$", clean, re.M))
        if protos:
            at = protos[-1].end() + 1
        else:
            incs = list(re.finditer(r"^#include.*$", clean, re.M))
            at = incs[-1].end() + 1 if incs else 0
            proto = "\n" + proto
        return text[:at] + proto + text[at:]

    def _hook_tileset(self, ts_sym: str, init: str) -> None:
        hp = self.root / HEADERS_FILE
        text = hp.read_text()
        m = re.search(rf"struct Tileset\s+{re.escape(ts_sym)}\s*=\s*\{{(.*?)\}};", text, re.S)
        if not m:
            raise DecompError(f"{ts_sym} not found in {HEADERS_FILE}")
        body = m.group(1)
        if re.search(r"\.callback\s*=", body):
            body = re.sub(r"\.callback\s*=\s*\w+", f".callback = {init}", body)
        else:
            body = body.rstrip() + f"\n    .callback = {init},\n"
        self.dp._backup(HEADERS_FILE)
        hp.write_text(text[:m.start(1)] + body + text[m.end(1):])
        header = self.root / ANIM_HEADER
        if header.exists():
            h = header.read_text()
            if f"void {init}(void);" not in h:
                h = re.sub(r"(\n#endif[^\n]*\s*)$", f"\nvoid {init}(void);\\1", h, count=1) if "#endif" in h \
                    else h + f"void {init}(void);\n"
                self.dp._backup(ANIM_HEADER)
                header.write_text(h)
        self.dp._changed = None

    # ------------------------------------------------------------------ validation
    def check(self, only_changed: bool = True) -> list[tuple[str, str, str]]:
        """Problems with tileset animations (by default only ones new or changed since the last commit):
        (level, file, message)."""
        try:
            data = self.parse()
        except DecompError as e:
            return [("error", ANIM_FILE, str(e))]
        touched = set(data["anims"])
        if only_changed:
            changed = self.dp.changed_files()
            if not any(f in changed for f in (ANIM_FILE, HEADERS_FILE)) and \
                    not any(f in changed for a in data["anims"].values() for f in a.pngs.values()):
                return []
            try:
                before = {a.table: a for a in self.original()["anims"].values()}
            except DecompError:
                before = {}
            key = lambda a: {k: v for k, v in a.as_dict().items() if k not in ("line", "notes")}
            touched = {i for i, a in data["anims"].items()
                       if a.table not in before or key(before[a.table]) != key(a)
                       or any(f in changed for f in [*a.pngs.values(), *(x for v in a.parts.values() for x in v)])}
        issues = []
        maps_by_ts: dict[str, str] = {}
        for n, m in self.maps.maps.items():
            lay = self.maps.layouts.get(m["layout"])
            if lay:
                for k in ("primary_tileset", "secondary_tileset"):
                    maps_by_ts.setdefault(lay[k], n)
        for a in (data["anims"][i] for i in touched):
            where = f"{ANIM_FILE}:{a.line}"
            for sym in a.frames:
                png = a.pngs.get(sym)
                if png is None:
                    issues.append(("error", where, f"{a.id}: frame {sym} is not declared with INCGFX_U16"))
                    continue
                missing = [f for f in a.parts.get(sym, [png]) if not (self.root / f).exists()]
                if missing:
                    issues.append(("error", missing[0], f"{a.id}: frame image missing"))
                    continue
                with Image.open(self.root / png) as im:
                    if im.mode != "P":
                        issues.append(("warning", png, f"{a.id}: not an indexed PNG (run `pae fix`)"))
                n_tiles = len(self.frame_tiles(a, sym))
                if n_tiles < a.count:
                    issues.append(("error", png, f"{a.id}: has {n_tiles} tiles but {a.count} are copied each frame "
                                                 "(the game would copy garbage)"))
            for sl in a.slots:
                if sl.dest + a.count > 1024:
                    issues.append(("error", where, f"{a.id}: tiles {sl.dest}-{sl.dest + a.count - 1} are past the end "
                                                   "of the background tile memory"))
            if a.callback and not a.inits:
                issues.append(("warning", where, f"{a.id}: {a.callback} is not used by any InitTilesetAnim_ function"))
            elif a.inits and not a.tilesets:
                issues.append(("warning", where, f"{a.id}: no tileset uses {', '.join(a.inits)}"))
            m = next((maps_by_ts[t] for t in a.tilesets if t in maps_by_ts), None)
            if m and a.period:
                cmax = self.counter_max(m, a)
                if cmax == 0:
                    issues.append(("error", where, f"{a.id}: the animation counter never advances (its primary "
                                                   "tileset has no animation; set the counter max in "
                                                   f"{', '.join(a.inits)})"))
                elif a.period > cmax:
                    issues.append(("warning", where, f"{a.id}: speed {a.period} is slower than the counter cycle "
                                                     f"({cmax}), so only frame 0 is shown"))
                elif not a.staggered and a.sequence and (cmax // a.period) % len(a.sequence):
                    issues.append(("warning", where, f"{a.id}: {len(a.sequence)} frames don't divide the "
                                                     f"{cmax // a.period} steps per counter cycle; the animation "
                                                     "jumps back to frame 0 once per cycle"))
        # more than DMA_SLOTS copies on one game frame are silently dropped
        for init, info in data["inits"].items():
            cb = info["callback"]
            anims = [a for a in data["anims"].values() if cb and a.callback == cb]
            if not anims or not any(a.id in touched for a in anims):
                continue
            cmax = info["counter_max"] if isinstance(info["counter_max"], int) else 256
            for c in range(min(cmax, 512)):
                n = sum(1 for a in anims if a.period for sl in a.slots if c % a.period == sl.phase)
                if n > DMA_SLOTS // 2:
                    issues.append(("warning", ANIM_FILE, f"{cb}: {n} tile copies on the same game frame ({c}); "
                                                         f"with the primary tileset's, more than {DMA_SLOTS} are "
                                                         "dropped. Use different phases"))
                    break
        return issues

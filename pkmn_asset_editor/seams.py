"""Connection seams: neighbouring maps drawn right when they use other tilesets.

When two connected maps use different tilesets, the strip of the neighbour the game shows at a map's edge (7 blocks
deep) is drawn with *this* map's tiles, which shows garbage. Forks with a ``kanto_connection_seam.c`` (this repo's
FireRed Kanto/Johto hack) can load extra "adapter" metatiles for that strip: the neighbour's tiles copied into free
VRAM tile slots, its palettes into free palette slots, and metatiles at the top of the metatile range that the
strip's blocks are translated to.

``build(maps, map_name)`` records that ``map_name`` and its neighbours need seams for each other (in
``src/data/pae_seams.json``) and regenerates ``src/data/pae_seams.h`` from that record, which
``kanto_connection_seam.c`` includes (added once).

A neighbour's palette with the same colours as one the map shows uses the map's own, and when both maps have the
same primary tileset its tiles are used as they are (only the secondary tiles are copied): maps built from one
kit need few slots. Copied tiles take their colours wherever a slot has them, so the colours several of the
neighbour's palettes use in the strip share a slot when they fit in one (15 colours). When no slot has room for a
copied palette's colours, its tiles take each colour's nearest in the slot that has them closest (the strip is
drawn a little off until the neighbour is rebuilt; ``build`` says which). Free slots: first those the map's tilesets never use. When those run out, palette slots and secondary tiles the map
never shows are borrowed: the game reloads every palette and the secondary tiles whenever you cross into another map
(primary tiles are not reloaded when the primary tileset stays, so those are only taken when the tileset doesn't
use them). Adapters go at the top of the metatile ids, above the map's tilesets' metatiles; a map whose tilesets
fill every id lends them the top ids it never shows (one it shows between them stays its own). "Shown" is the map's
blocks, its border, what its scripts put with ``setmetatile`` and the strips of neighbours with the same tilesets.
"""

from __future__ import annotations

import json

import numpy as np

from .decomp import DecompError

DEPTH = 7  # MAP_OFFSET: how far into a neighbour the game draws
RECORD = "src/data/pae_seams.json"
HEADER = "src/data/pae_seams.h"
SEAM_C = "src/kanto_connection_seam.c"
VERSIONS = {"emerald": "LAYOUT_VERSION_EMERALD", "frlg": "LAYOUT_VERSION_FRLG", "hns": "LAYOUT_VERSION_HNS"}


def _layout_symbol(maps, name: str) -> str:
    """The MapLayout the game uses for this map (a map with its own geometry gets <Map>_FireRedGeometry)."""
    data = maps.get_map(name)
    if isinstance(data.get("firered_geometry"), dict):
        return f"{name}_FireRedGeometry"
    return maps.layouts[data["layout"]]["name"]


def _context(maps, name: str):
    lay = maps.layout_of(name)
    te = maps.tileset_editor
    prim, sec, version = te.sides(lay)
    return lay, te, prim, sec, version


def _same_tilesets(a: dict, b: dict) -> bool:
    return (a["primary_tileset"], a["secondary_tileset"], a.get("layout_version", "emerald")) == \
        (b["primary_tileset"], b["secondary_tileset"], b.get("layout_version", "emerald"))


def strip(maps, target: str, source: str) -> set[int]:
    """Metatile ids of ``source`` in the strip ``target`` shows of it (empty if they aren't connected)."""
    tl, sl = maps.layout_of(target), maps.layout_of(source)
    blocks = maps.read_blocks(sl) & 0x3FF
    sid = maps.get_map(source)["id"]
    out: set[int] = set()
    for c in maps.get_map(target).get("connections") or []:
        if c.get("map") != sid:
            continue
        d, off = c["direction"], int(c.get("offset", 0))
        sh, sw = blocks.shape
        if d in ("up", "down"):
            xs = [x - off for x in range(-DEPTH, tl["width"] + DEPTH + 1) if 0 <= x - off < sw]
            ys = range(sh - DEPTH, sh) if d == "up" else range(0, DEPTH)
        elif d in ("left", "right"):
            ys = [y - off for y in range(-DEPTH, tl["height"] + DEPTH + 1) if 0 <= y - off < sh]
            xs = range(sw - DEPTH, sw) if d == "left" else range(0, DEPTH)
        else:
            continue
        for y in ys:
            for x in xs:
                if 0 <= y < sh and 0 <= x < sw:
                    out.add(int(blocks[y, x]))
    return out


def _used(maps, name: str) -> tuple[set[int], set[int], dict]:
    """(tile slots, palette banks) the map's own tilesets use, and info."""
    lay, te, prim, sec, version = _context(maps, name)
    npm, npt, npp = maps.consts(version)
    tiles = set(range(0, te._tile_count(prim))) | set(range(npt, npt + te._tile_count(sec)))
    tiles |= set(range(434, 450)) | set(range(1008, 1024))  # door animation slots (HnS, and FRLG/Emerald)
    tiles.add(0)
    banks = set()
    for side in (prim, sec):
        for row in te._read_metas(side):
            for v in row:
                banks.add(int(v) >> 12)
    return tiles, banks, {"npm": npm, "npt": npt, "npp": npp, "version": version,
                          "metatiles": sec.metatile_base + len(te._read_metas(sec))}


def _shown(maps, name: str) -> set[int] | None:
    """Metatile ids the map can show: its blocks, its border, what its scripts set, and the strips of neighbours with
    the same tilesets (drawn with its metatiles as they are); None if that can't be told."""
    lay = maps.layout_of(name)
    mids = set(np.unique(maps.read_blocks(lay) & 0x3FF).tolist())
    for c in maps.get_map(name).get("connections") or []:
        other = maps.map_by_id.get(c.get("map"))
        if other is not None and _same_tilesets(lay, maps.layout_of(other)):
            mids |= strip(maps, name, other)
    try:
        mids |= set(np.unique(maps.read_blocks(lay, "border") & 0x3FF).tolist())
    except Exception:
        return None
    import re
    for m in re.finditer(r"^\s*setmetatile\s+[^,]+,\s*[^,]+,\s*(\w+)", maps.read_script(name), re.M):
        v = m.group(1)
        v = maps.dp.defines.get(v, v)
        try:
            mids.add(int(str(v), 0) & 0x3FF)
        except ValueError:
            return None
    return mids


def _spare(maps, name: str, info: dict) -> tuple[list[int], list[int]]:
    """Secondary tile slots and palette slots the map never shows (borrowable; see the module notes)."""
    shown = _shown(maps, name)
    if shown is None:
        return [], []
    te = maps.tileset_editor
    tiles, banks = set(), set()
    for mid in shown:
        try:
            mt = te.get_metatile(name, mid)
        except Exception:
            continue
        for e in mt["entries"]:
            tiles.add(e["tile"])
            banks.add(e["palette"])
    from .tileset_anims import TilesetAnims
    try:
        for a in TilesetAnims(maps).for_map(name):
            for sl in a.slots:
                tiles |= set(range(sl.dest, sl.dest + a.count))
    except DecompError:
        pass
    tiles |= set(range(434, 450)) | set(range(1008, 1024))
    spare_tiles = [t for t in range(1023, info["npt"] - 1, -1) if t not in tiles]
    return spare_tiles, [b for b in range(13) if b not in banks]


def _pack(need: set[int], cols, own: dict, bank_colors: dict, packed: dict, free_banks: list) -> int | None:
    """A palette slot for a bank whose tiles are copied, with its colours (``need``: its indices in ``cols``) on any
    indices: one that has them all already (the map's own, or one filled before), else one filled colour by colour
    with room for the ones it lacks, else a new one filled so (from ``free_banks``). None if there's no room."""
    want = list(dict.fromkeys(tuple(cols[i]) for i in sorted(need)))
    for slot, have in list(own.items()) + list(bank_colors.items()):
        n = packed.get(slot, 15)  # a packed slot's colours past its last aren't set yet
        if all(c in [tuple(h) for h in have[1:n + 1]] for c in want):
            return slot
    for slot, n in sorted(packed.items()):
        have = [tuple(h) for h in bank_colors[slot][1:n + 1]]
        lack = [c for c in want if c not in have]
        if n + len(lack) <= 15:
            bank_colors[slot][n + 1:n + 1 + len(lack)] = lack
            packed[slot] = n + len(lack)
            return slot
    if not free_banks:
        return None
    slot = free_banks.pop(0)
    bank_colors[slot] = [(0, 0, 0)] + want + [(0, 0, 0)] * (15 - len(want))
    packed[slot] = len(want)
    return slot


def _nearest(need: set[int], cols, own: dict, bank_colors: dict, packed: dict) -> tuple[int, dict, int] | None:
    """The slot whose colours are closest to a copied bank's (``need``: its indices in ``cols``), for when no slot
    has room for them: (slot, {index: its nearest's index in the slot}, the farthest any colour is, in RGB steps)."""
    best = None
    for slot, have in list(own.items()) + list(bank_colors.items()):
        n = packed.get(slot, 15)
        if n < 1:
            continue
        cand = range(1, n + 1)
        to = {i: min(cand, key=lambda j: sum((int(a) - int(b)) ** 2 for a, b in zip(cols[i], have[j])))
              for i in need}
        d = [sum((int(a) - int(b)) ** 2 for a, b in zip(cols[i], have[j])) for i, j in to.items()]
        if best is None or sum(d) < best[0]:
            best = (sum(d), slot, to, round(max(d) ** 0.5))
    return None if best is None else best[1:]


def plan(maps, target: str, sources: list[str], beside: dict | None = None) -> dict:
    """Adapters ``target`` needs to draw the strips of ``sources``; ``beside``: what a hand-made seam of the map
    already takes (see ``hand_made``)."""
    used_tiles, used_banks, info = _used(maps, target)
    if beside:
        used_tiles = used_tiles | beside["tiles"]
        used_banks = used_banks | beside["banks"]
    free_tiles = [t for t in range(1023, 0, -1) if t not in used_tiles]
    free_banks = [b for b in range(13) if b not in used_banks]
    spare_tiles, spare_banks = _spare(maps, target, info)
    if beside:
        spare_tiles = [t for t in spare_tiles if t not in beside["tiles"]]
        spare_banks = [b for b in spare_banks if b not in beside["banks"]]
    strict_tiles, strict_banks = set(free_tiles), set(free_banks)
    free_tiles += [t for t in spare_tiles if t not in strict_tiles]
    free_banks += [b for b in spare_banks if b not in strict_banks]
    metas, glyphs = [], {}  # glyph pixels bytes -> slot
    approx = []  # copied palettes drawn in their nearest colours (no room for their own)
    unknown = []  # blocks of a strip whose metatiles aren't in the neighbour's tilesets (not copied)
    bank_colors: dict[int, list] = {}  # slot -> colours loaded there
    packed: dict[int, int] = {}  # slots filled colour by colour (_pack) -> how many colours they have
    # the map's own palettes (the banks it shows): a neighbour's palette with the same colours just uses them
    _, tte, tprim, _, _ = _context(maps, target)
    tpal = tte.palettes(target)
    own = {b: list(tpal[b]) for b in sorted(used_banks - set(spare_banks) - (beside["banks"] if beside else set()))
           if b < len(tpal)}
    for src in sources:
        mids = sorted(strip(maps, target, src))
        # a neighbour's block fixes can put its own hand-made seam's adapters on the map (ids above its tilesets'
        # metatiles, drawn from kanto_connection_seam.c): they aren't copied, so they show as they did before
        have = _used(maps, src)[2]["metatiles"]
        if any(mid >= have for mid in mids):
            unknown.append({"source": src, "ids": [mid for mid in mids if mid >= have]})
            mids = [mid for mid in mids if mid < have]
        if not mids:
            continue
        slay, ste, sprim, ssec, sver = _context(maps, src)
        spal = ste.palettes(src)
        stiles = {}
        for side in (sprim, ssec):
            arr, _ = ste.read_tiles(side)
            stiles[side.tile_base] = (arr, ste._tile_count(side))
        snpm, snpt, _ = maps.consts(sver)
        same_primary = sprim.symbol == tprim.symbol and sver == info["version"]

        def pixels(t: int) -> np.ndarray:
            base = 0 if t < snpt else snpt
            arr, n = stiles[base]
            return arr[t - base] if t - base < min(n, len(arr)) else np.zeros((8, 8), np.uint8)
        # which colour indices each bank needs, to share palette slots where the colours agree. A bank some of whose
        # tiles are used as they are (the same primary tileset) keeps its colours on their indices ("fixed"); the
        # others' tiles are copied, so their pixels can take the colours wherever a slot has them
        need: dict[int, set[int]] = {}
        fixed: set[int] = set()
        rows = {}
        for mid in mids:
            mt = ste.get_metatile(src, mid)
            rows[mid] = mt
            for e in mt["entries"]:
                px = pixels(e["tile"])
                if px.any():
                    need.setdefault(e["palette"], set()).update(int(v) for v in np.unique(px) if v)
                    if same_primary and e["tile"] < snpt:
                        fixed.add(e["palette"])
        slot_of: dict[int, int] = {}
        lut: dict[int, np.ndarray] = {}  # source bank -> its colour indices in its slot
        for bank in sorted(need, key=lambda b: (b not in fixed, -len(need[b]))):
            cols = spal[bank]
            lut[bank] = np.arange(16, dtype=np.uint8)
            slots = list(own.items()) + list(bank_colors.items())
            if same_primary and bank in own:  # the same bank first: then its metatiles may need no adapter at all
                slots.insert(0, (bank, own[bank]))
            for slot, have in slots:  # the same colours on these indices
                if all(i <= packed.get(slot, 15) and have[i] == cols[i] for i in need[bank]):
                    slot_of[bank] = slot
                    break
            else:
                if bank not in fixed:
                    slot = _pack(need[bank], cols, own, bank_colors, packed, free_banks)
                    if slot is not None:
                        have = own[slot] if slot in own else bank_colors[slot]
                        for i in need[bank]:
                            lut[bank][i] = next(j for j in range(1, 16) if have[j] == cols[i])
                        slot_of[bank] = slot
                        continue
                    near = _nearest(need[bank], cols, own, bank_colors, packed)
                    if near is not None:
                        slot, to, far = near
                        for i, j in to.items():
                            lut[bank][i] = j
                        slot_of[bank] = slot
                        approx.append({"source": src, "palette": bank, "slot": slot, "colours": len(to),
                                       "farthest": far})
                        continue
                if not free_banks:
                    raise DecompError(f"{target}: no free palette slot left for {src}'s palette {bank} "
                                      f"(the strip needs {len(need)} palettes; the map shows the others)")
                slot = free_banks.pop(0)
                bank_colors[slot] = list(cols)
                slot_of[bank] = slot
        for mid in mids:
            mt = rows[mid]
            if same_primary and mid < snpm and all(
                    not pixels(e["tile"]).any() or (e["tile"] < snpt and slot_of[e["palette"]] == e["palette"])
                    for e in mt["entries"]):
                # one of the primary's metatiles, drawn from the primary's tiles in palettes the map has on the same
                # banks: the map draws it as it is (a block with no adapter is kept), so it needs none
                continue
            ent = []
            for e in mt["entries"]:
                px = pixels(e["tile"])
                if not px.any():
                    ent.append(0)
                    continue
                if same_primary and e["tile"] < snpt:  # the same primary tileset: its tiles are loaded already
                    ent.append(e["tile"] | (int(e["hflip"]) << 10) | (int(e["vflip"]) << 11)
                               | (slot_of[e["palette"]] << 12))
                    continue
                key = lut[e["palette"]][px].tobytes()
                if key not in glyphs:
                    if not free_tiles:
                        raise DecompError(f"{target}: no free tile slot left for {src}'s strip")
                    glyphs[key] = free_tiles.pop(0)
                ent.append(glyphs[key] | (int(e["hflip"]) << 10) | (int(e["vflip"]) << 11)
                           | (slot_of[e["palette"]] << 12))
            metas.append({"source": src, "source_layout": _layout_symbol(maps, src), "mid": mid, "tiles": ent,
                          "attr": int(mt["raw_attr"]), "version": VERSIONS.get(sver, "LAYOUT_VERSION_EMERALD")})
    if not metas:
        return {}
    top = beside["first"] if beside else 1024  # below a hand-made seam's range
    first = top - len(metas)
    borrowed_ids = 0
    if first < info["metatiles"]:
        # no room above its tilesets' metatiles: take the ids at the top it never shows, the ones it shows between
        # them left as they are (an empty entry: the game draws the map's own metatile)
        shown = _shown(maps, target)
        free = [] if shown is None else [i for i in range(info["npm"], top) if i not in shown][-len(metas):]
        if len(free) < len(metas):
            raise DecompError(f"{target}: {len(metas)} adapters don't fit above its {info['metatiles']} metatiles "
                              "(nor in the ids it never shows)")
        first, at = free[0], dict(zip(free, metas))
        metas = [at.get(i) for i in range(first, top)]
        borrowed_ids = sum(1 for i in free if i < info["metatiles"])
    return {"target": target, "layout": _layout_symbol(maps, target), "first": first, "metatiles": metas,
            "glyphs": {slot: np.frombuffer(k, np.uint8).reshape(8, 8) for k, slot in glyphs.items()},
            "palettes": bank_colors,
            "borrowed": {"palettes": sorted(b for b in bank_colors if b not in strict_banks),
                         "tiles": sum(1 for s in glyphs.values() if s not in strict_tiles),
                         "metatiles": borrowed_ids},
            "approximated": approx, "not_copied": unknown}


def _c_glyphs(prefix: str, glyphs: dict) -> tuple[str, int]:
    slots = sorted(glyphs)
    runs, cur = [], []
    for s in slots:
        if cur and s != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(s)
    if cur:
        runs.append(cur)
    out = []
    for i, run in enumerate(runs):
        words = []
        for s in run:
            px = glyphs[s]
            data = (px[:, 0::2] | (px[:, 1::2] << 4)).astype(np.uint8).tobytes()
            words += [int.from_bytes(data[j:j + 2], "little") for j in range(0, 32, 2)]
        out.append(f"static const u16 ALIGNED(4) {prefix}_GlyphRun{i}[] = {{ "
                   + ", ".join(f"0x{w:04X}" for w in words) + " };\n")
    out.append(f"static const struct KantoSeamGlyphRun {prefix}_GlyphRuns[] = {{\n"
               + "".join(f"    {{ {prefix}_GlyphRun{i}, {run[0]}, {len(run) * 32} }},\n" for i, run in enumerate(runs))
               + "};\n")
    return "".join(out), len(runs)


def render_header(plans: list[dict]) -> str:
    out = ["// Generated by pae (`pae seam build`) from src/data/pae_seams.json: adapter metatiles for connection",
           "// strips across different tilesets. Do not edit by hand.", "#ifndef GUARD_PAE_SEAMS_H",
           "#define GUARD_PAE_SEAMS_H", ""]
    layouts = sorted({p["layout"] for p in plans} | {m["source_layout"] for p in plans for m in p["metatiles"] if m})
    out += [f"extern const struct MapLayout {s};" for s in layouts] + [""]
    entries = []
    for p in plans:
        pre = f"sPaeSeam_{p['target']}"
        out.append(f"static const struct KantoSeamMetatile {pre}_Metatiles[] = {{")
        for m in p["metatiles"]:
            if m is None:  # an id the map shows, between borrowed ones: its own metatile
                out.append("    { .sourceLayout = NULL },")
                continue
            out.append(f"    {{ .sourceLayout = &{m['source_layout']}, .attributes = 0x{m['attr']:08X}u, "
                       f".sourceMid = {m['mid']}, .tiles = {{ " + ", ".join(f"0x{t:04X}" for t in m["tiles"])
                       + f" }}, .layoutVersion = {m['version']} }},")
        out.append("};")
        g, nruns = _c_glyphs(pre, p["glyphs"])
        out.append(g)
        out.append(f"static const struct KantoSeamPalette ALIGNED(4) {pre}_Palettes[] = {{")
        for slot, cols in sorted(p["palettes"].items()):
            c555 = [(r >> 3) | ((g_ >> 3) << 5) | ((b >> 3) << 10) for r, g_, b in cols]
            out.append("    { { " + ", ".join(f"0x{c:04X}" for c in c555) + f" }}, {slot * 16} }},")
        out.append("};\n")
        entries.append(f"{{ &{p['layout']}, {pre}_Metatiles, {pre}_GlyphRuns, {pre}_Palettes, NULL, {p['first']}, "
                       f"{len(p['metatiles'])}, {nruns}, {len(p['palettes'])}, 0, 0 }},")
    out.append("#define PAE_SEAMS \\")
    out += [f"    {e} \\" for e in entries]
    out += ["", "#endif // GUARD_PAE_SEAMS_H", ""]
    return "\n".join(out)


def _hook(maps) -> None:
    """Include the header in kanto_connection_seam.c and put PAE_SEAMS in sKantoSeams (once): last, so a map's
    hand-made seam is the first entry the game finds for it."""
    p = maps.root / SEAM_C
    text = p.read_text()
    if "pae_seams.h" in text:
        return
    if "static const struct KantoSeam sKantoSeams[] = {" not in text:
        raise DecompError(f"{SEAM_C} has no sKantoSeams table to add pae's seams to")
    text = text.replace('#include "kanto_connection_seam.h"\n', '#include "kanto_connection_seam.h"\n'
                        '#include "data/pae_seams.h"\n', 1)
    head, rest = text.split("static const struct KantoSeam sKantoSeams[] = {\n", 1)
    table, tail = rest.split("};", 1)
    text = head + "static const struct KantoSeam sKantoSeams[] = {\n" + table + "    PAE_SEAMS\n};" + tail
    maps.dp._backup(SEAM_C)
    p.write_text(text)


def hand_made(maps, layout: str) -> dict | None:
    """What a layout's hand-made seam takes: its metatile range, VRAM tile slots and palette slots (pae's adapters
    for other neighbours go beside it), or None."""
    p = maps.root / SEAM_C
    if not p.exists():
        return None
    import re
    text = p.read_text()
    body = text.split("static const struct KantoSeam sKantoSeams[] = {", 1)
    if len(body) != 2:
        return None
    row = re.search(rf"\{{\s*&{layout}\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\w+)\s*,\s*(\d+)\s*,\s*(\d+)",
                    body[1].split("};", 1)[0])
    if not row:
        return None
    _, glyphs, pals, anims, first, count = row.groups()

    def array(name):
        m = re.search(rf"\b{name}\[\]\s*=\s*\{{(.*?)\n\}};", text, re.DOTALL)
        return m.group(1) if m else ""
    tiles, banks = set(), set()
    for dest, size in re.findall(r"\{\s*\w+\s*,\s*(\d+)\s*,\s*(\d+)\s*\}", array(glyphs) if glyphs != "NULL" else ""):
        tiles |= set(range(int(dest), int(dest) + int(size) // 32))
    for dest in re.findall(r"\}\s*,\s*(\d+)\s*\}", array(pals) if pals != "NULL" else ""):
        banks.add(int(dest) // 16)
    for dest, size in re.findall(r"\{\s*\w+\s*,\s*(\d+)\s*,\s*(\d+)\s*,", array(anims) if anims != "NULL" else ""):
        tiles |= set(range(int(dest), int(dest) + int(size) // 32))
    return {"first": int(first), "count": int(count), "tiles": tiles, "banks": banks}


def engine_metatiles(maps, map_name: str) -> dict[int, tuple[int, str]]:
    """The metatiles the game makes for a map above its tilesets' own: the adapters of its seams (hand-made and
    pae's) and the ones its block fixes use (the dual layouts' in kanto_connection_seam.c). id -> (attributes, the
    layout version they're in), from the seam rows for the map's layout; ids a row leaves to the map (a NULL source)
    are left out."""
    import re
    sym = _layout_symbol(maps, maps.resolve(map_name))
    texts = [(maps.root / rel).read_text() for rel in (SEAM_C, HEADER) if (maps.root / rel).exists()]
    version_of = {v: k for k, v in VERSIONS.items()}
    out: dict[int, tuple[int, str]] = {}
    for text in texts:
        for array, first in re.findall(rf"\{{\s*&{sym}\s*,\s*(\w+)\s*,\s*\w+\s*,\s*\w+\s*,\s*\w+\s*,\s*(\d+)\s*,",
                                       text):
            body = next((m.group(1) for t in texts
                         for m in [re.search(rf"\b{array}\[\]\s*=\s*\{{(.*?)\n\}};", t, re.DOTALL)] if m), "")
            entries = [line for line in body.split("\n") if ".sourceLayout" in line]
            for i, line in enumerate(entries):
                attr = re.search(r"\.attributes\s*=\s*(0x[0-9A-Fa-f]+|\d+)", line)
                if "NULL" in line.split(".sourceLayout", 1)[1].split(",", 1)[0] or not attr:
                    continue
                ver = re.search(r"\.layoutVersion\s*=\s*(\w+)", line)
                out.setdefault(int(first) + i, (int(attr.group(1), 0), version_of.get(ver.group(1) if ver else "",
                                                                                         "frlg")))
    return out


def _several_per_layout(maps) -> bool:
    """Whether the game looks at every seam entry of a layout (needed to add pae's beside a hand-made one)."""
    p = maps.root / SEAM_C
    return p.exists() and "KantoSeam_FindNext" in p.read_text()


def existing_targets(maps) -> set[str]:
    """Layout symbols that already have a hand-made seam (pae won't replace those)."""
    p = maps.root / SEAM_C
    if not p.exists():
        return set()
    import re
    body = p.read_text().split("static const struct KantoSeam sKantoSeams[] = {", 1)
    return set(re.findall(r"\{\s*&(\w+)\s*,", body[1].split("};", 1)[0])) if len(body) == 2 else set()


def build(maps, map_name: str, skip=()) -> list[dict]:
    """Record seams between ``map_name`` and each neighbour with other tilesets, then regenerate the header.
    ``skip``: neighbours left without seams either way (e.g. one with no room for them until it's rebuilt); their
    pairs are dropped from the record, and come back at a build that doesn't skip them."""
    if not (maps.root / SEAM_C).exists():
        raise DecompError(f"this game has no {SEAM_C} (connection seams are a feature of that fork)")
    name = maps.resolve(map_name)
    rec_path = maps.root / RECORD
    record = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    lay = maps.layout_of(name)
    skip = {maps.resolve(x) for x in skip}
    for c in maps.get_map(name).get("connections") or []:
        other = maps.map_by_id.get(c.get("map"))
        if other is None or c.get("direction") not in ("up", "down", "left", "right") or other in skip:
            continue
        if _same_tilesets(lay, maps.layout_of(other)):
            continue
        for t, s in ((name, other), (other, name)):
            record.setdefault(t, [])
            if s not in record[t]:
                record[t].append(s)
    # drop pairs that share tilesets now
    record = {t: [s for s in srcs if not _same_tilesets(maps.layout_of(t), maps.layout_of(s)) and s not in skip]
              for t, srcs in record.items() if t not in skip}
    record = {t: s for t, s in sorted(record.items()) if s}
    hand = existing_targets(maps)
    plans = []
    for t, srcs in record.items():
        beside = None
        if _layout_symbol(maps, t) in hand:
            if not _several_per_layout(maps):
                raise DecompError(f"{t} already has a hand-made seam in {SEAM_C}, and this game uses one seam per "
                                  "map (pae's go beside a hand-made one when the game looks at every entry of a "
                                  "map: KantoSeam_FindNext)")
            beside = hand_made(maps, _layout_symbol(maps, t))
        p = plan(maps, t, srcs, beside)
        if p:
            plans.append(p)
    maps.dp._backup(RECORD)
    rec_path.write_text(json.dumps(record, indent=1) + "\n")
    maps.dp._backup(HEADER)
    (maps.root / HEADER).write_text(render_header(plans))
    _hook(maps)
    maps.dp._changed = None
    return [{"target": p["target"], "adapters": sum(1 for m in p["metatiles"] if m), "glyphs": len(p["glyphs"]),
             "palettes": sorted(p["palettes"]), "sources": sorted({m["source"] for m in p["metatiles"] if m}),
             "borrowed": p["borrowed"], "approximated": p["approximated"], "not_copied": p["not_copied"]}
            for p in plans]

"""Door animations for doors you drew yourself.

The game animates a door only when its metatile is in the door table of ``src/field_door.c`` with animation
frames. ``add(maps, map_name, x, y, panel)`` makes those frames from the closed door on the map: the door panel (a
pixel box inside the 16x16 block) slides up and shows the dark doorway, in three steps. Doors are recorded in
``src/data/pae_doors.json``; ``src/data/pae_doors.h`` is generated from it and included in the door table (once).
A record whose metatile is no longer a door (a tileset rebuild moved the blocks) is dropped at the next ``add``.
Small (one block) doors on FRLG/HnS-style layouts, the kind outdoor houses use.
"""

from __future__ import annotations

import json

import numpy as np

from .decomp import DecompError

RECORD = "src/data/pae_doors.json"
HEADER = "src/data/pae_doors.h"
DOOR_C = "src/field_door.c"
SOUNDS = {"normal": "DOOR_SOUND_NORMAL", "sliding": "DOOR_SOUND_SLIDING"}


def _block_pixels(maps, map_name: str, mid: int) -> tuple[list[np.ndarray], list[int], list]:
    """The four 8x8 tiles of a metatile as drawn (top layer over the bottom), each with one palette bank."""
    te = maps.tileset_editor
    lay = maps.layout_of(map_name)
    prim, sec, version = te.sides(lay)
    pals = te.palettes(map_name)
    mt = te.get_metatile(map_name, mid)
    sheets = {}
    for side in (prim, sec):
        sheets[side.tile_base] = te.read_tiles(side)[0]

    def tile(e) -> np.ndarray:
        base = 0 if e["tile"] < sec.tile_base else sec.tile_base
        arr = sheets[base]
        t = arr[e["tile"] - base] if e["tile"] - base < len(arr) else np.zeros((8, 8), np.uint8)
        t = t[:, ::-1] if e["hflip"] else t
        return t[::-1, :] if e["vflip"] else t
    out, banks = [], []
    for q in range(4):
        b, t = mt["entries"][q], mt["entries"][q + 4]
        bp, tp = tile(b), tile(t)
        rgb = np.zeros((8, 8, 3), np.int32)
        for yy in range(8):
            for xx in range(8):
                if tp[yy, xx]:
                    rgb[yy, xx] = pals[t["palette"]][tp[yy, xx]]
                else:
                    rgb[yy, xx] = pals[b["palette"]][bp[yy, xx]]
        # one bank per door tile: the one that has all its colours (the top layer's, then the bottom's)
        best = None
        for bank in (t["palette"], b["palette"]):
            pal = np.array(pals[bank][1:], np.int32)
            d = ((rgb[..., None, :] - pal[None, None]) ** 2).sum(-1)
            err = int(d.min(-1).sum())
            if best is None or err < best[0]:
                best = (err, bank, (d.argmin(-1) + 1).astype(np.uint8))
        out.append(best[2])
        banks.append(best[1])
    return out, banks, pals


def frames(tiles: list[np.ndarray], banks: list[int], pals, panel: tuple[int, int, int, int]) -> list[list[np.ndarray]]:
    """Three frames: the panel slid up by a third, two thirds, all the way (the doorway is the darkest colour)."""
    block = np.zeros((16, 16), np.uint8)
    for q, t in enumerate(tiles):
        block[(q // 2) * 8:(q // 2) * 8 + 8, (q % 2) * 8:(q % 2) * 8 + 8] = t
    x0, y0, x1, y1 = panel
    if not (0 <= x0 < x1 <= 16 and 0 <= y0 < y1 <= 16):
        raise DecompError("the panel is a box inside the 16x16 door block: x0,y0,x1,y1")
    out = []
    h = y1 - y0
    for step in (1, 2, 3):
        f = block.copy()
        shift = round(h * step / 3)
        f[y0:y1, x0:x1] = 0
        if shift < h:
            f[y0:y1 - shift, x0:x1] = block[y0 + shift:y1, x0:x1]
        frame_tiles = []
        for q in range(4):
            t = f[(q // 2) * 8:(q // 2) * 8 + 8, (q % 2) * 8:(q % 2) * 8 + 8].copy()
            pal = np.array(pals[banks[q]], np.int32)
            dark = int(np.argmin(pal[1:].sum(1))) + 1
            t[t == 0] = dark  # the doorway
            frame_tiles.append(t)
        out.append(frame_tiles)
    return out


def _tileset_of(maps, map_name: str, mid: int) -> str:
    te = maps.tileset_editor
    prim, sec, _ = te.sides(maps.layout_of(map_name))
    return prim.symbol if mid < sec.metatile_base else sec.symbol


def _still_a_door(maps, r: dict) -> bool:
    """Whether a record's metatile is still a door block of its tileset (False once a rebuild has moved it)."""
    te = maps.tileset_editor
    try:
        mt = te.get_metatile(r["map"], r["metatile"])
        version = te.sides(maps.layout_of(r["map"]))[2]
    except (DecompError, KeyError):
        return False
    names = te.behaviors(version)
    name = names[mt["behavior"]] if mt["behavior"] < len(names) else None
    return mt["tileset"] == r["tileset"] and ("DOOR" in name if name else mt["behavior"] != 0)


def add(maps, map_name: str, x: int, y: int, panel: tuple[int, int, int, int], sound: str = "normal") -> dict:
    name = maps.resolve(map_name)
    if not (maps.root / DOOR_C).exists():
        raise DecompError(f"no {DOOR_C} in this game")
    lay = maps.layout_of(name)
    if lay.get("layout_version") not in ("frlg", "hns"):
        raise DecompError("door animations from pae are for FRLG/HnS-style layouts (layout_version frlg or hns)")
    b = maps.read_blocks(lay)
    if not (0 <= y < b.shape[0] and 0 <= x < b.shape[1]):
        raise DecompError(f"({x},{y}) is outside {name}")
    mid = int(b[y, x]) & 0x3FF
    tiles, banks, pals = _block_pixels(maps, name, mid)
    fr = frames(tiles, banks, pals, panel)
    rec_path = maps.root / RECORD
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else []
    key = (_tileset_of(maps, name, mid), mid)
    rec = [r for r in rec if (r["tileset"], r["metatile"]) != key and _still_a_door(maps, r)]
    rec.append({"map": name, "tileset": key[0], "metatile": mid, "panel": list(panel), "sound": sound,
                "banks": banks, "frames": [[t.tolist() for t in f] for f in fr]})
    maps.dp._backup(RECORD)
    rec_path.write_text(json.dumps(rec) + "\n")
    _write_header(maps, rec)
    return {"metatile": mid, "tileset": key[0], "banks": banks}


def _write_header(maps, rec: list[dict]) -> None:
    out = ["// Generated by pae (`pae door add`) from src/data/pae_doors.json: door animations. Do not edit by hand.",
           "#ifndef GUARD_PAE_DOORS_H", "#define GUARD_PAE_DOORS_H", ""]
    entries = []
    for i, r in enumerate(rec):
        data = []
        for f in r["frames"]:
            for t in f:
                px = np.array(t, np.uint8)
                data += list((px[:, 0::2] | (px[:, 1::2] << 4)).astype(np.uint8).tobytes())
        data += [0] * (16 * 32 - len(data))  # the game copies 8 tiles from the last frame
        out.append(f"static const u8 ALIGNED(4) sPaeDoorTiles_{i}[{len(data)}] = {{ "
                   + ", ".join(f"0x{v:02X}" for v in data) + " };")
        banks = r["banks"] + r["banks"]
        out.append(f"static const u8 sPaeDoorPalettes_{i}[] = {{ " + ", ".join(map(str, banks)) + " };")
        entries.append(f"{{ {r['metatile']}, &{r['tileset']}, {SOUNDS.get(r['sound'], 'DOOR_SOUND_NORMAL')}, 1, "
                       f"sPaeDoorTiles_{i}, sPaeDoorPalettes_{i} }},")
    out.append("")
    out.append("#define PAE_DOORS \\")
    out += [f"    {e} \\" for e in entries]
    out += ["", "#endif // GUARD_PAE_DOORS_H", ""]
    maps.dp._backup(HEADER)
    (maps.root / HEADER).write_text("\n".join(out))
    p = maps.root / DOOR_C
    text = p.read_text()
    if "pae_doors.h" not in text:
        anchor = "static const struct DoorGraphics sDoorAnimGraphicsTable[] =\n{\n"
        if anchor not in text:
            raise DecompError(f"{DOOR_C} has no sDoorAnimGraphicsTable to add pae's doors to")
        text = text.replace(anchor, '#include "data/pae_doors.h"\n\n' + anchor + "    PAE_DOORS\n", 1)
        maps.dp._backup(DOOR_C)
        p.write_text(text)
    maps.dp._changed = None

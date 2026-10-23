"""Door animations for doors you drew yourself.

The game animates a door only when its metatile is in the door table of ``src/field_door.c`` with animation
frames. ``add(maps, map_name, x, y, panel)`` makes those frames from the closed door on the map: the door panel (a
pixel box inside the 16x16 block) slides up and shows the dark doorway, in three steps. Or the frames are drawn:
``add(..., frames=image)``, three frames side by side, 48x16 for a one-block door, 48x32 for a tall one (the door
block and the block above it, FRLG's large doors) or 96x32 for a wide one (two door blocks side by side and the two
above them, a double door: ``x, y`` is its left door block, and both open whichever of them you walk into);
transparent pixels show the block as it is. Doors are recorded in ``src/data/pae_doors.json``;
``src/data/pae_doors.h`` is generated from it and included in the door table (once). A record whose metatile is no
longer a door (a tileset rebuild moved the blocks) is dropped at the next ``add``. Doors on FRLG/HnS-style layouts,
the kind outdoor houses use. A wide door needs the game's door code to know them (``DOOR_WIDE_LEFT`` in
``src/field_door.c``) and 16 free tile slots for its frames (HnS layouts: 434-449; FRLG: 1008-1023).
"""

from __future__ import annotations

import json

import numpy as np

from .decomp import DecompError

RECORD = "src/data/pae_doors.json"
WIDE_LEFT, WIDE_RIGHT = 3, 4  # a wide door's two door blocks (the sizes small doors are 1 and tall ones 2)
WIDE_SLOTS = {"hns": range(434, 450), "frlg": range(1008, 1024)}  # where the game copies a wide door's frames
HEADER = "src/data/pae_doors.h"
DOOR_C = "src/field_door.c"
SOUNDS = {"normal": "DOOR_SOUND_NORMAL", "sliding": "DOOR_SOUND_SLIDING"}


def _block_rgb(maps, map_name: str, mid: int) -> tuple[list[np.ndarray], list[tuple[int, int]], list]:
    """The four 8x8 tiles of a metatile as drawn (top layer over the bottom) in RGB, with each tile's two palette
    banks (its top layer's, then its bottom's)."""
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
        out.append(rgb)
        banks.append((t["palette"], b["palette"]))
    return out, banks, pals


def _in_bank(rgb: np.ndarray, candidates, pals) -> tuple[int, int, np.ndarray]:
    """The bank of ``candidates`` that has a tile's colours (the closest, if none has them all): (error, bank,
    the tile as that bank's colour indices)."""
    best = None
    for bank in candidates:
        pal = np.array(pals[bank][1:], np.int32)
        d = ((rgb[..., None, :] - pal[None, None]) ** 2).sum(-1)
        err = int(d.min(-1).sum())
        if best is None or err < best[0]:
            best = (err, bank, (d.argmin(-1) + 1).astype(np.uint8))
    return best


def _block_pixels(maps, map_name: str, mid: int) -> tuple[list[np.ndarray], list[int], list]:
    """The four 8x8 tiles of a metatile as drawn (top layer over the bottom), each with one palette bank."""
    rgbs, cands, pals = _block_rgb(maps, map_name, mid)
    out, banks = [], []
    for rgb, c in zip(rgbs, cands):
        _, bank, t = _in_bank(rgb, c, pals)
        out.append(t)
        banks.append(bank)
    return out, banks, pals


def drawn_frames(maps, map_name: str, x: int, y: int, image) -> tuple[int, list[list[np.ndarray]], list[int]]:
    """Frames from a drawn strip (an RGBA array: 48x16, 48x32, or 96x32 for a wide door): (door size - 1, 2 or
    ``WIDE_LEFT`` -, each frame's tiles - the block above's four first for a tall door; for a wide one the left
    column, then the right, each the block above, then the door -, the tiles' banks). Each tile takes the bank of
    its block's layers that has its colours; a colour no bank has is an error."""
    a = np.asarray(image)
    if a.ndim != 3 or (a.shape[1], a.shape[0]) not in ((48, 16), (48, 32), (96, 32)):
        raise DecompError("door frames are three frames side by side: 48x16 (a one-block door), 48x32 (tall) or "
                          "96x32 (wide: two door blocks side by side)")
    wide = a.shape[1] == 96
    size = WIDE_LEFT if wide else a.shape[0] // 16
    b = maps.read_blocks(maps.layout_of(map_name))
    if size != 1 and y < 1:
        raise DecompError("a tall door needs the block above it on the map")
    if wide and x + 1 >= b.shape[1]:
        raise DecompError("a wide door needs the block right of its left door block on the map")
    blocks = [(x + i // 2, y - 1 + i % 2) for i in range(4)] if wide else [(x, y - 1), (x, y)] if size == 2 \
        else [(x, y)]
    fw = 32 if wide else 16
    closed = [_block_rgb(maps, map_name, int(b[yy, xx]) & 0x3FF) for xx, yy in blocks]
    out, banks = [], None
    for k in range(3):
        tiles, fb = [], []
        for j, (rgbs, cands, pals) in enumerate(closed):
            row, col = (j % 2, j // 2) if wide else (j, 0)
            for q in range(4):
                y0, x0 = row * 16 + (q // 2) * 8, k * fw + col * 16 + (q % 2) * 8
                px = a[y0:y0 + 8, x0:x0 + 8]
                rgb = np.where(px[..., 3:4] > 0, px[..., :3].astype(np.int32), rgbs[q])
                err, bank, t = _in_bank(rgb, cands[q], pals)
                if err:
                    raise DecompError(f"door frame {k + 1}, tile {j * 4 + q + 1}: colours that neither of its "
                                      f"block's palettes ({', '.join(map(str, cands[q]))}) has")
                tiles.append(t)
                fb.append(bank)
        if banks is not None and fb != banks:  # every frame draws with the first frame's banks
            raise DecompError("each door tile must keep its palette in all three frames")
        banks = fb
        out.append(tiles)
    return size, out, banks


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


def add(maps, map_name: str, x: int, y: int, panel: tuple[int, int, int, int] | None = None, sound: str = "normal",
        frames_image=None) -> dict:
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
    if frames_image is not None:
        size, fr, banks = drawn_frames(maps, name, x, y, frames_image)
    elif panel is not None:
        size = 1
        tiles, banks, pals = _block_pixels(maps, name, mid)
        fr = frames(tiles, banks, pals, panel)
    else:
        raise DecompError("give the door's panel (to slide it up) or its drawn frames")
    leaves = [(mid, size)]  # a wide door: its left door block, then its right (the same frames)
    if size == WIDE_LEFT:
        _check_wide(maps, name, lay)
        right = int(b[y, x + 1]) & 0x3FF
        if right == mid:
            raise DecompError(f"a wide door's two door blocks are one metatile (0x{mid:03X}): the game couldn't tell "
                              "which of them was walked into")
        leaves.append((right, WIDE_RIGHT))
    new = [{"map": name, "tileset": _tileset_of(maps, name, m), "metatile": m, "panel": list(panel) if panel else None,
            "size": sz, "sound": sound, "banks": banks, "frames": [[t.tolist() for t in f] for f in fr]}
           for m, sz in leaves]
    for r in new:
        if not _still_a_door(maps, r):
            raise DecompError(f"metatile 0x{r['metatile']:03X} at the door isn't a door block (MB_ANIMATED_DOOR)")
    rec_path = maps.root / RECORD
    rec = json.loads(rec_path.read_text()) if rec_path.exists() else []
    keys = {(r["tileset"], r["metatile"]) for r in new}
    rec = [r for r in rec if (r["tileset"], r["metatile"]) not in keys and _still_a_door(maps, r)] + new
    rec.sort(key=lambda r: (r["tileset"], r["metatile"]))  # the same file whatever order doors were added in
    maps.dp._backup(RECORD)
    rec_path.write_text(json.dumps(rec) + "\n")
    _write_header(maps, rec)
    out = {"metatile": mid, "tileset": new[0]["tileset"], "banks": banks, "size": size}
    if size == WIDE_LEFT:
        out["right"] = leaves[1][0]
    return out


def _check_wide(maps, map_name: str, lay: dict) -> None:
    """A wide door's frames go in 16 tile slots the game copies them to: the game's door code must know wide doors,
    and the map's tilesets must leave those slots free."""
    if "DOOR_WIDE_LEFT" not in (maps.root / DOOR_C).read_text():
        raise DecompError(f"this game's {DOOR_C} has no wide doors (DOOR_WIDE_LEFT): only one-block-wide doors")
    te = maps.tileset_editor
    prim, sec, version = te.sides(lay)
    slots = set(WIDE_SLOTS[version])
    for side in (prim, sec):
        used = {int(v) & 0x3FF for row in te._read_metas(side) for v in row} & slots
        if used:
            raise DecompError(f"{side.symbol} draws with tiles {min(used)}-{max(used)}, where the game copies a wide "
                              f"door's frames ({min(slots)}-{max(slots)}): reserve them in the tileset")


def _write_header(maps, rec: list[dict]) -> None:
    out = ["// Generated by pae (`pae door add`) from src/data/pae_doors.json: door animations. Do not edit by hand.",
           "#ifndef GUARD_PAE_DOORS_H", "#define GUARD_PAE_DOORS_H", ""]
    entries, arrays = [], {}  # tile data -> its array (a wide door's two blocks share theirs)
    for i, r in enumerate(rec):
        data = []
        for f in r["frames"]:
            for t in f:
                px = np.array(t, np.uint8)
                data += list((px[:, 0::2] | (px[:, 1::2] << 4)).astype(np.uint8).tobytes())
        size = r.get("size", 1)
        # frames are 4 tiles apart for a small door, 8 for a tall one, 16 for a wide one, and the game copies 8 tiles
        # from the last (16 for a wide one)
        per = len(r["frames"][0])
        data += [0] * ((2 * per + (16 if size >= WIDE_LEFT else 8)) * 32 - len(data))
        tiles = arrays.get(bytes(data))
        if tiles is None:
            tiles = arrays[bytes(data)] = f"sPaeDoorTiles_{i}"
            out.append(f"static const u8 ALIGNED(4) {tiles}[{len(data)}] = {{ "
                       + ", ".join(f"0x{v:02X}" for v in data) + " };")
        # each block's four tiles, then four for the layer the game leaves empty (it reads them from the next block's
        # place for the block above a tall door)
        banks = r["banks"] + r["banks"][-4:]
        out.append(f"static const u8 sPaeDoorPalettes_{i}[] = {{ " + ", ".join(map(str, banks)) + " };")
        entries.append(f"{{ {r['metatile']}, &{r['tileset']}, {SOUNDS.get(r['sound'], 'DOOR_SOUND_NORMAL')}, {size}, "
                       f"{tiles}, sPaeDoorPalettes_{i} }},")
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

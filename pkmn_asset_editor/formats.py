"""Render assets to editable files and parse edited files back into ROM data."""

from __future__ import annotations

import numpy as np

from .catalog import Asset
from .gfx import (GRAYSCALE16, RGB, Layout, LoadedImage, decode_tiles, encode_tiles, image_to_tiles,
                  pad_palette, remap_to_palette, same_color, tiles_to_image)

FILLER: RGB = (64, 64, 64)


class AssetError(ValueError):
    pass


# --------------------------------------------------------------------------- helpers

def sprite_data(asset: Asset, raw: dict[int, bytes]) -> bytes:
    if "frames" in asset.blobs:
        return b"".join(raw[o] for o in asset.blobs["frames"])
    return raw[asset.blobs["tiles"]]


def link_colors(link: dict, palettes: dict[str, list[RGB]]) -> list[RGB]:
    pal = palettes.get(link["palette"])
    if pal is None:
        return [FILLER] * 16
    s = link["pal_bank"] * 16
    return pad_palette(pal[s:s + 16], 16)


def image_palette(asset: Asset, palettes: dict[str, list[RGB]]) -> list[RGB]:
    """The PLTE an asset's PNG is written with."""
    if asset.kind == "sprite":
        if not asset.palettes:
            return list(GRAYSCALE16)
        return link_colors(asset.palettes[0], palettes)
    plte = [FILLER] * 256
    for link in asset.palettes:
        b = link["png_bank"] * 16
        plte[b:b + 16] = link_colors(link, palettes)
    return plte


def tileset_layout(asset: Asset) -> Layout:
    return Layout.sheet(asset.layout["count"], asset.layout["tiles_wide"])


def map_entries(raw_map: bytes) -> list[int]:
    return [raw_map[i] | (raw_map[i + 1] << 8) for i in range(0, len(raw_map) - 1, 2)]


def cell_xy(i: int, map_w: int) -> tuple[int, int]:
    if map_w == 64:
        sb, j = divmod(i, 1024)
        return sb * 32 + j % 32, j // 32
    return i % map_w, i // map_w


def _flip(t: np.ndarray, h: int, v: int) -> np.ndarray:
    if h:
        t = t[:, ::-1]
    if v:
        t = t[::-1, :]
    return t


# --------------------------------------------------------------------------- render

def render(asset: Asset, raw: dict[int, bytes], palettes: dict[str, list[RGB]]) -> tuple[np.ndarray, list[RGB]]:
    """Return ``(pixels, plte)`` for an image asset."""
    if asset.kind == "sprite":
        layout = Layout.from_dict(asset.layout)
        tiles = decode_tiles(sprite_data(asset, raw), asset.layout.get("bpp", 4))
        return tiles_to_image(tiles, layout), image_palette(asset, palettes)
    if asset.kind == "tileset":
        layout = tileset_layout(asset)
        tiles = decode_tiles(raw[asset.blobs["tiles"]])
        banks = asset.layout["banks"]
        tiles = tiles | np.array([int(c, 16) << 4 for c in banks], dtype=np.uint8)[: len(tiles), None, None]
        return tiles_to_image(tiles, layout), image_palette(asset, palettes)
    if asset.kind == "bg":
        return render_bg(asset, raw[asset.blobs["tiles"]], raw[asset.blobs["map"]]), image_palette(asset, palettes)
    raise AssetError(f"{asset.id} is not an image asset")


def render_bg(asset: Asset, raw_tiles: bytes, raw_map: bytes) -> np.ndarray:
    mw, mh = asset.layout["map_w"], asset.layout["map_h"]
    tiles = decode_tiles(raw_tiles)
    img = np.zeros((mh * 8, mw * 8), dtype=np.uint8)
    blank = np.zeros((8, 8), dtype=np.uint8)
    for i, e in enumerate(map_entries(raw_map)):
        x, y = cell_xy(i, mw)
        if y >= mh:
            continue
        t = e & 0x3FF
        px = tiles[t] if t < len(tiles) else blank
        img[y * 8:y * 8 + 8, x * 8:x * 8 + 8] = _flip(px, (e >> 10) & 1, (e >> 11) & 1) | ((e >> 12) << 4)
    return img


# --------------------------------------------------------------------------- palettes from PNGs

def plte_proposals(asset: Asset, img: LoadedImage, original: dict[str, list[RGB]]) -> list[tuple[str, int, dict[int, RGB]]]:
    """Palette entries an indexed PNG changed relative to the ROM: [(palette id, colour index, rgb)]."""
    if img.palette is None:
        return []
    out = []
    for link in asset.palettes:
        base = link["png_bank"] * 16
        pal = original.get(link["palette"])
        if pal is None:
            continue
        changes = {}
        for j in range(16):
            if base + j >= len(img.palette):
                break
            ci = link["pal_bank"] * 16 + j
            if ci >= len(pal):
                break
            if not same_color(img.palette[base + j], pal[ci]):
                changes[ci] = tuple(int(v) for v in img.palette[base + j][:3])
        if changes:
            out.append((link["palette"], link["png_bank"], changes))
    return out


# --------------------------------------------------------------------------- parse

class Parsed:
    def __init__(self):
        self.blobs: dict[int, bytes] = {}
        self.warnings: list[str] = []


def _check_dims(asset: Asset, img: LoadedImage, w: int, h: int) -> None:
    if (img.width, img.height) != (w, h):
        raise AssetError(f"{asset.id}: image is {img.width}x{img.height} but must be {w}x{h} "
                         f"(use `pae import --resize` to scale artwork)")


def _sprite_indices(asset: Asset, img: LoadedImage, palettes, hint: np.ndarray, parsed: Parsed) -> np.ndarray:
    if img.indices is not None and int(img.indices.max(initial=0)) < 16:
        return img.indices
    pal = image_palette(asset, palettes)
    idx, inexact = remap_to_palette(img.rgba, pal, hint)
    if inexact:
        parsed.warnings.append(f"{asset.id}: {inexact} pixels used colours not in the palette and were "
                               f"approximated (re-import with --new-palette to rebuild the palette)")
    return idx


def parse(asset: Asset, img: LoadedImage, raw: dict[int, bytes], palettes: dict[str, list[RGB]]) -> Parsed:
    """Convert an edited image into new blob contents. ``raw`` is the original ROM data."""
    parsed = Parsed()
    if asset.kind == "sprite":
        layout = Layout.from_dict(asset.layout)
        _check_dims(asset, img, layout.width, layout.height)
        bpp = asset.layout.get("bpp", 4)
        hint = tiles_to_image(decode_tiles(sprite_data(asset, raw), bpp), layout)
        idx = _sprite_indices(asset, img, palettes, hint, parsed)
        data = encode_tiles(image_to_tiles(idx, layout), bpp)
        if "tiles" in asset.blobs:
            parsed.blobs[asset.blobs["tiles"]] = data
        else:
            pos = 0
            for off in asset.blobs["frames"]:
                size = len(raw[off])
                chunk = data[pos:pos + size]
                pos += size
                prev = parsed.blobs.get(off)
                if prev is not None and prev != chunk:
                    if chunk != raw[off] and prev != raw[off]:
                        parsed.warnings.append(f"{asset.id}: frame at 0x{off:X} appears twice with different "
                                               f"edits; keeping the first")
                    if prev == raw[off]:
                        parsed.blobs[off] = chunk
                    continue
                parsed.blobs[off] = chunk
        return parsed
    if asset.kind == "tileset":
        return _parse_tileset(asset, img, raw, palettes, parsed)
    if asset.kind == "bg":
        return _parse_bg(asset, img, raw, palettes, parsed)
    raise AssetError(f"{asset.id} is not an image asset")


def _per_tile_remap(block: np.ndarray, plte: list[RGB], banks: list[int], prefer: int, hint: np.ndarray):
    """Pick the bank that represents an 8x8 RGBA block best, return (bank, indices, inexact)."""
    best = None
    order = [prefer] + [b for b in banks if b != prefer]
    for b in order:
        pal = plte[b * 16:b * 16 + 16]
        idx, inexact = remap_to_palette(block, pal, hint if b == prefer else None)
        if inexact == 0:
            return b, idx, 0
        if best is None or inexact < best[2]:
            best = (b, idx, inexact)
    return best


def _parse_tileset(asset, img, raw, palettes, parsed):
    layout = tileset_layout(asset)
    _check_dims(asset, img, layout.width, layout.height)
    banks = [int(c, 16) for c in asset.layout["banks"]]
    orig = decode_tiles(raw[asset.blobs["tiles"]])
    if img.indices is not None:
        full = img.indices
        low = full & 0x0F
        tiles = image_to_tiles(low, layout)
        if int(full.max(initial=0)) >= 16:
            hi = image_to_tiles(full >> 4, layout)
            moved = [t for t in range(len(tiles)) if (tiles[t] != orig[t]).any()
                     and np.any((hi[t] != banks[t]) & (tiles[t] != 0))]
            if moved:
                parsed.warnings.append(f"{asset.id}: {len(moved)} edited tiles use colours from a different "
                                       f"palette than the metatiles draw them with (tile {moved[0]}…); only the "
                                       f"colour index is stored, the in-game palette is chosen by the metatile")
    else:
        plte = image_palette(asset, palettes)
        linked = sorted({l["png_bank"] for l in asset.palettes}) or [0]
        tiles = np.zeros_like(orig)
        inexact_total = 0
        for t in range(layout.count):
            x, y = layout.tile_pos(t)
            block = img.rgba[y:y + 8, x:x + 8]
            _, idx, inexact = _per_tile_remap(block, plte, linked, banks[t], orig[t])
            tiles[t] = idx
            inexact_total += inexact
        if inexact_total:
            parsed.warnings.append(f"{asset.id}: {inexact_total} pixels were approximated to the tileset palettes")
    parsed.blobs[asset.blobs["tiles"]] = encode_tiles(tiles)
    return parsed


def _bg_cells(asset, img, palettes, orig_img):
    """Split the BG image into 8x8 cells → list of (bank, 8x8 indices)."""
    mw, mh = asset.layout["map_w"], asset.layout["map_h"]
    cells = []
    linked = sorted({l["png_bank"] for l in asset.palettes}) or [0]
    plte = image_palette(asset, palettes) if img.indices is None else None
    inexact_total = 0
    for cy in range(mh):
        for cx in range(mw):
            ys, xs = cy * 8, cx * 8
            ob = orig_img[ys:ys + 8, xs:xs + 8]
            obank = int(np.bincount((ob >> 4).ravel()).argmax())
            if img.indices is not None:
                blk = img.indices[ys:ys + 8, xs:xs + 8]
                hi = blk >> 4
                lo = blk & 0x0F
                nz = hi[lo != 0]
                bank = int(np.bincount(nz).argmax()) if len(nz) else obank
                cells.append((bank, lo.copy()))
            else:
                bank, idx, inexact = _per_tile_remap(img.rgba[ys:ys + 8, xs:xs + 8], plte, linked, obank, ob & 0x0F)
                inexact_total += inexact
                cells.append((bank, idx))
    return cells, inexact_total


def _parse_bg(asset, img, raw, palettes, parsed):
    mw, mh = asset.layout["map_w"], asset.layout["map_h"]
    _check_dims(asset, img, mw * 8, mh * 8)
    tiles_off, map_off = asset.blobs["tiles"], asset.blobs["map"]
    orig_tiles = decode_tiles(raw[tiles_off])
    entries = map_entries(raw[map_off])
    orig_img = render_bg(asset, raw[tiles_off], raw[map_off])
    cells, inexact = _bg_cells(asset, img, palettes, orig_img)
    if inexact:
        parsed.warnings.append(f"{asset.id}: {inexact} pixels were approximated to the background palette")

    # 1) try to keep the existing tilemap and only change tile pixels / palette bits
    new_tiles = orig_tiles.copy()
    new_entries = list(entries)
    claimed: dict[int, np.ndarray] = {}
    consistent = True
    for i, e in enumerate(entries):
        x, y = cell_xy(i, mw)
        if y >= mh:
            continue
        bank, px = cells[y * mw + x]
        t = e & 0x3FF
        if t >= len(orig_tiles):
            if px.any():
                consistent = False
            continue
        unflipped = _flip(px, (e >> 10) & 1, (e >> 11) & 1)
        if t in claimed:
            if not np.array_equal(claimed[t], unflipped):
                consistent = False
        else:
            claimed[t] = unflipped
        if bank != e >> 12:
            new_entries[i] = (e & 0x0FFF) | (bank << 12)
    if consistent or not asset.layout.get("retile"):
        if not consistent:
            parsed.warnings.append(f"{asset.id}: some tiles are reused in several places but were edited "
                                   f"differently; this background's tiles are shared, so the first edit wins")
            claimed = {}
            for i, e in enumerate(entries):
                x, y = cell_xy(i, mw)
                t = e & 0x3FF
                if y >= mh or t >= len(orig_tiles):
                    continue
                unflipped = _flip(cells[y * mw + x][1], (e >> 10) & 1, (e >> 11) & 1)
                if t not in claimed or (np.array_equal(claimed[t], orig_tiles[t]) and not np.array_equal(unflipped, orig_tiles[t])):
                    claimed[t] = unflipped
        for t, px in claimed.items():
            new_tiles[t] = px
        parsed.blobs[tiles_off] = encode_tiles(new_tiles)
        parsed.blobs[map_off] = b"".join(v.to_bytes(2, "little") for v in new_entries)
        return parsed

    # 2) re-tile: rebuild the tile set with de-duplication (including flips)
    lookup: dict[bytes, tuple[int, int, int]] = {}
    out_tiles: list[np.ndarray] = []
    out_entries = list(entries)
    for i in range(len(entries)):
        x, y = cell_xy(i, mw)
        if y >= mh:
            continue
        bank, px = cells[y * mw + x]
        key = px.tobytes()
        hit = lookup.get(key)
        if hit is None:
            t = len(out_tiles)
            out_tiles.append(px)
            for h in (0, 1):
                for v in (0, 1):
                    lookup.setdefault(_flip(px, h, v).tobytes(), (t, h, v))
            hit = (t, 0, 0)
        t, h, v = hit
        out_entries[i] = t | (h << 10) | (v << 11) | (bank << 12)
    limit = len(orig_tiles)
    if len(out_tiles) > limit:
        raise AssetError(f"{asset.id}: the edited background needs {len(out_tiles)} unique 8x8 tiles but only "
                         f"{limit} fit in its VRAM slot; reuse more tiles or simplify the art")
    while len(out_tiles) < limit and len(out_tiles) < len(orig_tiles):
        out_tiles.append(np.zeros((8, 8), dtype=np.uint8))
    parsed.blobs[tiles_off] = encode_tiles(np.array(out_tiles))
    parsed.blobs[map_off] = b"".join(v.to_bytes(2, "little") for v in out_entries)
    return parsed

"""Tiles, palettes, PNG I/O and colour quantisation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image

RGB = tuple[int, int, int]


# --------------------------------------------------------------------------- palettes

def bgr555_to_rgb(c: int) -> RGB:
    r, g, b = c & 31, (c >> 5) & 31, (c >> 10) & 31
    return ((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2))


def rgb_to_bgr555(rgb: RGB) -> int:
    r, g, b = rgb[:3]
    return (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10)


def decode_palette(raw: bytes) -> list[RGB]:
    return [bgr555_to_rgb(raw[i] | (raw[i + 1] << 8)) for i in range(0, len(raw) - 1, 2)]


def encode_palette(colors: list[RGB]) -> bytes:
    out = bytearray()
    for c in colors:
        v = rgb_to_bgr555(c)
        out += bytes((v & 0xFF, v >> 8))
    return bytes(out)


def same_color(a: RGB, b: RGB) -> bool:
    """Colours are equal at GBA (5 bits per channel) precision."""
    return all((x >> 3) == (y >> 3) for x, y in zip(a[:3], b[:3]))


def looks_like_palette(raw: bytes) -> bool:
    # Only small blobs: larger ones are far more likely to be tiles whose odd bytes happen to be < 0x80.
    if not raw or len(raw) % 32 or len(raw) > 0x80:
        return False
    return all(raw[i + 1] < 0x80 for i in range(0, len(raw), 2))


GRAYSCALE16: list[RGB] = [(v, v, v) for v in (255 - i * 17 for i in range(16))]


def read_jasc(path) -> list[RGB]:
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        lines = [ln.strip() for ln in fh if ln.strip()]
    if len(lines) < 3 or lines[0] != "JASC-PAL":
        raise ValueError(f"{path}: not a JASC-PAL file")
    count = int(lines[2])
    colors = []
    for ln in lines[3:3 + count]:
        parts = ln.split()
        colors.append((int(parts[0]), int(parts[1]), int(parts[2])))
    if len(colors) != count:
        raise ValueError(f"{path}: expected {count} colours, found {len(colors)}")
    return colors


def write_jasc(path, colors: list[RGB]) -> None:
    with open(path, "w", encoding="ascii", newline="\r\n") as fh:
        fh.write("JASC-PAL\n0100\n%d\n" % len(colors))
        for r, g, b in colors:
            fh.write(f"{r} {g} {b}\n")


# --------------------------------------------------------------------------- tiles

def decode_tiles(raw: bytes, bpp: int = 4) -> np.ndarray:
    """Return an (n, 8, 8) uint8 array of colour indices."""
    arr = np.frombuffer(raw, dtype=np.uint8)
    if bpp == 4:
        n = len(arr) // 32
        arr = arr[: n * 32]
        px = np.empty(len(arr) * 2, dtype=np.uint8)
        px[0::2] = arr & 0x0F
        px[1::2] = arr >> 4
    elif bpp == 8:
        n = len(arr) // 64
        px = arr[: n * 64].copy()
    else:
        raise ValueError("bpp must be 4 or 8")
    return px.reshape(-1, 8, 8)


def encode_tiles(tiles: np.ndarray, bpp: int = 4) -> bytes:
    flat = np.ascontiguousarray(tiles, dtype=np.uint8).reshape(-1)
    if bpp == 4:
        return bytes((flat[0::2] & 0x0F) | ((flat[1::2] & 0x0F) << 4))
    return bytes(flat)


@dataclass
class Layout:
    """How a run of tiles is arranged into an image.

    Tiles are grouped into frames of ``frame_w`` x ``frame_h`` tiles (row-major
    inside a frame, which is how GBA 1D-mapped sprites are stored) and frames
    are placed left-to-right, top-to-bottom, ``cols`` per row. Positions past
    ``count`` tiles are padding and are ignored when encoding.
    """

    frame_w: int
    frame_h: int
    frames: int
    cols: int
    count: int

    @property
    def rows(self) -> int:
        return -(-self.frames // self.cols)

    @property
    def width(self) -> int:
        return self.cols * self.frame_w * 8

    @property
    def height(self) -> int:
        return self.rows * self.frame_h * 8

    def tile_pos(self, t: int) -> tuple[int, int]:
        per = self.frame_w * self.frame_h
        f, i = divmod(t, per)
        fr, fc = divmod(f, self.cols)
        return (fc * self.frame_w + i % self.frame_w) * 8, (fr * self.frame_h + i // self.frame_w) * 8

    def to_dict(self) -> dict:
        return {"frame_w": self.frame_w, "frame_h": self.frame_h, "frames": self.frames,
                "cols": self.cols, "count": self.count}

    @classmethod
    def from_dict(cls, d: dict) -> "Layout":
        return cls(d["frame_w"], d["frame_h"], d["frames"], d["cols"], d["count"])

    @classmethod
    def sheet(cls, count: int, tiles_wide: int = 16) -> "Layout":
        w = max(1, min(tiles_wide, count))
        return cls(w, -(-count // w), 1, 1, count)

    @classmethod
    def frames_of(cls, count: int, fw: int, fh: int, cols: int = 1) -> "Layout":
        per = fw * fh
        if per <= 0 or count % per:
            return cls.sheet(count)
        frames = count // per
        return cls(fw, fh, frames, max(1, min(cols, frames)), count)


def tiles_to_image(tiles: np.ndarray, layout: Layout) -> np.ndarray:
    img = np.zeros((layout.height, layout.width), dtype=np.uint8)
    for t in range(min(len(tiles), layout.count)):
        x, y = layout.tile_pos(t)
        img[y:y + 8, x:x + 8] = tiles[t]
    return img


def image_to_tiles(img: np.ndarray, layout: Layout) -> np.ndarray:
    tiles = np.zeros((layout.count, 8, 8), dtype=np.uint8)
    for t in range(layout.count):
        x, y = layout.tile_pos(t)
        tiles[t] = img[y:y + 8, x:x + 8]
    return tiles


def pad_palette(colors: list[RGB], n: int) -> list[RGB]:
    colors = list(colors[:n])
    while len(colors) < n:
        colors.append((0, 0, 0))
    return colors


# --------------------------------------------------------------------------- PNG I/O

def save_indexed_png(path, pixels: np.ndarray, palette: list[RGB], transparent0: bool = False) -> None:
    h, w = pixels.shape
    img = Image.frombytes("P", (w, h), np.ascontiguousarray(pixels, dtype=np.uint8).tobytes())
    flat = [v for c in palette for v in c[:3]]
    img.putpalette(flat)
    kwargs = {"optimize": False}
    if len(palette) <= 16:
        kwargs["bits"] = 4
    if transparent0:
        kwargs["transparency"] = 0
    img.save(path, format="PNG", **kwargs)


@dataclass
class LoadedImage:
    width: int
    height: int
    indices: np.ndarray | None  # (h, w) palette indices when the PNG is indexed
    palette: list[RGB] | None  # PLTE of an indexed PNG
    rgba: np.ndarray  # (h, w, 4) always available


def load_png(path) -> LoadedImage:
    with Image.open(path) as im:
        im.load()
        w, h = im.size
        if im.mode == "P":
            idx = np.array(im, dtype=np.uint8)
            raw_pal = im.getpalette() or []
            pal = [tuple(raw_pal[i:i + 3]) for i in range(0, len(raw_pal) - 2, 3)]
            rgba = np.array(im.convert("RGBA"), dtype=np.uint8)
            return LoadedImage(w, h, idx, pal, rgba)
        rgba = np.array(im.convert("RGBA"), dtype=np.uint8)
        return LoadedImage(w, h, None, None, rgba)


# --------------------------------------------------------------------------- quantisation

def _nearest(color: tuple[int, int, int], palette: list[RGB], start: int = 0) -> tuple[int, int]:
    best, best_d = start, 1 << 30
    r, g, b = color
    for i in range(start, len(palette)):
        pr, pg, pb = palette[i][:3]
        d = (r - pr) ** 2 * 3 + (g - pg) ** 2 * 4 + (b - pb) ** 2 * 2
        if d < best_d:
            best, best_d = i, d
            if d == 0:
                break
    return best, best_d


def remap_to_palette(rgba: np.ndarray, palette: list[RGB], hint: np.ndarray | None = None,
                     transparent_index: int = 0) -> tuple[np.ndarray, int]:
    """Map RGBA pixels to the nearest palette entry.

    Fully transparent pixels become ``transparent_index``. When ``hint`` (the
    previous indices) is given and the pixel's colour still equals the hinted
    entry, the hinted index is kept so duplicate palette entries stay stable.
    Returns ``(indices, inexact_pixel_count)``.
    """
    h, w = rgba.shape[:2]
    out = np.zeros((h, w), dtype=np.uint8)
    cache: dict[tuple, tuple[int, int]] = {}
    inexact = 0
    for y in range(h):
        row = rgba[y]
        for x in range(w):
            r, g, b, a = (int(v) for v in row[x])
            if a < 128:
                out[y, x] = transparent_index
                continue
            if hint is not None:
                hi = int(hint[y, x])
                if hi < len(palette) and same_color((r, g, b), palette[hi]):
                    out[y, x] = hi
                    continue
            key = (r >> 3, g >> 3, b >> 3)
            res = cache.get(key)
            if res is None:
                exact = [i for i, c in enumerate(palette) if same_color((r, g, b), c)]
                if exact:
                    # Prefer non-transparent indices for opaque pixels
                    res = (exact[1] if exact[0] == transparent_index and len(exact) > 1 else exact[0], 0)
                else:
                    res = _nearest((r, g, b), palette, 1 if len(palette) > 1 else 0)
                cache[key] = res
            out[y, x] = res[0]
            if res[1]:
                inexact += 1
    return out, inexact


def quantize_new_palette(rgba: np.ndarray, ncolors: int = 16,
                         background: RGB | None = None) -> tuple[np.ndarray, list[RGB]]:
    """Build a brand new ``ncolors`` palette (index 0 = transparent) for an RGBA image."""
    h, w = rgba.shape[:2]
    alpha = rgba[:, :, 3] >= 128
    opaque = rgba[alpha][:, :3]
    snapped = (opaque >> 3) << 3
    if len(snapped):
        uniq = np.unique(snapped, axis=0)
    else:
        uniq = np.zeros((0, 3), dtype=np.uint8)
    if len(uniq) <= ncolors - 1:
        colors = [tuple(int(v) for v in c) for c in uniq]
    else:
        src = Image.fromarray(snapped.reshape(1, -1, 3).astype(np.uint8), "RGB")
        q = src.quantize(colors=ncolors - 1, method=Image.Quantize.MEDIANCUT)
        raw = q.getpalette()[: (ncolors - 1) * 3]
        colors = [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
        colors = [((r >> 3) << 3, (g >> 3) << 3, (b >> 3) << 3) for r, g, b in colors]
    bg = background or _pick_background(colors)
    palette = [bg] + colors
    palette = pad_palette(palette, ncolors)
    idx, _ = remap_to_palette(rgba, palette)
    return idx, palette


def _pick_background(colors: list[RGB]) -> RGB:
    """A colour for index 0 that is unlikely to clash with the art (magenta-ish)."""
    for cand in ((248, 0, 248), (0, 248, 248), (0, 248, 0), (248, 248, 0)):
        if not any(same_color(cand, c) for c in colors):
            return cand
    return (248, 0, 248)


def rgb_image_of(indices: np.ndarray, palette: list[RGB]) -> np.ndarray:
    pal = np.array(pad_palette(palette, 256), dtype=np.uint8)
    return pal[indices]

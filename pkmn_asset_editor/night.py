"""Night, in games that tint the overworld by time of day (pokeemerald-expansion's ``OW_ENABLE_DNS``): the colours a
map shows at night, the colours that are lit (a palette's ``.pla``) and the round glows of light objects.

At night the game (``src/overworld.c`` ``gTimeOfDayBlend``, ``src/palette.c`` ``TimeMixPalettes``) multiplies every
colour of an outdoor map's palettes, channel by channel in 5 bits, by 0.456 (red, green) and 0.615 (blue). A colour
whose light bit is set (its index is in the palette's ``.pla``) goes halfway to a light colour instead: the palette's
colour 0 when that is lit too, else the game's warm light (248, 224, 120). Colour 0 itself is never tinted. Towns,
cities and routes are tinted; caves and buildings are not.

A light object (``OBJ_EVENT_GFX_LIGHT_SPRITE``, its kind in ``trainer_sight_or_berry_tree_id``) shows a glow there
after dark, blended over the map (``Weather_SetBlendCoeffs(7, BASE_SHADOW_INTENSITY)``); its colours are never tinted.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from .decomp import DecompError
from .gfx import load_png, pad_palette, read_jasc

NIGHT_TINT = (116, 116, 157)  # Q_8_8(0.456), Q_8_8(0.456), Q_8_8(0.615)
DEFAULT_LIGHT = (248, 224, 120)
LIGHT_GFX = "OBJ_EVENT_GFX_LIGHT_SPRITE"
# the glow pictures by light kind (gFieldEffectLightTemplates: ball, Pokémon Center sign, Poké Mart sign), the offset
# of their top-left corner from the light's block's (SpawnLightSprite: the ball's measured against the game at 22:00,
# the signs' from the ball's and their own centring) and their blend (alpha 7/16 over 12/16)
GLOWS = ((("light", 32), (-8, -9)), (("poke_center_light", 16), (-8, -7)), (("mart_light", 16), (-8, -7)))
GLOW_EVA, GLOW_EVB = 7, 12
NOTE = "# lit at night (pae)"  # gbagfx reads lines of up to 64 characters


def pla_of(pal: Path) -> Path:
    return Path(pal).with_suffix(".pla")


def read_lights(pal: Path) -> list[int]:
    """The colours (by index) the palette's ``.pla`` gives the light bit (none: no ``.pla``), as gbagfx reads it."""
    p = pla_of(pal)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        try:
            out.append(int(s.split()[0], 10))
        except ValueError:
            raise DecompError(f"{p}: {s!r} is not a colour index")
    return sorted(set(out))


def pla_text(cols: list[int]) -> str:
    return NOTE + "\n" + "".join(f"{c}\n" for c in sorted(set(cols)))


def night_palette(cols: list[tuple[int, int, int]], lit) -> list[tuple[int, int, int]]:
    """A 16-colour palette as the game shows it at night (TIME_NIGHT, after 22:00 and before 04:00 the blend is the
    night one alone). Colours are 8-bit; the game works in 5 bits, so the result is too (multiples of 8)."""
    lit = set(lit)
    cols = list(cols) + [(0, 0, 0)] * (16 - len(cols))
    light = [v >> 3 for v in (cols[0] if 0 in lit else DEFAULT_LIGHT)]
    out = []
    for i, c in enumerate(cols[:16]):
        c5 = [int(v) >> 3 for v in c]
        if i == 0:
            n = c5  # copied through
        elif i in lit:
            n = [v + (((l - v) * 16) >> 5) for v, l in zip(c5, light)]
        else:
            n = [min(31, (t * v) >> 8) for t, v in zip(NIGHT_TINT, c5)]
        out.append(tuple(v << 3 for v in n))
    return out


def night_rgb(rgb: np.ndarray) -> np.ndarray:
    """Unlit colours (an array of 8-bit RGB) at night: for sprites drawn over a night map."""
    c5 = rgb[..., :3].astype(np.int32) >> 3
    out = np.minimum(31, (c5 * np.array(NIGHT_TINT)) >> 8) << 3
    return out.astype(np.uint8)


def glow_sprites(root: Path) -> list[Image.Image | None]:
    """The three glow pictures in the game's colours (the light palette; None where the game has none)."""
    pics = root / "graphics/object_events/pics/misc"
    pal_file = root / "graphics/object_events/palettes/light.pal"
    out = []
    for (name, _size), _off in GLOWS:
        f = pics / f"{name}.png"
        if not f.exists():
            out.append(None)
            continue
        img = load_png(f)
        pal = read_jasc(pal_file) if pal_file.exists() else img.palette
        if img.indices is None or not pal:
            out.append(None)
            continue
        idx = img.indices & 0x0F
        rgb = np.array(pad_palette(pal, 16), np.uint8)[idx]
        alpha = np.where(idx == 0, 0, 255).astype(np.uint8)
        out.append(Image.fromarray(np.dstack([rgb, alpha]), "RGBA"))
    return out


def light_kind(obj: dict) -> int:
    try:
        k = int(str(obj.get("trainer_sight_or_berry_tree_id", "0")), 0)
    except ValueError:
        k = 0
    return min(max(k, 0), len(GLOWS) - 1)  # the game clamps an unknown kind to the last template


def draw_glows(im: Image.Image, data: dict, root: Path) -> int:
    """Blend each light object's glow over a night picture of its map (as the GBA blends a semi-transparent sprite:
    min(31, sprite x 7/16 + map x 12/16), in 5 bits). Returns how many it drew."""
    objs = [o for o in data.get("object_events", []) if o.get("graphics_id") == LIGHT_GFX]
    if not objs:
        return 0
    sprites = glow_sprites(root)
    base = np.array(im.convert("RGBA"), np.int32)
    n = 0
    for o in objs:
        k = light_kind(o)
        spr = sprites[k]
        if spr is None:
            continue
        (_name, size), (ox, oy) = GLOWS[k]
        x0, y0 = int(o.get("x", 0)) * 16 + ox, int(o.get("y", 0)) * 16 + oy
        s = np.array(spr, np.int32)
        sx0, sy0 = max(0, -x0), max(0, -y0)
        dx0, dy0 = max(0, x0), max(0, y0)
        w = min(size - sx0, base.shape[1] - dx0)
        h = min(size - sy0, base.shape[0] - dy0)
        if w <= 0 or h <= 0:
            continue
        cut = s[sy0:sy0 + h, sx0:sx0 + w]
        dst = base[dy0:dy0 + h, dx0:dx0 + w]
        mask = cut[..., 3] > 0
        mixed = np.minimum(31, ((cut[..., :3] >> 3) * GLOW_EVA + (dst[..., :3] >> 3) * GLOW_EVB) >> 4) << 3
        dst[..., :3][mask] = mixed[mask]
        n += 1
    im.paste(Image.fromarray(base.astype(np.uint8), "RGBA"))
    return n

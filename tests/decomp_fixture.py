"""A miniature pokeemerald-expansion-style source tree (git repo) for the source-tree tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image

from pkmn_asset_editor.gfx import write_jasc as _write_jasc


def write_jasc(path: Path, colors) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_jasc(path, colors)

PAL_A = [(248, 0, 248)] + [(i * 16, 255 - i * 16, 40) for i in range(1, 16)]
PAL_B = [(0, 248, 248)] + [(255 - i * 16, i * 8, i * 16) for i in range(1, 16)]
ICON_PALS = [[(0, 0, 0)] + [((j * 40 + i * 13) % 256, (i * 16) % 256, j * 50) for i in range(1, 16)] for j in range(3)]


def png(path: Path, w: int, h: int, pal, seed: int = 0, bits_max: int = 16):
    rng = np.random.default_rng(seed)
    idx = np.zeros((h, w), np.uint8)
    for _ in range(5):
        x0, y0 = rng.integers(0, w - 4), rng.integers(0, h - 4)
        idx[y0:y0 + rng.integers(2, 12), x0:x0 + rng.integers(2, 12)] = rng.integers(1, bits_max)
    im = Image.frombytes("P", (w, h), idx.tobytes())
    im.putpalette([v for c in pal for v in c])
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)


def build(root: Path) -> Path:
    g = root / "graphics"
    (root / "src/data/graphics").mkdir(parents=True)
    (root / "src/data/pokemon/species_info").mkdir(parents=True)
    (root / "src/data/object_events").mkdir(parents=True)
    (root / "src/data/tilesets").mkdir(parents=True)
    (root / "include/config").mkdir(parents=True)
    (root / "data/layouts").mkdir(parents=True)
    (root / "Makefile").write_text("all:\n\t@echo built\n")
    (root / "include/config/pokemon.h").write_text("#define P_GBA_STYLE_SPECIES_ICONS FALSE\n")
    (root / "include/fieldmap.h").write_text("#define NUM_TILES_IN_PRIMARY 512\n#define NUM_PALS_IN_PRIMARY 6\n")
    (root / "spritesheet_rules.mk").write_text(
        "OBJEVENTGFXDIR := graphics/object_events/pics\n\n"
        "$(OBJEVENTGFXDIR)/people/hero/walking.4bpp: %.4bpp: %.png\n\t$(GFX) $< $@ -mwidth 2 -mheight 4\n\n"
        "graphics/types/joined.gbapal: graphics/types/a.gbapal graphics/types/b.gbapal\n\t@cat $^ >$@\n")

    # Pokémon
    for i, name in enumerate(("bulbasaur", "pikachu")):
        d = g / "pokemon" / name
        png(d / "anim_front.png", 64, 128, PAL_A, i)
        png(d / "back.png", 64, 64, PAL_A, 10 + i)
        png(d / "icon.png", 32, 64, ICON_PALS[i + 1], 20 + i)
        footprint = Image.new("P", (16, 16))
        footprint.putpalette([0, 0, 0, 255, 255, 255])
        footprint.save(d / "footprint.png")
        write_jasc(d / "normal.pal", PAL_A)
        write_jasc(d / "shiny.pal", PAL_B)
    png(g / "pokemon/pikachu/alola/back.png", 64, 64, PAL_A, 30)  # form without its own palette
    for j, pal in enumerate(ICON_PALS):
        write_jasc(g / f"pokemon/icon_palettes/pal{j}.pal", pal)
    lines = []
    for name in ("Bulbasaur", "Pikachu"):
        low = name.lower()
        lines += [f'const u32 gMonFrontPic_{name}[] = INCGFX_U32("graphics/pokemon/{low}/anim_front.png", ".4bpp.smol");',
                  f'const u16 gMonPalette_{name}[] = INCGFX_U16("graphics/pokemon/{low}/normal.pal", ".gbapal");',
                  f'const u32 gMonBackPic_{name}[] = INCGFX_U32("graphics/pokemon/{low}/back.png", ".4bpp.smol");',
                  f'const u16 gMonShinyPalette_{name}[] = INCGFX_U16("graphics/pokemon/{low}/shiny.pal", ".gbapal");',
                  f'const u8 gMonIcon_{name}[] = INCGFX_U8("graphics/pokemon/{low}/icon.png", ".4bpp");',
                  f'const u8 gMonFootprint_{name}[] = INCGFX_U8("graphics/pokemon/{low}/footprint.png", ".1bpp");']
    lines.append('const u32 gMonBackPic_PikachuAlola[] = INCGFX_U32("graphics/pokemon/pikachu/alola/back.png", ".4bpp.smol");')
    lines.append('// const u32 gMonBackPic_Ghost[] = INCGFX_U32("graphics/pokemon/ghost/back.png", ".4bpp.smol");')
    (root / "src/data/graphics/pokemon.h").write_text("\n".join(lines) + "\n")
    (root / "src/graphics.c").write_text(
        "const u16 gMonIconPalettes[][16] =\n{\n" +
        "".join(f'    INCGFX_U16("graphics/pokemon/icon_palettes/pal{j}.pal", ".gbapal"),\n' for j in range(3)) + "};\n"
        'const u32 gBattleAnimSpriteGfx_Bone[] = INCGFX_U32("graphics/battle_anims/sprites/bone.png", ".4bpp.smol");\n'
        'const u16 gBattleAnimSpritePal_Bone[] = INCGFX_U16("graphics/battle_anims/sprites/bone.png", ".gbapal");\n'
        'const u32 gBattleAnimBgImage_Dark[] = INCGFX_U32("graphics/battle_anims/backgrounds/dark.png", ".4bpp.smol");\n'
        'const u16 gBattleAnimBgPalette_Dark[] = INCGFX_U16("graphics/battle_anims/backgrounds/dark.png", ".gbapal");\n'
        'const u32 gBattleAnimBgTilemap_Dark[] = INCGFX_U32("graphics/battle_anims/backgrounds/dark.bin", ".smolTM");\n'
        'const u16 gJoined_Pal[] = INCBIN_U16("graphics/types/joined.gbapal");\n')
    (root / "src/data/pokemon/species_info/gen_1_families.h").write_text(
        "    [SPECIES_BULBASAUR] = {\n        .iconSprite = gMonIcon_Bulbasaur,\n"
        "        .iconPalIndex = P_GBA_STYLE_SPECIES_ICONS ? 0 : 1,\n    },\n"
        "    [SPECIES_PIKACHU] = {\n        .iconSprite = gMonIcon_Pikachu,\n        .iconPalIndex = 2,\n    },\n")
    png(g / "battle_anims/sprites/bone.png", 32, 32, PAL_B, 40)
    # tilemap background: 24 tiles + a 32x32 map that reuses them (some flipped)
    rng = np.random.default_rng(7)
    png(g / "battle_anims/backgrounds/dark.png", 64, 24, PAL_A, 41)
    entries = [int(rng.integers(0, 24)) | (int(rng.integers(0, 2)) << 10) for _ in range(1024)]
    (g / "battle_anims/backgrounds/dark.bin").write_bytes(b"".join(e.to_bytes(2, "little") for e in entries))

    # trainers / items: palettes paired by symbol name
    png(g / "trainers/front_pics/hiker.png", 64, 64, PAL_B, 50)
    write_jasc(g / "trainers/palettes/hiker.pal", PAL_B)
    png(g / "items/icons/potion.png", 24, 24, PAL_A, 51)
    write_jasc(g / "items/icon_palettes/potion.pal", PAL_A)
    write_jasc(g / "items/icon_palettes/super_potion.pal", PAL_B)
    (root / "src/data/graphics/trainers.h").write_text(
        'const u32 gTrainerFrontPic_Hiker[] = INCGFX_U32("graphics/trainers/front_pics/hiker.png", ".4bpp.smol");\n'
        'const u16 gTrainerPalette_Hiker[] = INCGFX_U16("graphics/trainers/palettes/hiker.pal", ".gbapal");\n')
    (root / "src/data/graphics/items.h").write_text(
        'const u32 gItemIcon_Potion[] = INCGFX_U32("graphics/items/icons/potion.png", ".4bpp.smol");\n'
        'const u16 gItemIconPalette_Potion[] = INCGFX_U16("graphics/items/icon_palettes/potion.pal", ".gbapal");\n'
        'const u16 gItemIconPalette_SuperPotion[] = INCGFX_U16("graphics/items/icon_palettes/super_potion.pal", ".gbapal");\n')
    (root / "src/data/items.h").write_text(
        "    [ITEM_POTION] = { .iconPic = gItemIcon_Potion, .iconPalette = gItemIconPalette_Potion, },\n"
        "    [ITEM_SUPER_POTION] = { .iconPic = gItemIcon_Potion, .iconPalette = gItemIconPalette_SuperPotion, },\n")

    # object events: pic -> pic table -> graphics info .paletteTag -> palette table
    png(g / "object_events/pics/people/hero/walking.png", 16 * 9, 32, PAL_B, 60)
    write_jasc(g / "object_events/palettes/hero.pal", PAL_A)
    (root / "src/data/object_events/object_event_graphics.h").write_text(
        'const u32 gObjectEventPic_HeroNormal[] = INCBIN_U32("graphics/object_events/pics/people/hero/walking.4bpp");\n'
        'const u16 gObjectEventPal_Hero[] = INCGFX_U16("graphics/object_events/palettes/hero.pal", ".gbapal");\n')
    (root / "src/data/object_events/object_event_pic_tables.h").write_text(
        "static const struct SpriteFrameImage sPicTable_HeroNormal[] = {\n"
        "    overworld_ascending_frames(gObjectEventPic_HeroNormal, 2, 4),\n};\n")
    (root / "src/data/object_events/object_event_graphics_info.h").write_text(
        "const struct ObjectEventGraphicsInfo gObjectEventGraphicsInfo_HeroNormal = {\n"
        "    .paletteTag = OBJ_EVENT_PAL_TAG_HERO,\n    .width = 16,\n    .height = 32,\n"
        "    .images = sPicTable_HeroNormal,\n};\n")
    (root / "src/event_object_movement.c").write_text("    {gObjectEventPal_Hero, OBJ_EVENT_PAL_TAG_HERO},\n")

    # tilesets: primary + secondary paired by a layout
    gray = [(i * 16, i * 16, i * 16) for i in range(16)]
    ts_lines = []
    for kind, name, sym in (("primary", "general", "General"), ("secondary", "town", "Town")):
        d = root / "data/tilesets" / kind / name
        png(d / "tiles.png", 128, 32, gray, 70 if kind == "primary" else 71)
        for b in range(16):
            write_jasc(d / "palettes" / f"{b:02d}.pal", [(0, 0, 0)] + [((b * 30 + i * 9) % 256, (i * 15) % 256, b * 15) for i in range(1, 16)])
        base = 512 if kind == "secondary" else 0
        meta = []
        for m in range(32):
            for q in range(8):
                t = base + (m * 2 + q) % 64
                bank = (6 + m % 3) if kind == "secondary" else m % 3
                meta.append(t | (bank << 12))
        (d / "metatiles.bin").write_bytes(b"".join(v.to_bytes(2, "little") for v in meta))
        ts_lines.append(f'const u32 gTilesetTiles_{sym}[] = INCGFX_U32("data/tilesets/{kind}/{name}/tiles.png", ".4bpp.smol");')
        ts_lines.append(f"const u16 gTilesetPalettes_{sym}[][16] =\n{{")
        ts_lines += [f'    INCGFX_U16("data/tilesets/{kind}/{name}/palettes/{b:02d}.pal", ".gbapal"),' for b in range(16)]
        ts_lines.append("};")
    (root / "src/data/tilesets/graphics.h").write_text("\n".join(ts_lines) + "\n")
    headers, mt = [], []
    for kind, name, sym in (("primary", "general", "General"), ("secondary", "town", "Town")):
        d = f"data/tilesets/{kind}/{name}"
        (root / d / "metatile_attributes.bin").write_bytes(bytes(2 * 32))
        headers.append(f"const struct Tileset gTileset_{sym} =\n{{\n    .isCompressed = TRUE,\n"
                       f"    .isSecondary = {'TRUE' if kind == 'secondary' else 'FALSE'},\n"
                       f"    .tiles = gTilesetTiles_{sym},\n    .palettes = gTilesetPalettes_{sym},\n"
                       f"    .metatiles = gMetatiles_{sym},\n    .metatileAttributes = gMetatileAttributes_{sym},\n}};\n")
        mt.append(f'const u16 gMetatiles_{sym}[] = INCBIN_U16("{d}/metatiles.bin");')
        mt.append(f'const u16 gMetatileAttributes_{sym}[] = INCBIN_U16("{d}/metatile_attributes.bin");')
    (root / "src/data/tilesets/headers.h").write_text("".join(headers))
    (root / "src/data/tilesets/metatiles.h").write_text("\n".join(mt) + "\n")

    # maps: a town (using a secondary metatile) and a house interior
    (root / "data/maps/Town").mkdir(parents=True)
    (root / "data/maps/Town_House1").mkdir(parents=True)
    for lname, w, h in (("Town", 10, 8), ("House1", 6, 5)):
        ld = root / "data/layouts" / lname
        ld.mkdir(parents=True)
        blocks = np.full((h, w), 1 | (3 << 12), dtype="<u2")
        blocks[0, :] = 2 | (1 << 10) | (3 << 12)
        blocks[2, 3] = 512 + 5 | (3 << 12)
        (ld / "map.bin").write_bytes(blocks.tobytes())
        (ld / "border.bin").write_bytes(np.full(4, 1, dtype="<u2").tobytes())
    layouts = {"layouts_table_label": "gMapLayouts", "layouts": [
        {"id": "LAYOUT_TOWN", "name": "Town_Layout", "width": 10, "height": 8, "primary_tileset": "gTileset_General",
         "secondary_tileset": "gTileset_Town", "border_filepath": "data/layouts/Town/border.bin",
         "blockdata_filepath": "data/layouts/Town/map.bin", "layout_version": "emerald"},
        {"id": "LAYOUT_HOUSE1", "name": "House1_Layout", "width": 6, "height": 5, "primary_tileset": "gTileset_General",
         "secondary_tileset": "gTileset_Town", "border_filepath": "data/layouts/House1/border.bin",
         "blockdata_filepath": "data/layouts/House1/map.bin", "layout_version": "emerald"}]}
    (root / "data/layouts/layouts.json").write_text(json.dumps(layouts, indent=2) + "\n")
    (root / "data/maps/map_groups.json").write_text(json.dumps(
        {"group_order": ["gMapGroup_Towns", "gMapGroup_Indoor"], "gMapGroup_Towns": ["Town"],
         "gMapGroup_Indoor": ["Town_House1"]}, indent=2) + "\n")
    town = {"id": "MAP_TOWN", "name": "Town", "layout": "LAYOUT_TOWN", "music": "MUS_TOWN", "weather": "WEATHER_SUNNY",
            "map_type": "MAP_TYPE_TOWN", "allow_cycling": True, "connections": [],
            "object_events": [{"local_id": "LOCALID_TOWN_HERO", "graphics_id": "OBJ_EVENT_GFX_HERO", "x": 4, "y": 4,
                               "elevation": 3, "movement_type": "MOVEMENT_TYPE_FACE_DOWN", "movement_range_x": 0,
                               "movement_range_y": 0, "trainer_type": "TRAINER_TYPE_NONE",
                               "trainer_sight_or_berry_tree_id": "0", "script": "Town_EventScript_Hero",
                               "flag": "FLAG_HIDE_HERO"}],
            "warp_events": [{"x": 2, "y": 2, "elevation": 0, "dest_map": "MAP_TOWN_HOUSE1", "dest_warp_id": "0"}],
            "coord_events": [], "bg_events": []}
    house = {"id": "MAP_TOWN_HOUSE1", "name": "Town_House1", "layout": "LAYOUT_HOUSE1", "music": "MUS_TOWN",
             "weather": "WEATHER_NONE", "map_type": "MAP_TYPE_INDOOR", "allow_cycling": False, "connections": None,
             "object_events": [{"local_id": "LOCALID_HOUSE_MOM", "graphics_id": "OBJ_EVENT_GFX_HERO", "x": 2, "y": 1,
                                "elevation": 3, "movement_type": "MOVEMENT_TYPE_FACE_DOWN", "movement_range_x": 0,
                                "movement_range_y": 0, "trainer_type": "TRAINER_TYPE_NONE",
                                "trainer_sight_or_berry_tree_id": "0", "script": "Town_House1_EventScript_Mom",
                                "flag": "0"}],
             "warp_events": [{"x": 2, "y": 4, "elevation": 0, "dest_map": "MAP_TOWN", "dest_warp_id": "0"}],
             "coord_events": [], "bg_events": []}
    (root / "data/maps/Town/map.json").write_text(json.dumps(town, indent=2) + "\n")
    (root / "data/maps/Town_House1/map.json").write_text(json.dumps(house, indent=2) + "\n")
    (root / "data/maps/Town/scripts.inc").write_text(
        "Town_MapScripts::\n\t.byte 0\n\nTown_EventScript_Hero::\n\tmsgbox Town_Text_Hero, MSGBOX_NPC\n\tend\n\n"
        "Town_Text_Hero:\n\t.string \"Hi!$\"\n")
    (root / "data/maps/Town_House1/scripts.inc").write_text(
        "Town_House1_MapScripts::\n\t.byte 0\n\nTown_House1_EventScript_Mom::\n\tapplymovement LOCALID_HOUSE_MOM, "
        "Common_Movement_FacePlayer\n\tmsgbox Town_House1_Text_Mom, MSGBOX_NPC\n\tend\n\n"
        "Town_House1_Text_Mom:\n\t.string \"Welcome home!$\"\n")
    (root / "data/scripts").mkdir(parents=True)
    (root / "data/scripts/common.inc").write_text("Common_Movement_FacePlayer::\n\tstep_end\n")
    (root / "include/constants").mkdir(parents=True, exist_ok=True)
    (root / "include/constants/flags.h").write_text("#define FLAG_HIDE_HERO 0x20\n#define FLAG_TEST 0x21\n")
    (root / "src/data/object_events/object_event_graphics_info_pointers.h").write_text(
        "    [OBJ_EVENT_GFX_HERO] = &gObjectEventGraphicsInfo_HeroNormal,\n")

    for cmd in (["git", "init", "-q"], ["git", "add", "-A"],
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        subprocess.run(cmd, cwd=root, check=True)
    return root

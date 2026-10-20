"""Minimal MCP (Model Context Protocol) server over stdio.

Lets an AI assistant browse, look at, edit and rebuild the project with tools.
Register it with e.g. ``claude mcp add pae -- pae mcp --project /path/to/project``.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
from pathlib import Path

from PIL import Image

from . import __version__
from .decomp import DecompError
from .previews import contact_sheet
from .project import ProjectError

ERRORS = (ProjectError, DecompError, ValueError, KeyError, OSError)

PROTOCOL = "2025-06-18"


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or [], "additionalProperties": False}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

TOOLS = [
    ("list_assets", "List assets in the project. Filter by category (pokemon, trainers, items, battle_anims, "
     "battle_backgrounds, overworld, tilesets, unknown), kind (sprite, palette, tileset, bg) or a text query.",
     _schema({"category": S, "kind": S, "query": S, "limit": I})),
    ("describe_asset", "Full details and editing constraints (size, colours, frames, shared palettes) for one asset.",
     _schema({"id": S}, ["id"])),
    ("view_asset", "Return an image of the asset as it will look in the ROM. Optional palette id to preview "
     "another palette (e.g. the shiny one) and an integer zoom.", _schema({"id": S, "palette": S, "scale": I}, ["id"])),
    ("view_contact_sheet", "Render many assets whose id starts with a prefix onto one labelled image.",
     _schema({"prefix": S, "limit": I, "scale": I}, ["prefix"])),
    ("get_pixels", "Read colour indices as text rows of hex digits (one char per pixel for sprites; two per pixel "
     "for tilesets/backgrounds where value = bank*16+colour). Optional region x,y,w,h.",
     _schema({"id": S, "x": I, "y": I, "w": I, "h": I}, ["id"])),
    ("paint_pixels", "Write colour indices. 'rows' are hex strings (same format as get_pixels) placed at x,y; "
     "'.' leaves a pixel unchanged. 'rects' fills rectangles: [{x,y,w,h,index}].",
     _schema({"id": S, "x": I, "y": I, "rows": {"type": "array", "items": S},
              "rects": {"type": "array", "items": {"type": "object"}}}, ["id"])),
    ("import_image", "Replace an asset with an image file (path) or base64 PNG. Colours are mapped to the "
     "palette; set new_palette to build a new palette from the art, resize to scale it to fit.",
     _schema({"id": S, "path": S, "png_base64": S, "resize": B, "new_palette": B}, ["id"])),
    ("set_palette", "Overwrite a palette. colors: list of '#rrggbb' strings or [r,g,b] (must match the palette size).",
     _schema({"id": S, "colors": {"type": "array"}}, ["id", "colors"])),
    ("revert_asset", "Restore an asset to the original ROM contents.", _schema({"id": S}, ["id"])),
    # ---- maps, events and scripts (source trees)
    ("list_maps", "List maps (name, id, group, layout). Optional text query.", _schema({"query": S})),
    ("view_map", "Image of a map with its events drawn (NPC sprites; W=warp, T=trigger, S=sign + index). "
     "One block = 16px.", _schema({"name": S, "scale": I, "events": B}, ["name"])),
    ("get_map", "A map's map.json (header, connections, object/warp/coord/bg events) plus layout size and tilesets.",
     _schema({"name": S}, ["name"])),
    ("view_metatiles", "Image of every metatile this map can use, 8 per row, rows labelled with the hex id of "
     "their first metatile. Use the ids with set_blocks.", _schema({"name": S, "scale": I}, ["name"])),
    ("get_blocks", "Read blocks as text rows. Each block is 'mmmCE': metatile id (3 hex digits), collision "
     "(0 passable, 1 blocked), elevation (hex). Optional region.", _schema({"name": S, "x": I, "y": I, "w": I, "h": I},
                                                                            ["name"])),
    ("set_blocks", "Write blocks. rows: strings of space-separated cells, 'mmm' (metatile only) or 'mmmCE' "
     "(metatile+collision+elevation); '.' leaves a block unchanged. Placed with the top-left at x,y.",
     _schema({"name": S, "x": I, "y": I, "rows": {"type": "array", "items": S}}, ["name", "rows"])),
    ("copy_blocks", "Copy a rectangle of blocks (e.g. a house) from one map to another (or the same) map.",
     _schema({"source": S, "x": I, "y": I, "w": I, "h": I, "dest": S, "dx": I, "dy": I},
             ["source", "x", "y", "w", "h", "dest", "dx", "dy"])),
    ("edit_map", "Change a map: set header fields (music, weather, map_type, …), and add/update/delete events. "
     "events: [{op:'add'|'update'|'delete', kind:'object_events'|'warp_events'|'coord_events'|'bg_events', "
     "index (update/delete), fields:{...}}]. Returns validation warnings.",
     _schema({"name": S, "header": {"type": "object"}, "events": {"type": "array", "items": {"type": "object"}}},
             ["name"])),
    ("get_script", "The map's scripts file (scripts.inc or scripts.pory) as text.", _schema({"name": S}, ["name"])),
    ("write_script", "Replace the map's scripts file. Keep existing labels that events or other maps use.",
     _schema({"name": S, "text": S}, ["name", "text"])),
    ("preview_text", "How a map's dialogue looks in the game's own message box (image), with problems: lines too "
     "wide for the box, a 3rd line without \\l or \\p, characters the font lacks, missing $. Give `label` for "
     "one text, or none for all texts with problems.", _schema({"name": S, "label": S}, ["name"])),
    ("new_script", "Append a ready-made script (npc / sign / trigger) that shows a message; returns its label "
     "to put in an event's 'script' field.", _schema({"name": S, "kind": S, "text": S}, ["name"])),
    ("duplicate_map", "Create a new map as a copy of an existing one (e.g. copy a house interior to add a new "
     "house). Scripts and local ids are renamed; copy_layout (default true) gives it its own blocks.",
     _schema({"source": S, "new_name": S, "group": S, "copy_layout": B, "map_id": S}, ["source", "new_name"])),
    ("link_warps", "Connect warp `warp` of `map` with warp `dest_warp` of `dest_map` in both directions.",
     _schema({"map": S, "warp": I, "dest_map": S, "dest_warp": I}, ["map", "warp", "dest_map", "dest_warp"])),
    ("resize_map", "Change a map layout's width/height (new area filled with the border block).",
     _schema({"name": S, "width": I, "height": I}, ["name", "width", "height"])),
    ("playtest_start", "Build the ROM with `make` (unless build=false) and start a play-test: a fresh game, or "
     "`checkpoint` (a saved game: story progress, party, bag). Optional `state` lines applied before arriving: "
     "FLAG_X, !FLAG_X, VAR_X=3, ITEM_X*5. `chapter` (number from list_story_chapters) first gives everything a "
     "player has by the end of that chapter: needed items, badges/flags, and with `extras` item balls, hidden items "
     "and gifts too. `team`: Pokémon to add, e.g. 'SPECIES_MUDKIP lv20 ITEM_ORAN_BERRY MOVE_TACKLE MOVE_WATER_GUN' "
     "(clear_team empties the team first). Warps to `map` at x,y (default: its first warp; with a checkpoint and no "
     "map, stays where it was saved). `script` starts a script label on arrival. Returns the screen.",
     _schema({"map": S, "x": I, "y": I, "build": B, "checkpoint": S, "state": {"type": "array", "items": S},
              "script": S, "chapter": I, "extras": B, "team": {"type": "array", "items": S}, "clear_team": B})),
    ("playtest_save_checkpoint", "Save the running play-test like the in-game Save and keep it as a named "
     "checkpoint to start later tests from (survives rebuilds).", _schema({"name": S, "note": S}, ["name"])),
    ("list_checkpoints", "Saved checkpoints (name, map, note).", _schema({})),
    ("playtest_run_script", "Start any script label in the running play-test (event, cutscene, sign) and return "
     "the screen.", _schema({"label": S, "frames": I}, ["label"])),
    ("playtest_set_state", "Set story state in the running play-test: FLAG_X, !FLAG_X, VAR_X=3, ITEM_X*5, "
     "SPECIES_X lv20 [ITEM_held] [MOVE_…] (add a Pokémon), CLEAR_TEAM. NPCs "
     "and map scripts only react after the next map load (playtest_start does that for you).",
     _schema({"state": {"type": "array", "items": S}}, ["state"])),
    ("playtest_press", "Press buttons in the running play-test and return the screen. inputs like "
     "'UP, wait 10, A, wait 60' (KEY*N holds N frames; 16 frames = one step). record=true also returns a strip "
     "of frames.", _schema({"inputs": S, "record": B}, ["inputs"])),
    ("tileset_info", "The map's primary/secondary tilesets: metatile and tile counts and limits, palettes.",
     _schema({"name": S}, ["name"])),
    ("get_metatile", "One metatile: 8 tile entries (0-3 bottom layer TL,TR,BL,BR; 4-7 top layer) with tile id, "
     "hflip, vflip, palette; plus behavior (MB_* index) and layer_type (0 normal, 1 covered, 2 split).",
     _schema({"name": S, "id": I}, ["name", "id"])),
    ("set_metatile", "Change a metatile's entries (all 8), behavior (name like MB_NORMAL or number) and/or layer_type.",
     _schema({"name": S, "id": I, "entries": {"type": "array", "items": {"type": "object"}}, "behavior": S,
              "layer_type": S}, ["name", "id"])),
    ("add_metatiles", "Append metatiles to the map's secondary tileset (blank, or copies of copy_from); returns ids.",
     _schema({"name": S, "count": I, "copy_from": I}, ["name"])),
    ("view_tiles", "Image of every 8x8 tile the map can use, 16 per row, coloured with palette `bank`. Tile id = "
     "row*16 + column (secondary tiles start at NUM_TILES_IN_PRIMARY).", _schema({"name": S, "bank": I}, ["name"])),
    ("import_art_as_metatiles", "Turn artwork (a building, sign, statue…) into new tiles + metatiles in the map's "
     "secondary tileset. Reuses existing tiles (incl. flips). base_metatile puts that metatile's ground under "
     "transparent pixels. Returns the new metatile ids as rows for set_blocks; place them with set_blocks and "
     "paint collision. Then playtest to see it in game.",
     _schema({"name": S, "path": S, "png_base64": S, "base_metatile": I, "bank": I, "new_palette": B,
              "behavior": S}, ["name"])),
    ("list_story_chapters", "The story chapters in order (each: name, maps, flags turned on when it's done) and the "
     "maps not placed in any chapter yet. Use the chapter number with playtest_start(chapter=…).", _schema({})),
    ("story_items", "The items a player has by the end of a chapter (number from list_story_chapters): needed ones "
     "(key items, HMs) and, with extras, item balls / hidden items / gifts, with the map each comes from.",
     _schema({"chapter": I, "extras": B}, ["chapter"])),
    ("set_map_chapter", "Put a map (and its inside maps) in a story chapter (number), or 0 to take it out.",
     _schema({"map": S, "chapter": I}, ["map", "chapter"])),
    ("story_flag", "Which events (map + script) turn a flag/variable on or off or set it, and which events need it "
     "or check it: follow how the story is chained together.", _schema({"name": S}, ["name"])),
    ("suggest_map_chapters", "Suggest a story chapter for maps not in one (from the doors/edges to placed maps); "
     "apply=true puts them there.", _schema({"apply": B})),
    ("playtest_team", "The running play-test's team: species, level, held item and moves (as numbers).",
     _schema({})),
    ("list_trainers", "Trainers (id, name, class, team) from src/data/trainers.party, optionally filtered by text.",
     _schema({"query": S})),
    ("get_trainer", "One trainer: fields (Name, Class, Items, Double Battle, AI…), the team (species, level, item, "
     "moves, other lines like IVs/Nature) and the maps whose scripts battle them.", _schema({"id": S}, ["id"])),
    ("set_trainer", "Change a trainer: `mons` replaces the whole team (each {species, level, item, moves[], extra[]}; "
     "names like 'Geodude'/'Rock Tomb' or constants), `fields` replaces fields ([{key, value}]).",
     _schema({"id": S, "mons": {"type": "array", "items": {"type": "object"}},
              "fields": {"type": "array", "items": {"type": "object"}}}, ["id"])),
    ("get_wild", "A map's wild Pokémon: for grass, surfing, rock smash and fishing, the encounter rate and each "
     "slot's species, levels and chance.", _schema({"map": S}, ["map"])),
    ("set_wild", "Set one kind of wild encounter on a map: kind (land_mons, water_mons, rock_smash_mons, "
     "fishing_mons), encounter_rate, and mons with exactly one {species, min_level, max_level} per slot "
     "(get_wild shows how many). Empty mons removes that kind.",
     _schema({"map": S, "kind": S, "encounter_rate": I, "mons": {"type": "array", "items": {"type": "object"}}},
             ["map", "kind"])),
    ("get_species", "A Pokémon's data: name, base stats, types, abilities, catch rate, EXP, evolutions, level-up "
     "moves and its Pokédex entry (category, height, weight, description).",
     _schema({"id": S}, ["id"])),
    ("set_species", "Change a Pokémon: baseHP/baseAttack/baseDefense/baseSpAttack/baseSpDefense/baseSpeed, "
     "catchRate, expYield, types [TYPE_X, TYPE_Y], abilities [3], evolutions [{method, param, target}], "
     "learnset [{level, move}] (replaces the level-up moves), name, category, height, weight, description "
     "(Pokédex), teachable {MOVE_X: true/false} (TM/HM/tutor moves), egg_moves [MOVE_X].",
     _schema({"id": S, "changes": {"type": "object"}}, ["id", "changes"])),
    ("list_items", "Items (id, name), optionally filtered by text.", _schema({"query": S})),
    ("get_item", "One item: name, price, description, pocket, hold effect, its icon and the shops that sell it.",
     _schema({"item": S}, ["item"])),
    ("set_item", "Change an item: name, price, description (real newlines; keep to the lines and width other "
     "descriptions use), pocket (POCKET_X), holdEffectParam.", _schema({"item": S, "changes": {"type": "object"}},
                                                                     ["item", "changes"])),
    ("list_moves", "Moves (id, name), optionally filtered by text.", _schema({"query": S})),
    ("get_move", "One move: name, description, power, accuracy, pp, priority, type, category, effect, target, "
     "its TM/HM number and which Pokémon learn it by level.", _schema({"move": S}, ["move"])),
    ("set_move", "Change a move: name, description, power, accuracy, pp, priority, type (TYPE_X), category "
     "(DAMAGE_CATEGORY_X), effect (EFFECT_X), target, makesContact.",
     _schema({"move": S, "changes": {"type": "object"}}, ["move", "changes"])),
    ("get_tms", "Which move each TM and HM teaches, the move tutors in scripts, and how many TM/HM items exist.",
     _schema({})),
    ("set_tms", "Replace the TM list and/or the HM list (moves in order: the first is TM01). TM items, item balls "
     "and shops that use them are renamed to match.", _schema({"tms": {"type": "array", "items": S},
                                                              "hms": {"type": "array", "items": S}})),
    ("get_teachable", "Which TM/HM and tutor moves a Pokémon can learn (change with set_species "
     "{teachable: {MOVE_X: true}}); also its egg moves.", _schema({"id": S}, ["id"])),
    ("list_marts", "Every Poké Mart list (label, map, items).", _schema({})),
    ("difficulty_report", "Trainer levels (strongest Pokémon), bosses and wild grass/cave levels per story chapter, "
     "plus warnings: trainers stronger than the chapter's boss, level spikes, too-easy trainers, bosses weaker "
     "than the previous one, wild Pokémon above the boss.", _schema({})),
    ("set_mart", "Set what a Poké Mart list sells (items in order).",
     _schema({"label": S, "items": {"type": "array", "items": S}}, ["label", "items"])),
    ("playtest_battle", "In the running play-test, start a battle now: against `trainer` (TRAINER_X) or a wild "
     "`species` at `level`, with the current team. Returns the screen; continue with playtest_press.",
     _schema({"trainer": S, "species": S, "level": I}, [])),
    ("list_tileset_anims", "Tileset animations (water, flowers, waterfalls…), all or those `map` shows: id, frames "
     "(PNG files), play order, speed (game frames per animation frame, 60 = 1 s), animated tile range.",
     _schema({"map": S})),
    ("view_tileset_anim", "Image of an animation's frames in their in-game colours, plus its details and (with "
     "`map`) which metatiles it animates.", _schema({"id": S, "map": S}, ["id"])),
    ("edit_tileset_anim", "Change an animation's speed (game frames per animation frame) and/or play order "
     "(indexes into its frames, e.g. [0,1,0,2]).",
     _schema({"id": S, "speed": I, "order": {"type": "array", "items": I}}, ["id"])),
    ("set_anim_frame", "Replace one frame's art (an image the size of the frame; colours are matched to the "
     "palettes the animated tiles use on `map`).", _schema({"id": S, "frame": I, "path": S, "png_base64": S,
                                                             "map": S}, ["id", "frame"])),
    ("add_anim_frame", "Add a frame to an animation: a copy of frame `copy_of` (default the last) or an image; "
     "inserted in the play order at `position` (default the end).",
     _schema({"id": S, "copy_of": I, "position": I, "path": S, "png_base64": S, "map": S}, ["id"])),
    ("remove_anim_frame", "Remove a frame from an animation's play order.", _schema({"id": S, "frame": I},
                                                                                     ["id", "frame"])),
    ("create_tileset_anim", "Animate `count` tiles starting at global tile id `tile` (see view_tiles) of the map's "
     "tilesets, e.g. your own water. Frames start as copies of the current tiles; redraw them with set_anim_frame.",
     _schema({"map": S, "name": S, "tile": I, "count": I, "frames": I, "speed": I}, ["map", "name", "tile", "count"])),
    ("find_constants", "Look up constant names by prefix, e.g. OBJ_EVENT_GFX_, MOVEMENT_TYPE_, FLAG_, VAR_, MUS_, "
     "ITEM_, MAP_, plus an optional substring filter.", _schema({"prefix": S, "query": S}, ["prefix"])),
    ("list_save_points", "Save points (git commits) of the game folder, newest first, and the files changed since "
     "the last one.", _schema({})),
    ("make_save_point", "Commit every change as a save point with a plain-language message. Only when the user asks "
     "for it.", _schema({"message": S}, ["message"])),
    ("status", "List assets that were edited.", _schema({})),
    ("validate", "Check every edit (sizes, colours, conflicts, tile limits) without writing a ROM.", _schema({})),
    ("build_rom", "ROM project: write the edited ROM and UPS/IPS patches. Source tree: check the edits, then run "
     "`make` and return its output.", _schema({"output": S})),
]


MAP_TOOLS = {"list_maps", "view_map", "get_map", "view_metatiles", "get_blocks", "set_blocks", "copy_blocks", "edit_map",
             "get_script", "write_script", "new_script", "preview_text", "duplicate_map", "link_warps", "resize_map",
             "find_constants", "playtest_start", "playtest_press", "tileset_info", "get_metatile", "set_metatile",
             "add_metatiles", "view_tiles", "import_art_as_metatiles", "playtest_save_checkpoint",
             "list_checkpoints", "playtest_run_script", "playtest_set_state", "list_tileset_anims",
             "view_tileset_anim", "edit_tileset_anim", "set_anim_frame", "add_anim_frame", "remove_anim_frame",
             "create_tileset_anim", "list_story_chapters", "story_items", "set_map_chapter", "playtest_team",
             "list_trainers", "get_trainer", "set_trainer", "get_wild", "set_wild", "get_species", "set_species",
             "playtest_battle", "story_flag", "suggest_map_chapters", "list_items", "get_item", "set_item", "list_moves", "get_move", "set_move", "get_tms", "set_tms",
             "get_teachable", "list_marts", "set_mart", "difficulty_report"}


class Server:
    def __init__(self, proj):
        self.proj = proj

    # ------------------------------------------------------------------ tools
    def _png(self, im: Image.Image) -> dict:
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/png"}

    def call(self, name: str, args: dict) -> list[dict]:
        proj = self.proj
        txt = lambda obj: [{"type": "text", "text": obj if isinstance(obj, str) else json.dumps(obj, indent=1)}]
        if name == "list_assets":
            rows = []
            q = (args.get("query") or "").lower()
            for a in sorted(proj.assets.values(), key=lambda a: a.id):
                if args.get("category") and a.category != args["category"]:
                    continue
                if args.get("kind") and a.kind != args["kind"]:
                    continue
                if q and q not in (a.id + " " + a.label + " " + " ".join(a.aliases)).lower():
                    continue
                rows.append({"id": a.id, "kind": a.kind, "label": a.label, "aliases": len(a.aliases)})
            limit = args.get("limit") or 200
            return txt({"total": len(rows), "assets": rows[:limit]})
        if name == "describe_asset":
            return txt(proj.describe(proj.get(args["id"])))
        if name == "view_asset":
            a = proj.get(args["id"])
            pal = proj.get(args["palette"]).id if args.get("palette") else None
            im = proj.render_image(a, palette_override=pal)
            s = max(1, min(int(args.get("scale") or (4 if im.width <= 64 else 2)), 8))
            im = im.resize((im.width * s, im.height * s), Image.NEAREST)
            return [self._png(im), *txt(f"{a.id}: {im.width // s}x{im.height // s} px (shown at {s}x)")]
        if name == "view_contact_sheet":
            pre = args["prefix"].rstrip("/")
            sel = [a for a in sorted(proj.assets.values(), key=lambda a: a.id)
                   if (a.id.startswith(pre) or a.category == pre) and a.kind != "palette"][: args.get("limit") or 60]
            return [self._png(contact_sheet(proj, sel, scale=args.get("scale") or 1, max_width=1200)),
                    *txt([a.id for a in sel])]
        if name == "get_pixels":
            a = proj.get(args["id"])
            if a.kind == "palette":
                raise ProjectError("palettes have no pixels; use describe_asset")
            indices, _plte, _t = proj.view(a)
            hgt, wid = indices.shape
            x, y = args.get("x", 0), args.get("y", 0)
            w, h = args.get("w") or wid - x, args.get("h") or hgt - y
            region = indices[y:y + h, x:x + w]
            two = a.kind in ("tileset", "bg") or int(indices.max(initial=0)) > 15
            rows = ["".join(("%02x" if two else "%x") % v for v in row) for row in region]
            return txt({"id": a.id, "x": x, "y": y, "w": region.shape[1], "h": region.shape[0],
                        "chars_per_pixel": 2 if two else 1, "rows": rows})
        if name == "paint_pixels":
            a = proj.get(args["id"])
            indices, _plte, _t = proj.view(a)
            idx = indices.copy()
            two = a.kind in ("tileset", "bg") or int(indices.max(initial=0)) > 15
            step = 2 if two else 1
            x0, y0 = args.get("x", 0), args.get("y", 0)
            for dy, row in enumerate(args.get("rows") or []):
                for dx in range(0, len(row), step):
                    ch = row[dx:dx + step]
                    if "." in ch:
                        continue
                    px, py = x0 + dx // step, y0 + dy
                    if 0 <= px < idx.shape[1] and 0 <= py < idx.shape[0]:
                        idx[py, px] = int(ch, 16)
            for r in args.get("rects") or []:
                idx[r["y"]:r["y"] + r["h"], r["x"]:r["x"] + r["w"]] = int(r["index"])
            if a.kind == "sprite" and a.layout.get("bpp", 4) == 4 and int(idx.max(initial=0)) > 15:
                raise ProjectError("sprite colour indices must be 0-15")
            notes = proj.write_view(a, idx)
            return txt({"updated": a.file, "notes": notes})
        if name == "import_image":
            a = proj.get(args["id"])
            src = args.get("path")
            tmp = None
            if not src:
                if not args.get("png_base64"):
                    raise ProjectError("give either path or png_base64")
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.write(base64.b64decode(args["png_base64"]))
                tmp.close()
                src = tmp.name
            try:
                notes = proj.import_image(a, src, new_palette=bool(args.get("new_palette")),
                                          resize=bool(args.get("resize")))
            finally:
                if tmp:
                    Path(tmp.name).unlink(missing_ok=True)
            return txt({"ok": True, "file": a.file, "notes": notes})
        if name == "set_palette":
            cols = []
            for c in args["colors"]:
                if isinstance(c, str):
                    c = c.lstrip("#")
                    cols.append(tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)))
                else:
                    cols.append(tuple(int(v) for v in c[:3]))
            return txt({"ok": True, "recoloured": proj.set_palette(proj.get(args["id"]).id, cols)})
        if name in ("list_save_points", "make_save_point"):
            if proj.kind != "decomp":
                raise ProjectError("save points need a source tree")
            return self.history_call(name, args, txt)
        if name in MAP_TOOLS:
            m = getattr(proj, "maps", None)
            if m is None:
                raise ProjectError("map tools need a pokeemerald-style source tree")
            return self.map_call(m, name, args, txt)
        if name == "revert_asset":
            proj.revert(proj.get(args["id"]))
            return txt("reverted")
        if name == "status":
            return txt(proj.status())
        if name == "validate":
            return txt(proj.build(dry_run=True).to_dict())
        if name == "build_rom":
            return txt(proj.build(args.get("output")).to_dict())
        raise ProjectError(f"unknown tool {name}")

    def map_call(self, m, name: str, args: dict, txt) -> list[dict]:
        if name == "list_maps":
            q = (args.get("query") or "").lower()
            return txt([x for x in m.list_maps() if not q or q in (x["name"] + x["id"]).lower()])
        if name == "view_map":
            scale = max(1, min(int(args.get("scale") or 1), 4))
            im = m.render(args["name"], events=args.get("events", True), scale=scale)
            lay = m.layout_of(args["name"])
            return [self._png(im), *txt(f"{m.resolve(args['name'])}: {lay['width']}x{lay['height']} blocks, "
                                        f"{16 * scale}px per block")]
        if name == "get_map":
            lay = m.layout_of(args["name"])
            return txt({"map": m.get_map(args["name"]),
                        "layout": {k: lay.get(k) for k in ("id", "width", "height", "primary_tileset",
                                                           "secondary_tileset", "layout_version")},
                        "scripts_file": str(m.script_path(args["name"]).relative_to(m.root))})
        if name == "view_metatiles":
            return [self._png(m.metatile_sheet(args["name"], int(args.get("scale") or 2))),
                    *txt("metatile id = row label + column (0-7)")]
        if name == "get_blocks":
            return txt(m.blocks_text(args["name"], args.get("x", 0), args.get("y", 0), args.get("w"), args.get("h")))
        if name == "set_blocks":
            n = m.apply_blocks(args["name"], args.get("x", 0), args.get("y", 0), args["rows"])
            return txt(f"updated {n} block(s)")
        if name == "copy_blocks":
            notes = m.copy_blocks(args["source"], args["x"], args["y"], args["w"], args["h"], args["dest"],
                                  args["dx"], args["dy"])
            return txt({"ok": True, "notes": notes})
        if name == "edit_map":
            data = m.get_map(args["name"])
            for k, v in (args.get("header") or {}).items():
                if k in ("id", "name") or isinstance(data.get(k), list):
                    raise ProjectError(f"header field {k} cannot be changed here")
                data[k] = v
            from .maps import DEFAULT_EVENTS
            for op in args.get("events") or []:
                kind = op.get("kind")
                if kind not in DEFAULT_EVENTS:
                    raise ProjectError(f"unknown event kind {kind}")
                lst = data.setdefault(kind, [])
                if op.get("op") == "add":
                    lst.append({**DEFAULT_EVENTS[kind], **(op.get("fields") or {})})
                elif op.get("op") in ("update", "delete"):
                    i = int(op.get("index", -1))
                    if not 0 <= i < len(lst):
                        raise ProjectError(f"{kind} has no index {i}")
                    if op["op"] == "update":
                        lst[i].update(op.get("fields") or {})
                    else:
                        lst.pop(i)
                else:
                    raise ProjectError("op must be add, update or delete")
            return txt({"ok": True, "warnings": m.save_map(args["name"], data),
                        "counts": {k: len(data.get(k, [])) for k in DEFAULT_EVENTS}})
        if name == "get_script":
            return txt(m.read_script(args["name"]))
        if name == "write_script":
            m.write_script(args["name"], args["text"])
            bad = [f"{t.label}: {p}" for t in self.proj.textbox.texts(args["text"]) for p in t.problems]
            return txt({"saved": True, "text_problems": bad[:40]} if bad else "saved")
        if name == "preview_text":
            tb = self.proj.textbox
            texts = tb.texts(m.read_script(m.resolve(args["name"])))
            if args.get("label"):
                texts = [t for t in texts if t.label == args["label"]]
                if not texts:
                    raise ProjectError(f"no text label {args['label']} in {args['name']}'s scripts")
            else:
                texts = [t for t in texts if t.problems][:6]
                if not texts:
                    return txt("every text in this map fits its box")
            out = []
            for t in texts:
                out.append(self._png(tb.render(t, scale=2)))
                out += txt({"label": t.label, "problems": t.problems})
            return out
        if name == "new_script":
            return txt({"label": m.new_script(args["name"], args.get("kind", "npc"), args.get("text", "Hello!"))})
        if name == "duplicate_map":
            notes = m.duplicate_map(args["source"], args["new_name"], args.get("group"), args.get("copy_layout", True),
                                    args.get("map_id"))
            return txt({"ok": True, "map": args["new_name"], "id": m.get_map(args["new_name"])["id"], "notes": notes})
        if name == "link_warps":
            m.link_warps(args["map"], int(args["warp"]), args["dest_map"], int(args["dest_warp"]))
            return txt("linked")
        if name == "resize_map":
            return txt({"ok": True, "notes": m.resize(args["name"], int(args["width"]), int(args["height"]))})
        te = m.tileset_editor
        if name == "tileset_info":
            return txt(te.info(args["name"]))
        if name == "get_metatile":
            return txt(te.get_metatile(args["name"], int(args["id"])))
        if name == "set_metatile":
            te.set_metatile(args["name"], int(args["id"]), args.get("entries"), args.get("behavior"),
                            args.get("layer_type"))
            return txt("saved")
        if name == "add_metatiles":
            return txt({"ids": te.add_metatiles(args["name"], int(args.get("count") or 1), args.get("copy_from"))})
        if name == "view_tiles":
            return [self._png(te.tile_sheet(args["name"], int(args.get("bank") or 0), 2)),
                    *txt("tile id = row * 16 + column")]
        if name == "import_art_as_metatiles":
            src, tmp = args.get("path"), None
            if not src:
                if not args.get("png_base64"):
                    raise ProjectError("give either path or png_base64")
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.write(base64.b64decode(args["png_base64"]))
                tmp.close()
                src = tmp.name
            try:
                r = te.import_image(args["name"], src, args.get("base_metatile"), args.get("bank"),
                                    bool(args.get("new_palette")), args.get("behavior") or 0)
            finally:
                if tmp:
                    Path(tmp.name).unlink(missing_ok=True)
            r["rows_for_set_blocks"] = [" ".join(f"{i:03x}" for i in r["ids"][y * r["w"]:(y + 1) * r["w"]])
                                        for y in range(r["h"])]
            return txt(r)
        if name in ("list_items", "get_item", "set_item", "list_moves", "get_move", "set_move", "get_tms", "set_tms",
                    "get_teachable", "list_marts", "set_mart", "difficulty_report"):
            gd = self.proj.gamedata
            if name == "list_items":
                return txt(gd.items(args.get("query", ""))[:400])
            if name == "get_item":
                return txt(gd.get_item(args["item"]))
            if name == "set_item":
                return txt(gd.set_item(args["item"], args["changes"]))
            if name == "list_moves":
                return txt(gd.moves(args.get("query", ""))[:400])
            if name == "get_move":
                return txt(gd.get_move(args["move"]))
            if name == "set_move":
                return txt(gd.set_move(args["move"], args["changes"]))
            if name == "get_tms":
                return txt(dict(gd.tms(), tutors=gd.tutors()))
            if name == "set_tms":
                return txt(gd.set_tms(args.get("tms"), args.get("hms")))
            if name == "get_teachable":
                return txt(dict(gd.teachable(args["id"]), egg_moves=gd.egg_moves(args["id"])))
            if name == "list_marts":
                return txt({"marts": gd.marts()})
            if name == "difficulty_report":
                d = gd.difficulty()
                for c in d["chapters"]:  # keep it short: the strongest few per chapter
                    c["trainers"] = sorted(c["trainers"], key=lambda t: -t["ace"])[:8]
                return txt(d)
            return txt(gd.set_mart(args["label"], args["items"]))
        if name in ("list_trainers", "get_trainer", "set_trainer", "get_wild", "set_wild", "get_species", "set_species"):
            gd = self.proj.gamedata
            if name == "list_trainers":
                return txt(gd.trainers(args.get("query", ""))[:300])
            if name == "get_trainer":
                return txt(gd.get_trainer(args["id"]))
            if name == "set_trainer":
                return txt(gd.set_trainer(args["id"], {k: args[k] for k in ("mons", "fields") if k in args}))
            if name == "get_wild":
                return txt(gd.wild(args["map"]))
            if name == "set_wild":
                return txt(gd.set_wild(args["map"], args["kind"], int(args.get("encounter_rate") or 0),
                                       args.get("mons") or []))
            if name == "get_species":
                return txt(gd.get_species(args["id"]))
            return txt(gd.set_species(args["id"], args["changes"]))
        if name == "playtest_battle":
            from .playtest import PlaytestError
            try:
                img = self.proj.play.start_battle(trainer=args.get("trainer"), species=args.get("species"),
                                                  level=int(args.get("level") or 5))
            except PlaytestError as e:
                raise ProjectError(str(e))
            return [self._png(img.resize((480, 320), Image.NEAREST)), *txt(f"battling {self.proj.play.where['battle']}")]
        if name == "story_flag":
            u = m.story.flow()["usage"].get(args["name"].upper())
            return txt(u or f"no event uses {args['name'].upper()}")
        if name == "suggest_map_chapters":
            sg = m.story.suggest_chapters()
            if args.get("apply"):
                m.story.apply_suggestions()
            return txt({"suggestions": sg, "applied": bool(args.get("apply"))})
        if name == "list_story_chapters":
            chapters = m.story.chapters()
            return txt({"chapters": [dict(c, number=i + 1) for i, c in enumerate(chapters)],
                        "maps_not_in_a_chapter": m.story.unplaced_maps()})
        if name == "story_items":
            r = m.story.catch_up(int(args["chapter"]) - 1, bool(args.get("extras")))
            keep = ("name", "item", "qty", "kind", "map", "needed", "flag")
            return txt({"chapter": r["name"], "flags": r["flags"], "items": [{k: i[k] for k in keep} for i in r["items"]]})
        if name == "set_map_chapter":
            ch = int(args["chapter"])
            m.story.set_chapter(args["map"], ch - 1 if ch > 0 else None)
            return txt({"ok": True})
        if name in ("playtest_save_checkpoint", "list_checkpoints", "playtest_run_script", "playtest_set_state",
                    "playtest_team"):
            from .playtest import PlaytestError
            play = self.proj.play
            try:
                if name == "playtest_save_checkpoint":
                    return txt(play.save_checkpoint(args["name"], args.get("note", "")))
                if name == "list_checkpoints":
                    return txt(play.checkpoints())
                if name == "playtest_team":
                    return txt(play.team())
                if name == "playtest_set_state":
                    return txt({"done": play.apply_state(args["state"])})
                img = play.run_script(args["label"], int(args.get("frames") or 60))
                return [self._png(img.resize((480, 320), Image.NEAREST)), *txt(f"started {args['label']}")]
            except PlaytestError as e:
                raise ProjectError(str(e))
        if name in ("playtest_start", "playtest_press"):
            from .playtest import PlaytestError, strip
            proj = self.proj
            try:
                if name == "playtest_start":
                    if args.get("build", True):
                        rep = proj.build_rom()
                        if rep.returncode != 0:
                            tail = "\n".join(rep.output.rstrip().splitlines()[-30:])
                            raise ProjectError(f"make failed (exit {rep.returncode}):\n{tail}")
                    state = args.get("state")
                    if args.get("chapter") or args.get("team"):
                        state = m.story.play_state(int(args["chapter"]) - 1 if args.get("chapter") else None,
                                                   bool(args.get("extras")), team=args.get("team"),
                                                   clear_team=bool(args.get("clear_team")), state=state)
                    img = proj.play.start(m.resolve(args["map"]) if args.get("map") else None, args.get("x"),
                                          args.get("y"), checkpoint=args.get("checkpoint"),
                                          state=state, script=args.get("script"))
                    out = [self._png(img.resize((480, 320), Image.NEAREST))]
                    return out + txt(f"playing {proj.play.where}")
                final, frames = proj.play.press(args["inputs"], record_every=8 if args.get("record") else 0)
                out = [self._png(final.resize((480, 320), Image.NEAREST))]
                if frames:
                    out.append(self._png(strip(frames)))
                return out + txt("ok")
            except PlaytestError as e:
                raise ProjectError(str(e))
        if name in ("list_tileset_anims", "view_tileset_anim", "edit_tileset_anim", "set_anim_frame", "add_anim_frame",
                    "remove_anim_frame", "create_tileset_anim"):
            return self.anim_call(m, name, args, txt)
        if name == "find_constants":
            q = (args.get("query") or "").upper()
            hits = [c for c in m.constants(args["prefix"]) if q in c]
            return txt({"total": len(hits), "constants": hits[:300]})
        raise ProjectError(f"unknown tool {name}")

    def history_call(self, name: str, args: dict, txt) -> list[dict]:
        from .history import History
        h = History(self.proj)
        if name == "make_save_point":
            return txt(h.save(args.get("message", "")))
        return txt({"unsaved": h.unsaved(), "save_points": h.points(30)})

    def anim_call(self, m, name: str, args: dict, txt) -> list[dict]:
        ta = m.tileset_anims
        mp = m.resolve(args["map"]) if args.get("map") else None
        if name == "list_tileset_anims":
            keep = ("id", "frames", "sequence", "period", "count", "slots", "tilesets", "staggered", "custom", "notes")
            return txt([{k: a[k] for k in keep} for a in ta.list(mp)])
        if name == "view_tileset_anim":
            a = ta.get(args["id"]).as_dict()
            if mp:
                a["animated_metatiles"] = [f"{i:03x}" for i in ta.animated_metatiles(mp, args["id"])]
            return [self._png(ta.frames_image(args["id"], mp)), *txt(a)]
        if name == "edit_tileset_anim":
            if args.get("speed"):
                ta.set_speed(args["id"], int(args["speed"]))
            if args.get("order"):
                ta.set_sequence(args["id"], [int(i) for i in args["order"]])
            return txt(ta.get(args["id"]).as_dict())
        if name == "remove_anim_frame":
            return txt({"notes": ta.remove_frame(args["id"], int(args["frame"])),
                        "anim": ta.get(args["id"]).as_dict()})
        if name == "create_tileset_anim":
            return txt(ta.create(m.resolve(args["map"]), args["name"], int(args["tile"]), int(args["count"]),
                                 int(args.get("frames") or 2), int(args.get("speed") or 16)))
        src, tmp = args.get("path"), None
        if not src and args.get("png_base64"):
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp.write(base64.b64decode(args["png_base64"]))
            tmp.close()
            src = tmp.name
        try:
            if name == "set_anim_frame":
                if not src:
                    raise ProjectError("give either path or png_base64")
                notes = ta.set_frame(args["id"], int(args["frame"]), src, mp)
                return [self._png(ta.frames_image(args["id"], mp)), *txt({"ok": True, "notes": notes})]
            r = ta.add_frame(args["id"], src, args.get("copy_of"), args.get("position"), mp)
            return [self._png(ta.frames_image(args["id"], mp)), *txt(r)]
        finally:
            if tmp:
                Path(tmp.name).unlink(missing_ok=True)

    # ------------------------------------------------------------------ protocol
    def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            return None  # notification
        try:
            if method == "initialize":
                ver = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
                result = {"protocolVersion": ver, "capabilities": {"tools": {}},
                          "serverInfo": {"name": "pkmn-asset-editor", "version": __version__},
                          "instructions": "Tools for editing the graphics of a Gen 3 Pokémon game (a pokeemerald-"
                                          "style source tree or an extracted ROM project). Start with "
                                          "list_assets/describe_asset, look with view_asset, edit with "
                                          "paint_pixels/import_image/set_palette, then validate and build_rom. "
                                          "Respect describe_asset's constraints (size, colours, in-game palette)."}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [{"name": n, "description": d, "inputSchema": s} for n, d, s in TOOLS]}
            elif method == "tools/call":
                params = msg.get("params") or {}
                try:
                    content = self.call(params.get("name"), params.get("arguments") or {})
                    result = {"content": content, "isError": False}
                except ERRORS as e:
                    result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def run(proj, stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    srv = Server(proj)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = srv.handle(msg)
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()

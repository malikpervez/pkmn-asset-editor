"""Command line interface: ``pae <command>``."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import __version__
from .decomp import DecompError, _exclude_state_dir, open_project
from .project import Project, ProjectError

ERRORS = (ProjectError, DecompError)


def describe(proj: Project, asset) -> dict:
    """Everything an editor (human or AI) needs to know to change one asset safely."""
    d = asset.to_dict()
    d.pop("sha1", None)
    hx = lambda v: [f"0x{x:06X}" for x in v] if isinstance(v, list) else f"0x{v:06X}"
    d["blobs"] = {k: hx(v) for k, v in asset.blobs.items()}
    d["meta"] = {k: (hx(v) if k.endswith("offset") and isinstance(v, int) else v) for k, v in asset.meta.items()}
    if asset.kind == "tileset":
        d["layout"] = {k: v for k, v in asset.layout.items() if k != "banks"}
    d["file"] = asset.file
    d["absolute_path"] = str(proj.path(asset))
    d["modified"] = proj.is_touched(asset)
    if asset.kind == "palette":
        d["constraints"] = {"colors": asset.layout["colors"], "format": "JASC-PAL; 15-bit colour (channels rounded to multiples of 8)",
                            "used_by": proj.users_of_palette(asset.id)}
    else:
        w, h = proj._dims(asset)
        c = {"width": w, "height": h}
        if asset.kind == "sprite":
            c.update({"format": "4-bit indexed PNG, 16 colours, index 0 = transparent",
                      "frame_size_px": [asset.layout["frame_w"] * 8, asset.layout["frame_h"] * 8],
                      "frames": asset.layout["frames"], "frame_arrangement": f"{asset.layout['cols']} per row"})
        elif asset.kind == "tileset":
            c.update({"format": "8-bit indexed PNG, pixel = bank*16 + colour; only the colour is stored",
                      "tiles": asset.layout["count"]})
        else:
            c.update({"format": "8-bit indexed PNG, pixel = bank*16 + colour; re-tiled automatically",
                      "max_unique_tiles": asset.layout["tiles"], "retile_allowed": asset.layout.get("retile", False)})
        shared = {}
        for link in asset.palettes:
            users = [u for u in proj.users_of_palette(link["palette"]) if u != asset.id]
            if users:
                shared[link["palette"]] = users
        c["palette_shared_with"] = shared
        d["constraints"] = c
    return d


def _print_report(rep, as_json: bool) -> None:
    if as_json:
        print(json.dumps(rep.to_dict(), indent=2))
        return
    for w in rep.warnings:
        print(f"warning: {w}")
    for e in rep.errors:
        print(f"error: {e}")
    if hasattr(rep, "checked"):  # source tree
        print(f"checked {rep.checked} file(s)")
        if rep.command:
            tail = "\n".join(rep.output.rstrip().splitlines()[-30:])
            print(f"$ {rep.command}\n{tail}\nexit status {rep.returncode} after {rep.seconds:.0f}s")
        return
    if rep.ok:
        print(f"{len(rep.changed_assets)} asset(s) changed: {len(rep.in_place)} block(s) written in place, "
              f"{len(rep.relocated)} relocated")
        for r in rep.relocated:
            print(f"  moved {r['from']} -> {r['to']} ({r['size']} bytes, {r['pointers_updated']} pointer(s) updated)")
        if rep.output:
            print(f"wrote {rep.output}")
        for p in rep.patches:
            print(f"wrote {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pae", description="Extract, organise, edit and re-insert Gen 3 Pokémon ROM graphics.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_project(p):
        p.add_argument("--project", "-p", default=os.environ.get("PAE_PROJECT", "."), help="project folder (default: .)")
        return p

    p = sub.add_parser("extract", help="extract every graphic from a ROM into a new project folder")
    p.add_argument("rom")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--force", action="store_true", help="overwrite an existing project")

    p = sub.add_parser("scan", help="show which graphics tables are found in a ROM (no project needed)")
    p.add_argument("rom")

    p = with_project(sub.add_parser("import-dir", help="import a folder of images; paths mirror asset ids"))
    p.add_argument("folder", help="e.g. a folder containing pokemon/025_pikachu/front.png")
    p.add_argument("--resize", action="store_true")
    p.add_argument("--new-palette", action="store_true")

    p = with_project(sub.add_parser("list", help="list assets"))
    p.add_argument("--category", "-c")
    p.add_argument("--kind", "-k", choices=["sprite", "palette", "tileset", "bg"])
    p.add_argument("--query", "-q", help="substring of id or label")
    p.add_argument("--json", action="store_true")

    p = with_project(sub.add_parser("describe", help="show one asset and its editing constraints"))
    p.add_argument("asset")

    p = with_project(sub.add_parser("status", help="list edited assets"))
    p.add_argument("--json", action="store_true")

    p = with_project(sub.add_parser("validate", aliases=["check"], help="check edits without building"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="source tree: check every referenced graphic, not just edits")

    p = with_project(sub.add_parser("build", help="write the edited ROM (ROM project) or run make (source tree)"))
    p.add_argument("--make", help="source tree: build command (default: make -jN; saved in .pae/config.json)")
    p.add_argument("-o", "--output", help="output ROM path (default: build/rom.gba)")
    p.add_argument("--patch", action="append", choices=["bps", "ups", "ips"], help="patch format(s) to write")
    p.add_argument("--no-patch", action="store_true")
    p.add_argument("--json", action="store_true")

    p = with_project(sub.add_parser("import", help="convert any image into an asset (palette mapping, resizing)"))
    p.add_argument("asset")
    p.add_argument("source")
    p.add_argument("--resize", action="store_true", help="scale the image to fit the asset size")
    p.add_argument("--new-palette", action="store_true", help="build a new palette from the image")

    p = with_project(sub.add_parser("revert", help="restore assets to their original ROM contents"))
    p.add_argument("assets", nargs="*")
    p.add_argument("--all", action="store_true")

    p = with_project(sub.add_parser("set", help="change how an asset is laid out / labelled / previewed"))
    p.add_argument("asset")
    p.add_argument("--frame-w", type=int, help="frame width in 8px tiles")
    p.add_argument("--frame-h", type=int, help="frame height in 8px tiles")
    p.add_argument("--cols", type=int, help="frames per row")
    p.add_argument("--tiles-wide", type=int, help="sheet width in tiles (single-frame view)")
    p.add_argument("--palette", help="palette asset used to colour this image ('none' for grayscale)")
    p.add_argument("--label")
    p.add_argument("--force", action="store_true")

    p = with_project(sub.add_parser("sync-palettes", help="re-colour PNGs after editing .pal files"))
    p.add_argument("palettes", nargs="*", help="palette ids (default: every edited palette)")

    p = with_project(sub.add_parser("fix", aliases=["normalize"],
                                    help="convert RGB/RGBA PNGs to valid indexed PNGs in place"))
    p.add_argument("files", nargs="*", help="default: every edited PNG")

    p = with_project(sub.add_parser("palette-from-png", help="source tree: copy a PNG's colours into the .pal "
                                                               "file the game uses for it"))
    p.add_argument("images", nargs="+")

    p = sub.add_parser("map", help="source tree: maps, events, scripts and tilesets (see `pae map -h`)")
    msub = p.add_subparsers(dest="map_cmd", required=True)
    q = with_project(msub.add_parser("list", help="list maps"))
    q.add_argument("query", nargs="?", default="")
    q = with_project(msub.add_parser("render", help="render a map (with events) to a PNG"))
    q.add_argument("name")
    q.add_argument("-o", "--out", required=True)
    q.add_argument("--scale", type=int, default=1)
    q.add_argument("--no-events", action="store_true")
    q.add_argument("--grid", action="store_true")
    q = with_project(msub.add_parser("metatiles", help="render the metatiles a map can use (with ids)"))
    q.add_argument("name")
    q.add_argument("-o", "--out", required=True)
    q = with_project(msub.add_parser("events", help="print a map's events"))
    q.add_argument("name")
    q = with_project(msub.add_parser("new", help="create a new map as a copy of an existing one (e.g. a new house)"))
    q.add_argument("source")
    q.add_argument("new_name")
    q.add_argument("--group")
    q.add_argument("--share-layout", action="store_true", help="reuse the source's blocks instead of copying them")
    q.add_argument("--id", help="map id constant (default derived from the name)")
    q = with_project(msub.add_parser("tiles", help="tileset info for a map (metatile/tile counts and limits)"))
    q.add_argument("name")
    q = with_project(msub.add_parser("import-art", help="turn artwork into new metatiles (and optionally place it on the map)"))
    q.add_argument("name")
    q.add_argument("image")
    q.add_argument("--x", type=int, help="place the result with its top-left block here")
    q.add_argument("--y", type=int)
    q.add_argument("--base", help="metatile id whose ground goes under transparent pixels (default with "
                                  "--x/--y: the block already there)")
    q.add_argument("--bank", type=int, help="palette bank to use (default: best match)")
    q.add_argument("--new-palette", action="store_true", help="write the art's colours into --bank")
    q.add_argument("--solid", action="store_true", help="make the placed blocks impassable")
    q = with_project(msub.add_parser("link", help="connect two warps in both directions"))
    q.add_argument("map")
    q.add_argument("warp", type=int)
    q.add_argument("dest_map")
    q.add_argument("dest_warp", type=int)

    p = sub.add_parser("anim", help="source tree: tileset animations - water, flowers, waterfalls… (see `pae anim -h`)")
    asub = p.add_subparsers(dest="anim_cmd", required=True)
    q = with_project(asub.add_parser("list", help="list tileset animations (all, or those a map shows)"))
    q.add_argument("--map")
    q = with_project(asub.add_parser("show", help="render an animation's frames in their in-game colours"))
    q.add_argument("id")
    q.add_argument("-o", "--out", required=True)
    q.add_argument("--map", help="map whose palettes to use (default: one that uses the tileset)")
    q = with_project(asub.add_parser("preview", help="record a map's animations as an animated GIF"))
    q.add_argument("map")
    q.add_argument("-o", "--out", required=True)
    q.add_argument("--region", help="x,y,w,h in blocks (default: the whole map)")
    q.add_argument("--scale", type=int, default=1)
    q = with_project(asub.add_parser("speed", help="game frames each animation frame stays up (60 = 1 s)"))
    q.add_argument("id")
    q.add_argument("frames", type=int)
    q = with_project(asub.add_parser("order", help="set the play order, e.g. 0,1,0,2"))
    q.add_argument("id")
    q.add_argument("order")
    q = with_project(asub.add_parser("frame", help="replace a frame's art with an image"))
    q.add_argument("id")
    q.add_argument("frame", type=int)
    q.add_argument("image")
    q.add_argument("--map", help="map whose palettes the image's colours are matched to")
    q = with_project(asub.add_parser("add-frame", help="add a frame (copy of one, or an image)"))
    q.add_argument("id")
    q.add_argument("--image")
    q.add_argument("--copy", type=int, help="frame to copy (default: the last)")
    q.add_argument("--at", type=int, help="position in the play order (default: the end)")
    q.add_argument("--map")
    q = with_project(asub.add_parser("remove-frame", help="take a frame out of an animation"))
    q.add_argument("id")
    q.add_argument("frame", type=int)
    q = with_project(asub.add_parser("new", help="animate tiles of a map's tileset (e.g. your own water)"))
    q.add_argument("map")
    q.add_argument("name", help="e.g. PondWater")
    q.add_argument("--tile", required=True, help="first tile id, as the metatile editor shows it")
    q.add_argument("--count", type=int, required=True, help="number of tiles animated")
    q.add_argument("--frames", type=int, default=2, help="frames (copies of the current tiles, to redraw)")
    q.add_argument("--speed", type=int, default=16, help="game frames per animation frame")
    q.add_argument("--images", nargs="*", help="frame images instead of copies")

    p = with_project(sub.add_parser("play", help="source tree: build, then play-test a map headlessly and save "
                                                  "screenshots (needs libmgba)"))
    p.add_argument("map", nargs="?", help="map name, e.g. PetalburgCity (optional with --from)")
    p.add_argument("--from", dest="checkpoint", help="continue from a saved checkpoint (story progress, party…)")
    p.add_argument("--state", action="append", default=[],
                   help="story state before arriving: FLAG_X, !FLAG_X, VAR_X=3, ITEM_X*5 (repeatable)")
    p.add_argument("--script", help="start this script label on arrival (e.g. a cutscene)")
    p.add_argument("--chapter", type=int, help="start as if you had played up to the end of this story chapter "
                                               "(see `pae story list`): gives the needed items and badges")
    p.add_argument("--extras", action="store_true", help="with --chapter: also give the extra items (item balls, "
                                                         "hidden items, gifts)")
    p.add_argument("--team", action="append", default=[],
                   help="add a Pokémon: 'SPECIES_MUDKIP lv20 ITEM_ORAN_BERRY MOVE_TACKLE' (repeatable)")
    p.add_argument("--clear-team", action="store_true", help="empty the team before adding --team Pokémon")
    p.add_argument("--battle", help="start a battle against this trainer (TRAINER_X) on arrival")
    p.add_argument("--wild", help="start a wild battle on arrival: SPECIES_X or SPECIES_X:level")
    p.add_argument("--save-as", help="after the inputs, save the game as a checkpoint with this name")
    p.add_argument("--x", type=int)
    p.add_argument("--y", type=int)
    p.add_argument("--inputs", default="", help="e.g. 'UP, wait 10, A, wait 60' (KEY*N holds N frames)")
    p.add_argument("-o", "--out", default="playtest.png", help="final screenshot (.png)")
    p.add_argument("--gif", help="also record the inputs as an animated GIF")
    p.add_argument("--no-build", action="store_true", help="use the existing ROM without running make")
    p.add_argument("--scale", type=int, default=2)

    p = with_project(sub.add_parser("savepoint", help="source tree: remember everything as it is now (a git commit)"))
    p.add_argument("message", nargs="*", help="what you changed")
    p = with_project(sub.add_parser("history", help="source tree: list save points, see one, or go back to one"))
    p.add_argument("action", nargs="?", choices=["list", "show", "back"], default="list")
    p.add_argument("sha", nargs="?")
    p = sub.add_parser("text", help="source tree: dialogue in the game's font, and checks for text that won't fit")
    xsub = p.add_subparsers(dest="text_cmd", required=True)
    q = with_project(xsub.add_parser("check", help="check every text of a map (or of all maps)"))
    q.add_argument("map", nargs="?")
    q = with_project(xsub.add_parser("find", help="find words in all dialogue, place names and trainer names"))
    q.add_argument("query")
    q.add_argument("--case", action="store_true", help="match upper/lower case")
    q.add_argument("--word", action="store_true", help="whole words only")
    q = with_project(xsub.add_parser("replace", help="replace words everywhere (UPPERCASE stays UPPERCASE; text that "
                                                     "no longer fits is re-wrapped)"))
    q.add_argument("query")
    q.add_argument("replacement")
    q.add_argument("--case", action="store_true", help="match upper/lower case")
    q.add_argument("--word", action="store_true", help="whole words only")
    q.add_argument("--dry-run", action="store_true", help="only show what would change")
    q = with_project(xsub.add_parser("preview", help="render a text label as it looks in game"))
    q.add_argument("map")
    q.add_argument("label")
    q.add_argument("-o", "--out", required=True)
    p = sub.add_parser("trainer", help="source tree: trainer teams (src/data/trainers.party)")
    tsub = p.add_subparsers(dest="trainer_cmd", required=True)
    q = with_project(tsub.add_parser("list", help="list trainers"))
    q.add_argument("query", nargs="?", default="")
    q = with_project(tsub.add_parser("show", help="show a trainer and their team"))
    q.add_argument("id")
    q = with_project(tsub.add_parser("set", help="replace a trainer's team and/or fields"))
    q.add_argument("id")
    q.add_argument("--mon", action="append", default=[],
                   help="'Geodude lv12 @Oran Berry : Tackle, Rock Throw' (repeat for each Pokémon; replaces the team)")
    q.add_argument("--field", action="append", default=[], help="Key=Value, e.g. 'Items=Potion / Potion'")
    p = sub.add_parser("wild", help="source tree: wild Pokémon per map (src/data/wild_encounters.json)")
    wsub = p.add_subparsers(dest="wild_cmd", required=True)
    q = with_project(wsub.add_parser("show", help="show a map's wild Pokémon"))
    q.add_argument("map")
    q = with_project(wsub.add_parser("set", help="set one kind of encounter on a map"))
    q.add_argument("map")
    q.add_argument("kind", choices=["land", "water", "rock_smash", "fishing"])
    q.add_argument("slots", nargs="+", help="one per slot: 'Poochyena:2-3' (species:min-max)")
    q.add_argument("--rate", type=int, default=20, help="how often (encounter rate, 20 = normal grass)")
    p = sub.add_parser("species", help="source tree: Pokémon stats, types, abilities, evolutions, moves")
    psub = p.add_subparsers(dest="species_cmd", required=True)
    q = with_project(psub.add_parser("show", help="show a Pokémon's data"))
    q.add_argument("id")
    q = with_project(psub.add_parser("set", help="change a Pokémon's data"))
    q.add_argument("id")
    for k, flag in (("baseHP", "--hp"), ("baseAttack", "--atk"), ("baseDefense", "--def"), ("baseSpAttack", "--spa"),
                    ("baseSpDefense", "--spd"), ("baseSpeed", "--spe"), ("catchRate", "--catch-rate"),
                    ("expYield", "--exp")):
        q.add_argument(flag, dest=k, type=int)
    q.add_argument("--types", help="e.g. TYPE_WATER,TYPE_GROUND")
    q.add_argument("--abilities", help="up to 3, e.g. ABILITY_TORRENT,,ABILITY_DAMP")
    q.add_argument("--learn", action="append", help="replace the level-up moves: '1:Tackle' (repeatable)")

    p = sub.add_parser("item", help="source tree: items (name, price, description, pocket)")
    isub = p.add_subparsers(dest="item_cmd", required=True)
    q = with_project(isub.add_parser("list", help="list items"))
    q.add_argument("query", nargs="?", default="")
    q = with_project(isub.add_parser("show", help="show an item"))
    q.add_argument("id")
    q = with_project(isub.add_parser("set", help="change an item"))
    q.add_argument("id")
    q.add_argument("--name")
    q.add_argument("--price", type=int)
    q.add_argument("--description", help="use \\n for a new line")
    q.add_argument("--pocket", help="e.g. POCKET_ITEMS")
    p = sub.add_parser("move", help="source tree: moves (power, accuracy, PP, type, description)")
    vsub = p.add_subparsers(dest="move_cmd", required=True)
    q = with_project(vsub.add_parser("list", help="list moves"))
    q.add_argument("query", nargs="?", default="")
    q = with_project(vsub.add_parser("show", help="show a move"))
    q.add_argument("id")
    q = with_project(vsub.add_parser("set", help="change a move"))
    q.add_argument("id")
    q.add_argument("--name")
    for k in ("power", "accuracy", "pp", "priority"):
        q.add_argument(f"--{k}", type=int)
    q.add_argument("--type", help="e.g. TYPE_FIRE")
    q.add_argument("--category", help="DAMAGE_CATEGORY_PHYSICAL, _SPECIAL or _STATUS")
    q.add_argument("--description", help="use \\n for a new line")
    p = sub.add_parser("tm", help="source tree: which move each TM and HM teaches")
    msub = p.add_subparsers(dest="tm_cmd", required=True)
    with_project(msub.add_parser("list", help="list TMs, HMs and move tutors"))
    q = with_project(msub.add_parser("set", help="set TM NUMBER (or HM NUMBER) to teach a move"))
    q.add_argument("kind", choices=["tm", "hm"])
    q.add_argument("number", type=int)
    q.add_argument("move")
    p = sub.add_parser("mart", help="source tree: what Poké Marts sell")
    ksub = p.add_subparsers(dest="mart_cmd", required=True)
    with_project(ksub.add_parser("list", help="list the shops and what they sell"))
    q = with_project(ksub.add_parser("set", help="set what a shop list sells"))
    q.add_argument("label", help="the list's label, e.g. OldaleTown_Mart_Pokemart_Basic")
    q.add_argument("items", nargs="+")

    with_project(sub.add_parser("difficulty", help="source tree: trainer and wild levels per story chapter, and "
                                                   "what looks too hard or too easy"))
    p = with_project(sub.add_parser("release", help="source tree: build a release and make BPS/UPS patches, a README "
                                                    "and a zip to share"))
    p.add_argument("--name")
    p.add_argument("--version")
    p.add_argument("--author")
    p.add_argument("--description")
    p.add_argument("--base", help="the original .gba the patch is for (remembered for next time)")
    p.add_argument("--notes", help="what's new, one item per line (default: your save points since the last release)")
    p.add_argument("--no-build", action="store_true", help="use the release ROM already built")
    p.add_argument("--force", action="store_true", help="make it even if Check for problems found errors")
    p.add_argument("--ups", action="store_true", help="also make a .ups patch (much bigger than the .bps)")
    p = with_project(sub.add_parser("new-pokemon", help="source tree: add a new Pokémon, copied from one you pick "
                                                        "(constants, Pokédex, graphics, learnsets, everything)"))
    p.add_argument("name", help="its name, e.g. Sparkit")
    p.add_argument("--from", dest="template", required=True, help="the Pokémon to copy, e.g. pichu")
    p.add_argument("--types", help="e.g. TYPE_ELECTRIC,TYPE_FAIRY")
    p.add_argument("--stats", help="HP,Atk,Def,SpA,SpD,Spe, e.g. 35,45,30,55,40,70")
    p.add_argument("--category", help="Pokédex category, e.g. Spark")
    p.add_argument("--description", help="Pokédex text (\\n for a new line)")
    p.add_argument("--evolves-from", help="SPECIES:LEVEL, e.g. pichu:12 (adds an EVO_LEVEL evolution)")
    for k in ("front", "back", "icon"):
        p.add_argument(f"--{k}", help=f"a picture for its {k} sprite (any PNG; fitted and recoloured)")
    p = sub.add_parser("story", help="source tree: story chapters, for play-tests that start later in the game")
    ssub = p.add_subparsers(dest="story_cmd", required=True)
    with_project(ssub.add_parser("list", help="list the chapters and their maps"))
    q = with_project(ssub.add_parser("items", help="items a player has by the end of a chapter"))
    q.add_argument("chapter", type=int)
    q.add_argument("--extras", action="store_true", help="include extra items, not just the needed ones")
    q = with_project(ssub.add_parser("move", help="put a map (and its inside maps) in a chapter"))
    q.add_argument("map")
    q.add_argument("chapter", help="chapter number, or 'none' to take it out of the story")
    q = with_project(ssub.add_parser("suggest", help="suggest chapters for maps not in one (by their doors/edges)"))
    q.add_argument("--apply", action="store_true", help="put the maps in the suggested chapters")
    q = with_project(ssub.add_parser("flag", help="which events change and check a flag or variable"))
    q.add_argument("name")
    q = with_project(ssub.add_parser("add", help="add a chapter"))
    q.add_argument("name")
    q.add_argument("--at", type=int, help="chapter number to insert at (default: the end)")
    q.add_argument("--flag", action="append", default=[], help="flag that is on once the chapter is done "
                                                                "(e.g. FLAG_BADGE01_GET)")

    p = with_project(sub.add_parser("checkpoint", help="source tree: saved games to start play-tests from"))
    p.add_argument("action", choices=["list", "import", "delete"])
    p.add_argument("name", nargs="?")
    p.add_argument("file", nargs="?", help="for import: a .sav from your emulator (mGBA, VBA, …)")

    p = with_project(sub.add_parser("doctor", help="check that building and play-testing will work"))
    p.add_argument("--fix", action="store_true", help="fix what's missing (installs tools, turns on Quickstart, "
                                                     "sets up save points, builds the game)")
    p.add_argument("--yes", action="store_true", help="with --fix: don't ask before each fix")

    with_project(sub.add_parser("guide", help="print editing instructions for AI agents (AGENTS.md style)"))

    p = with_project(sub.add_parser("sheet", help="render many assets onto one contact sheet"))
    p.add_argument("prefix", nargs="?", default="", help="id prefix or category, e.g. 'pokemon' or 'items/01'")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--kind", choices=["sprite", "palette", "tileset", "bg"])
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--width", type=int, default=1600)

    p = with_project(sub.add_parser("render", help="render one asset (optionally with another palette) to a PNG"))
    p.add_argument("asset")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("--palette", help="e.g. pokemon/025_pikachu/shiny")
    p.add_argument("--scale", type=int, default=1)

    p = with_project(sub.add_parser("serve", help="start the local web editor"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    with_project(sub.add_parser("mcp", help="run an MCP server on stdio for AI assistants"))

    p = sub.add_parser("apply-patch", help="apply a UPS or IPS patch to a ROM")
    p.add_argument("rom")
    p.add_argument("patch")
    p.add_argument("-o", "--out", required=True)

    args = ap.parse_args(argv)
    if args.cmd == "check":
        args.cmd = "validate"
    if args.cmd == "normalize":
        args.cmd = "fix"
    try:
        return _run(args)
    except ERRORS as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _parse_mon_arg(text: str) -> dict:
    """'Geodude lv12 @Oran Berry : Tackle, Rock Throw' -> a trainers.party Pokémon."""
    head, _, moves = text.partition(":")
    item = ""
    if "@" in head:
        head, item = head.split("@", 1)
    m = re.search(r"\b(?:lv|level)\s*(\d+)\b", head, re.I)
    level = int(m.group(1)) if m else None
    species = re.sub(r"\b(?:lv|level)\s*\d+\b", "", head, flags=re.I).strip()
    return {"species": species, "level": level, "item": item.strip(),
            "moves": [x.strip() for x in moves.split(",") if x.strip()]}


def _run(args) -> int:
    if args.cmd == "extract":
        Project.create(args.rom, args.out, force=args.force)
        print(f"project ready in {args.out} – see {Path(args.out) / 'AGENTS.md'}")
        return 0
    if args.cmd == "scan":
        from .discovery import discover
        from .rom import Rom
        rom = Rom.load(args.rom)
        d = discover(rom)
        print(f"{rom.info.name} [{rom.info.code}] '{rom.info.title}' rev {rom.info.version}")
        row = lambda name, off, n: print(f"  {name:<28} {('0x%06X' % off) if off is not None else '-':>10}  {n} entries")
        for k, t in d.mon_pic_tables.items():
            row(f"pokemon {k} pics", t.offset, len(t.entries))
        for name, t in (("pokemon palettes", d.mon_palettes), ("pokemon shiny palettes", d.mon_shiny_palettes),
                        ("pokemon icon palettes", d.mon_icon_palettes), ("trainer pics", d.trainer_pics),
                        ("trainer palettes", d.trainer_palettes), ("battle anim sprites", d.anim_pics),
                        ("battle anim palettes", d.anim_palettes)):
            row(name, t.offset if t else None, len(t.entries) if t else 0)
        print(f"  {'species names':<28} {len(d.species_names):>10}  ({', '.join(d.species_names[1:4])}…)")
        print(f"  {'pokemon icons':<28} {len(d.mon_icons):>10}")
        print(f"  {'item icons':<28} {len(d.item_icons):>10}  ({len(d.item_names)} names)")
        print(f"  {'anim sprite frame sizes':<28} {len(d.anim_frame_dims):>10}  (from sprite templates)")
        print(f"  {'battle anim backgrounds':<28} {len(d.anim_backgrounds):>10}")
        print(f"  {'battle backgrounds':<28} {len(d.battle_backgrounds):>10}")
        print(f"  {'overworld graphics':<28} {len(d.ow_graphics):>10}  ({len(d.ow_graphics_ids)} with ids, "
              f"{len(d.ow_palettes)} palettes)")
        print(f"  {'map tilesets':<28} {len(d.tilesets):>10}  ({len(d.tileset_pairs)} secondary/primary pairs)")
        print(f"  {'LZ77 streams referenced':<28} {len(d.lz_blobs):>10}")
        for n in d.notes:
            print(f"  note: {n}")
        return 0
    if args.cmd == "apply-patch":
        from . import patch
        src = Path(args.rom).read_bytes()
        data = Path(args.patch).read_bytes()
        out = patch.apply_bps(src, data) if data[:4] == b"BPS1" else patch.apply_ups(src, data) \
            if data[:4] == b"UPS1" else patch.apply_ips(src, data)
        Path(args.out).write_bytes(out)
        print(f"wrote {args.out}")
        return 0

    proj = open_project(Path(args.project), log=lambda m: print(m, file=sys.stderr))
    decomp = proj.kind == "decomp"
    if args.cmd == "list":
        rows = []
        for a in sorted(proj.assets.values(), key=lambda a: a.id):
            if args.category and a.category != args.category:
                continue
            if args.kind and a.kind != args.kind:
                continue
            if args.query and args.query.lower() not in (a.id + " " + a.label).lower():
                continue
            rows.append({"id": a.id, "kind": a.kind, "label": a.label, "file": a.file, "aliases": len(a.aliases)})
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            for r in rows:
                extra = f"  (+{r['aliases']} aliases)" if r["aliases"] else ""
                print(f"{r['id']:<48} {r['kind']:<8} {r['label']}{extra}")
            print(f"{len(rows)} asset(s)", file=sys.stderr)
        return 0
    if args.cmd == "describe":
        print(json.dumps(proj.describe(proj.get(args.asset)), indent=2))
        return 0
    if args.cmd == "status":
        st = proj.status()
        if decomp and not args.json:
            for s_ in st:
                print(f"{s_['state']:<9} {s_['id']}")
            if not st:
                print("no changes to graphics")
            return 0
        if args.json:
            print(json.dumps(st, indent=1))
        else:
            for s in st:
                print(f"{s['state']:<9} {s['id']}" + (f"  – {s['error']}" if s.get("error") else ""))
            if not st:
                print("no changes")
        return 0
    if args.cmd in ("validate", "build"):
        if args.cmd == "validate":
            rep = proj.check(only_changed=not args.all) if decomp else proj.build(dry_run=True)
        else:
            if decomp and args.make:
                proj.config["build"]["command"] = args.make
                proj.state_dir.mkdir(exist_ok=True)
                (proj.state_dir / "config.json").write_text(json.dumps({"build": {"command": args.make}}, indent=2))
                _exclude_state_dir(proj.root)
            patches = [] if args.no_patch else args.patch
            rep = proj.build(args.output, patches=patches)
        _print_report(rep, args.json)
        if args.cmd == "validate" and rep.ok and not args.json:
            print("all edits are valid")
        return 0 if rep.ok else 1
    if args.cmd == "import":
        notes = proj.import_image(proj.get(args.asset), args.source, new_palette=args.new_palette, resize=args.resize)
        for n in notes:
            print(f"note: {n}")
        print(f"imported into {proj.get(args.asset).file}")
        return 0
    if args.cmd == "import-dir":
        root = Path(args.folder)
        n = 0
        for f in sorted(root.rglob("*")):
            if f.suffix.lower() not in (".png", ".pal", ".json") or not f.is_file():
                continue
            ident = f.relative_to(root).with_suffix("").as_posix()
            try:
                a = proj.get(ident)
                for note in proj.import_image(a, f, new_palette=args.new_palette, resize=args.resize):
                    print(f"note: {a.id}: {note}")
                n += 1
            except ProjectError as e:
                print(f"skipped {f}: {e}")
        print(f"imported {n} file(s); run `pae validate` then `pae build`")
        return 0
    if args.cmd == "revert":
        if decomp:
            # any file under graphics/, data/tilesets, data/maps, data/layouts; new files are deleted
            changed = proj.changed_files()
            files = list(changed) if args.all else []
            for ident in args.assets:
                rel = ident.removeprefix(str(proj.root) + "/")
                if rel in changed:
                    files.append(rel)
                else:
                    files.extend(proj._files_of(proj.get(ident)))
            for f in dict.fromkeys(files):
                if f in changed:
                    proj.revert_file(f)
            print(f"reverted {len(set(files) & set(changed))} file(s)")
            return 0
        targets = list(proj.assets.values()) if args.all else [proj.get(a) for a in args.assets]
        n = 0
        for a in targets:
            if args.all and not proj.is_touched(a) and proj.path(a).exists():
                continue
            proj.revert(a)
            n += 1
        print(f"reverted {n} asset(s)")
        return 0
    if args.cmd == "set":
        if decomp:
            raise ProjectError("`set` only applies to ROM projects; in a source tree the layout comes from the files")
        a = proj.get(args.asset)
        proj.relayout(a, args.frame_w, args.frame_h, args.cols, args.tiles_wide, args.palette, args.label, args.force)
        print(json.dumps(describe(proj, a)["layout"]))
        return 0
    if args.cmd == "sync-palettes":
        if decomp:
            ids = [proj.get(x).id for x in args.palettes] or [f for f in proj.changed_files() if f.endswith(".pal")]
        else:
            ids = [proj.get(x).id for x in args.palettes] or [a.id for a in proj.assets.values()
                                                              if a.kind == "palette" and proj.is_touched(a)]
        for pid in ids:
            for aid in proj.sync_palette_views(pid):
                print(f"recoloured {aid}")
        return 0
    if args.cmd == "fix":
        done = proj.fix(args.files or None) if decomp else proj.normalize()
        print("\n".join(f"converted {d}" for d in done) if done else "nothing to convert")
        return 0
    if args.cmd == "palette-from-png":
        if not decomp:
            raise ProjectError("in a ROM project, PNG palette edits are picked up by `pae build` automatically")
        for f in args.images:
            print(f"updated {proj.palette_from_png(proj.get(f))}")
        return 0
    if args.cmd == "checkpoint":
        if not decomp:
            raise ProjectError("checkpoints belong to source-tree play-testing")
        from .playtest import PlaytestError
        try:
            if args.action == "list":
                cps = proj.play.checkpoints()
                for c in cps:
                    where = f"{c.get('map')} ({c.get('x')},{c.get('y')})" if c.get("map") else ""
                    print(f"{c['name']:<24} {c.get('created', ''):<17} {where:<32} {c.get('note', '')}")
                if not cps:
                    print("no checkpoints yet: `pae play MAP --inputs ... --save-as NAME` or `pae checkpoint import NAME FILE.sav`")
            elif args.action == "import":
                if not args.name or not args.file:
                    raise ProjectError("usage: pae checkpoint import NAME FILE.sav")
                print(f"imported {proj.play.import_save(args.file, args.name)['name']}")
            else:
                proj.play.delete_checkpoint(args.name or "")
                print(f"deleted {args.name}")
        except PlaytestError as e:
            raise ProjectError(str(e))
        return 0
    if args.cmd == "doctor":
        return _doctor(proj, fix=args.fix, yes=args.yes)
    if args.cmd == "play":
        if not decomp:
            raise ProjectError("`pae play` needs a source tree (it plays the ROM that `make` builds)")
        from .playtest import PlaytestError, save_gif
        if not args.no_build:
            rep = proj.build_rom()
            if rep.returncode != 0:
                print("\n".join(rep.output.rstrip().splitlines()[-25:]))
                print(f"make failed (exit {rep.returncode})")
                return 1
            print(f"built in {rep.seconds:.0f}s")
        try:
            name = (proj.maps.resolve(args.map) if proj.maps else args.map) if args.map else None
            state = args.state
            if proj.maps and (args.chapter is not None or args.team):
                state = proj.maps.story.play_state(args.chapter - 1 if args.chapter else None, args.extras,
                                                   team=args.team, clear_team=args.clear_team, state=args.state)
            first = proj.play.start(name, args.x, args.y, checkpoint=args.checkpoint, state=state,
                                    script=args.script)
            for line in proj.play.where.get("state") or []:
                print(f"state: {line}")
            if args.battle or args.wild:
                sp, _, lv = (args.wild or "").partition(":")
                first = proj.play.start_battle(trainer=args.battle, species=sp or None, level=int(lv or 5))
                print(f"battle: {proj.play.where.get('battle')}")
            final, frames = proj.play.press(args.inputs, record_every=4 if args.gif else 0) if args.inputs else (first, [])
            if args.save_as:
                print(f"saved checkpoint {proj.play.save_checkpoint(args.save_as)['name']}")
        except PlaytestError as e:
            raise ProjectError(str(e))
        final.resize((final.width * args.scale, final.height * args.scale)).save(args.out)
        print(f"wrote {args.out} ({proj.play.where})")
        if args.gif:
            save_gif([first] + frames, args.gif, args.scale)
            print(f"wrote {args.gif} ({len(frames) + 1} frames)")
        proj.play.close()
        return 0
    if args.cmd == "map":
        m = getattr(proj, "maps", None)
        if m is None:
            raise ProjectError("map commands need a pokeemerald-style source tree with data/maps")
        if args.map_cmd == "list":
            for x in m.list_maps():
                if args.query.lower() in (x["name"] + x["id"]).lower():
                    print(f"{x['name']:<44} {x['id']:<48} {x['group'].replace('gMapGroup_', '')}")
        elif args.map_cmd == "render":
            m.render(args.name, events=not args.no_events, grid=args.grid, scale=args.scale).save(args.out)
            print(f"wrote {args.out}")
        elif args.map_cmd == "metatiles":
            m.metatile_sheet(args.name, 2).save(args.out)
            print(f"wrote {args.out}")
        elif args.map_cmd == "events":
            data = m.get_map(args.name)
            for kind in ("object_events", "warp_events", "coord_events", "bg_events"):
                for i, e in enumerate(data.get(kind, [])):
                    what = e.get("graphics_id") or e.get("dest_map") or e.get("type", "")
                    extra = e.get("script") or e.get("dest_warp_id", "")
                    print(f"{kind[:-7]:<7} {i:>3}  ({e.get('x')},{e.get('y')})  {what}  {extra}")
        elif args.map_cmd == "new":
            for n in m.duplicate_map(args.source, args.new_name, args.group, not args.share_layout, args.id):
                print(f"note: {n}")
            print(f"created {args.new_name} ({m.get_map(args.new_name)['id']})")
        elif args.map_cmd == "tiles":
            print(json.dumps(m.tileset_editor.info(args.name), indent=2))
        elif args.map_cmd == "import-art":
            base = int(args.base, 0) if args.base else None
            lay = m.layout_of(args.name)
            if base is None and args.x is not None and args.y is not None:
                base = int(m.read_blocks(lay)[args.y, args.x]) & 0x3FF
            r = m.tileset_editor.import_image(args.name, args.image, base, args.bank, args.new_palette)
            for n in r["notes"]:
                print(f"note: {n}")
            rows = [r["ids"][y * r["w"]:(y + 1) * r["w"]] for y in range(r["h"])]
            print(f"stamp {r['w']}x{r['h']} metatiles (palette {r['bank']}):")
            for row in rows:
                print("  " + " ".join(f"{i:03x}" for i in row))
            if args.x is not None and args.y is not None:
                b = m.read_blocks(lay).copy()
                for j, row in enumerate(rows):
                    for i, mid in enumerate(row):
                        yy, xx = args.y + j, args.x + i
                        if 0 <= yy < b.shape[0] and 0 <= xx < b.shape[1]:
                            v = (int(b[yy, xx]) & ~0x3FF) | mid
                            if args.solid:
                                v = (v & ~0x0C00) | (1 << 10)
                            b[yy, xx] = v
                m.write_blocks(args.name, b)
                print(f"placed at ({args.x}, {args.y}) on {args.name}")
        elif args.map_cmd == "link":
            m.link_warps(args.map, args.warp, args.dest_map, args.dest_warp)
            print(f"linked {args.map} warp {args.warp} <-> {args.dest_map} warp {args.dest_warp}")
        return 0
    if args.cmd in ("savepoint", "history"):
        from .history import History
        if not decomp:
            raise ProjectError("save points need a source tree")
        h = History(proj)
        if args.cmd == "savepoint":
            p = h.save(" ".join(args.message))
            print(f"save point {p['short']}: {p['message']} ({p['files']} file(s))")
            return 0
        if args.action == "list":
            n = len(h.unsaved())
            print(f"{n} file(s) changed since the last save point" if n else "everything is in a save point")
            for p in h.points(30):
                print(f"{p['short']}  {p['date']}  {p['message']}  ({p['files']} file(s))")
        elif args.action == "show":
            d = h.show(args.sha or "HEAD")
            print(d["message"])
            for f in d["files"]:
                print(f"  {f['state']:<8} {f['file']}")
        else:
            if not args.sha:
                raise ProjectError("which save point? (see `pae history`)")
            r = h.go_back(args.sha)
            if r["kept_unsaved"]:
                print(f"kept your unsaved changes in save point {r['kept_unsaved']['short']}")
            print(f"now at: {r['now']['message']}")
        return 0
    if args.cmd == "text":
        m = getattr(proj, "maps", None)
        if m is None:
            raise ProjectError("text checks need a pokeemerald-style source tree")
        tb = proj.textbox
        if args.text_cmd == "find":
            r = proj.textsearch.search(args.query, args.case, args.word)
            for x in r["results"]:
                print(f"{x['file']}{':' + str(x['line']) if x['line'] else ''}: {x['label']}: "
                      + x["text"].replace("\n", " / "))
            print(f"{r['total']} match(es) in {len(r['results'])} text(s)")
            return 0
        if args.text_cmd == "replace":
            r = proj.textsearch.replace(args.query, args.replacement, args.case, args.word, dry_run=args.dry_run)
            for c in r["changes"]:
                print(f"{c['file']}: {c['label']}{' (re-wrapped)' if c['rewrapped'] else ''}")
                print("   " + c["after"].replace("\n", " / "))
                for p in c["problems"]:
                    print(f"   ⚠ {p}")
            verb = "would change" if args.dry_run else "changed"
            print(f"{verb} {len(r['changes'])} text(s) in {len(r['files'])} file(s)")
            return 0
        if args.text_cmd == "preview":
            name = m.resolve(args.map)
            hit = [t for t in tb.texts(m.read_script(name)) if t.label == args.label]
            if not hit:
                raise ProjectError(f"no text label {args.label} in {name}")
            tb.render(hit[0]).save(args.out)
            for msg in hit[0].problems:
                print(f"problem: {msg}")
            print(f"wrote {args.out}")
            return 0
        names = [m.resolve(args.map)] if args.map else list(m.maps)
        count = 0
        for name in names:
            try:
                script = m.read_script(name)
            except OSError:
                continue
            for t in tb.texts(script):
                for msg in t.problems:
                    count += 1
                    print(f"{m.script_path(name).relative_to(proj.root)}:{t.line}: {t.label}: {msg}")
        print(f"{count} problem(s)" if count else "every text fits its box")
        return 1 if count else 0
    if args.cmd == "new-pokemon":
        if not decomp:
            raise ProjectError("adding Pokémon needs a pokeemerald-expansion source tree")
        ch = {}
        if args.types:
            ch["types"] = [t.strip() for t in args.types.split(",") if t.strip()]
        if args.stats:
            vals = [int(x) for x in args.stats.split(",")]
            ch.update(dict(zip(("baseHP", "baseAttack", "baseDefense", "baseSpAttack", "baseSpDefense", "baseSpeed"),
                               vals)))
        if args.category:
            ch["category"] = args.category
        if args.description:
            ch["description"] = args.description.replace("\\n", "\n")
        evo = None
        if args.evolves_from:
            sp, _, lv = args.evolves_from.partition(":")
            evo = {"species": sp, "method": "EVO_LEVEL", "param": int(lv or 16)}
        images = {k: Path(getattr(args, k)).read_bytes() for k in ("front", "back", "icon") if getattr(args, k)}
        r = proj.newmon.create(args.name, args.template, ch, evo, images)
        for w in r["warnings"]:
            print("⚠ " + w)
        print(f"added {r['name']} ({r['species']}), made from {r['template']}; graphics in {r['graphics']}/")
        print(f"changed {len(r['files'])} file(s); build the game to meet it")
        return 0
    if args.cmd == "release":
        if not decomp:
            raise ProjectError("releases are made from a pokeemerald-expansion source tree (ROM projects: pae build)")
        rel = proj.release
        settings = {k: getattr(args, k) for k in ("name", "version", "author", "description") if getattr(args, k)}
        if args.base:
            settings["base_rom"] = str(Path(args.base).resolve())
        if settings:
            rel.save_settings(settings)
        for c in rel.checks():
            print(("⛔ " if c["level"] == "error" else "⚠ ") + c["message"])
        print(f"building with: {' '.join(rel.make_command())}" if not args.no_build else "using the built release ROM")
        r = rel.build(notes=args.notes.replace("\\n", "\n") if args.notes else None, build=not args.no_build,
                      force=args.force, formats=("bps", "ups") if args.ups else ("bps",))
        print(f"made {r['zip']} ({r['zip_size'] // 1024} KB) in {r['seconds']}s")
        for f in r["files"]:
            print(f"  {f} ({r['sizes'][f] // 1024} KB)")
        return 0
    if args.cmd == "difficulty":
        if not decomp:
            raise ProjectError("the difficulty curve needs a pokeemerald-expansion source tree")
        d = proj.gamedata.difficulty()
        for c in d["chapters"]:
            boss = f"{c['boss']['name']} lv {c['boss']['ace']}" if c["boss"] else "-"
            wild = f"lv {c['wild']['min']}-{c['wild']['max']}" if c["wild"] else "-"
            print(f"{c['index'] + 1:>2}. {c['name'][:44]:44}  trainers {len(c['trainers']):>3}  typical lv "
                  f"{c['median'] if c['median'] is not None else '-':>3}  boss {boss:18}  wild {wild}")
        if d["flags"]:
            print(f"\n{len(d['flags'])} thing(s) worth a look:")
            for f in d["flags"]:
                print(f"  chapter {f['chapter'] + 1}: {f['message']}")
        return 0
    if args.cmd in ("item", "move", "tm", "mart"):
        if not decomp:
            raise ProjectError("game data editing needs a pokeemerald-expansion source tree")
        gd = proj.gamedata
        if args.cmd == "item":
            if args.item_cmd == "list":
                for it in gd.items(args.query):
                    print(f"{it['id']:32} {it['name']}")
                return 0
            if args.item_cmd == "set":
                ch = {k: getattr(args, k) for k in ("name", "price", "pocket") if getattr(args, k) is not None}
                if args.description is not None:
                    ch["description"] = args.description.replace("\\n", "\n")
                gd.set_item(args.id, ch)
            d = gd.get_item(args.id)
            print(f"{d['name']} ({d['id']})  price {d['price']}  {d['pocket']}")
            print("  " + d["description"].replace("\n", "\n  "))
            if d["sold_in"]:
                print("  sold in: " + ", ".join(sorted({m['map'] for m in d['sold_in']})))
            return 0
        if args.cmd == "move":
            if args.move_cmd == "list":
                for mv in gd.moves(args.query):
                    print(f"{mv['id']:32} {mv['name']}")
                return 0
            if args.move_cmd == "set":
                ch = {k: getattr(args, k) for k in ("name", "power", "accuracy", "pp", "priority", "type", "category")
                      if getattr(args, k) is not None}
                if args.description is not None:
                    ch["description"] = args.description.replace("\\n", "\n")
                gd.set_move(args.id, ch)
            d = gd.get_move(args.id)
            print(f"{d['name']} ({d['id']})  {d['type'].replace('TYPE_', '').title()}  "
                  f"{d['category'].replace('DAMAGE_CATEGORY_', '').title()}{'  ' + d['tm'] if d['tm'] else ''}")
            print(f"  power {d['power']}  accuracy {d['accuracy']}  PP {d['pp']}  priority {d['priority']}")
            print("  " + d["description"].replace("\n", "\n  "))
            if d["learned_by"]:
                print("  learned by: " + ", ".join(f"{x['name']} (lv {x['level']})" for x in d["learned_by"][:20]))
            return 0
        if args.cmd == "tm":
            t = gd.tms()
            if args.tm_cmd == "set":
                key = args.kind + "s"
                moves = [x["move"] for x in t[key]]
                i = args.number - 1
                if not 0 <= i <= len(moves):
                    raise ProjectError(f"{args.kind.upper()}{args.number:02d}: there are {len(moves)}; "
                                       f"use {len(moves) + 1} to add one")
                from .gamedata import const_of
                moves[i:i + 1] = [const_of("MOVE", args.move)]
                t = gd.set_tms(**{key: moves})
            for kind in ("tms", "hms"):
                for i, x in enumerate(t[kind], 1):
                    print(f"{kind[:2].upper()}{i:02d}  {x['name']}")
            tut = gd.tutors()
            if tut:
                print("tutors: " + ", ".join(f"{x['name']} ({x['map'] or 'extra'})" for x in tut))
            return 0
        if args.mart_cmd == "set":
            gd.set_mart(args.label, args.items)
        for m in gd.marts():
            if args.mart_cmd == "set" and m["label"] != args.label:
                continue
            print(f"{m['label']} ({m['map']}): " + ", ".join(gd.display(i) for i in m["items"]))
        return 0
    if args.cmd in ("trainer", "wild", "species"):
        if not decomp:
            raise ProjectError("game data editing needs a pokeemerald-expansion source tree")
        gd = proj.gamedata
        if args.cmd == "trainer":
            if args.trainer_cmd == "list":
                for t in gd.trainers(args.query):
                    print(f"{t['id']:<40} {t['name']:<12} {t['class']:<18} " + ", ".join(
                        f"{n} {lv}" if lv else n for n, lv in zip(t["party"], t["levels"])))
                return 0
            if args.trainer_cmd == "set":
                body = {}
                if args.mon:
                    body["mons"] = [_parse_mon_arg(m) for m in args.mon]
                if args.field:
                    cur = gd.get_trainer(args.id)["fields"]
                    new = dict(f.split("=", 1) for f in args.field)
                    body["fields"] = [{"key": f["key"], "value": new.pop(f["key"], f["value"])} for f in cur] + \
                        [{"key": k, "value": v} for k, v in new.items()]
                gd.set_trainer(args.id, body)
            t = gd.get_trainer(args.id)
            for f in t["fields"]:
                print(f"{f['key']}: {f['value']}")
            for m in t["mons"]:
                item = f" @ {m['item_name']}" if m["item"] else ""
                print(f"  {m['species_name']} lv{m['level']}{item}: {', '.join(m['move_names']) or '(default moves)'}")
            if t["used_on"]:
                print("battled in: " + ", ".join(sorted({u['map'] for u in t['used_on']})))
            return 0
        if args.cmd == "wild":
            if args.wild_cmd == "set":
                mons = []
                for slot in args.slots:
                    sp, _, lv = slot.partition(":")
                    lo, _, hi = (lv or "5").partition("-")
                    mons.append({"species": sp, "min_level": int(lo), "max_level": int(hi or lo)})
                gd.set_wild(args.map, f"{args.kind}_mons", args.rate, mons)
            w = gd.wild(args.map)
            if not w["entries"]:
                print(f"{w['map']}: no wild Pokémon")
            for e in w["entries"]:
                for k, v in e["kinds"].items():
                    if v["mons"]:
                        slots = ", ".join(f"{m['name']} {m['min_level']}-{m['max_level']} ({r}%)"
                                          for m, r in zip(v["mons"], v["slot_rates"]))
                        print(f"{v['label']} (rate {v['encounter_rate']}): {slots}")
            return 0
        if args.species_cmd == "set":
            changes = {k: getattr(args, k) for k in ("baseHP", "baseAttack", "baseDefense", "baseSpAttack",
                                                     "baseSpDefense", "baseSpeed", "catchRate", "expYield")
                       if getattr(args, k) is not None}
            if args.types:
                changes["types"] = [t.strip() for t in args.types.split(",") if t.strip()]
            if args.abilities is not None:
                changes["abilities"] = [a.strip() or "ABILITY_NONE" for a in args.abilities.split(",")]
            if args.learn:
                changes["learnset"] = [{"level": int(x.split(":", 1)[0]), "move": x.split(":", 1)[1]} for x in args.learn]
            gd.set_species(args.id, changes)
        d = gd.get_species(args.id)
        print(f"{d['name']} ({d['id']})  {' / '.join(t.replace('TYPE_', '').title() for t in dict.fromkeys(d['types']))}")
        print("  " + "  ".join(f"{k.replace('base', '')} {v}" for k, v in d["stats"].items()) + f"  (total {d['total']})")
        print("  abilities: " + ", ".join(n for a, n in zip(d["abilities"], d["ability_names"]) if a != "ABILITY_NONE"))
        print(f"  catch rate {d['catchRate']}, exp {d['expYield']}")
        for e in d["evolutions"]:
            print(f"  evolves into {e['target_name']} ({e['method']} {e['param']})")
        print("  learns: " + ", ".join(f"{m['level']} {m['name']}" for m in d["learnset"]))
        return 0
    if args.cmd == "story":
        m = getattr(proj, "maps", None)
        if m is None:
            raise ProjectError("story chapters need a pokeemerald-style source tree")
        st = m.story
        c = args.story_cmd
        if c == "list":
            for i, ch in enumerate(st.chapters(), 1):
                flags = f"  (then: {', '.join(ch['flags'])})" if ch["flags"] else ""
                print(f"{i:>2}. {ch['name']}{flags}")
                print(f"      {', '.join(ch['maps']) or '(no maps yet)'}")
            rest = st.unplaced_maps()
            print(f"\n{len(rest)} map(s) are not in any chapter (`pae story move MAP N` to place one)")
        elif c == "items":
            r = st.catch_up(args.chapter - 1, args.extras)
            print(f"by the end of '{r['name']}':")
            for it in r["items"]:
                tag = "needed" if it["needed"] else "extra "
                print(f"  {tag}  {it['name']:<20} x{it['qty']:<3} {it['kind']:<12} {it['map']}")
            if r["flags"]:
                print(f"  and turns on: {', '.join(r['flags'])}")
        elif c == "move":
            ch = None if args.chapter.lower() == "none" else int(args.chapter) - 1
            st.set_chapter(args.map, ch)
            print(f"{m.resolve(args.map)} -> " + ("not in the story" if ch is None else st.chapters()[ch]["name"]))
        elif c == "suggest":
            sg = st.suggest_chapters()
            for x in sg:
                print(f"{x['map']:<40} -> {x['chapter'] + 1}. {x['chapter_name']}  (next to {x['via']})")
            if args.apply:
                print(f"placed {st.apply_suggestions()} map(s)")
            elif not sg:
                print("no suggestions")
        elif c == "flag":
            u = st.flow()["usage"].get(args.name.upper())
            if not u:
                print(f"no event uses {args.name.upper()}")
            else:
                for title, key in (("changed by", "set_by"), ("used by", "checked_by")):
                    print(title + ":")
                    for r in u[key]:
                        print(f"  {r['map']}: {r['how']} ({r['label']})")
        elif c == "add":
            i = st.add_chapter(args.name, args.at - 1 if args.at else None, args.flag)
            print(f"added chapter {i + 1}: {args.name}")
        return 0
    if args.cmd == "anim":
        m = getattr(proj, "maps", None)
        if m is None:
            raise ProjectError("tileset animations need a pokeemerald-style source tree")
        ta = m.tileset_anims
        c = args.anim_cmd
        if c == "list":
            for a in ta.list(args.map):
                speed = f"every {a['period']:>3}f" if a["period"] else "custom    "
                tiles = ", ".join(f"{s['dest']}" for s in a["slots"][:2]) + ("…" if len(a["slots"]) > 2 else "")
                print(f"{a['id']:<38} {len(a['frames']):>2} frames  plays {len(a['sequence']):>2}  {speed}  "
                      f"{a['count']:>3} tiles @ {tiles:<10} {', '.join(t.replace('gTileset_', '') for t in a['tilesets'])}")
            for pa in ta.parse()["palette"] if args.map is None else []:
                print(f"{pa['function']:<38} palette animation (not editable here)")
        elif c == "show":
            ta.frames_image(args.id, args.map).save(args.out)
            print(f"wrote {args.out}")
        elif c == "preview":
            region = tuple(int(v) for v in args.region.split(",")) if args.region else None
            frames, ms = ta.map_frames(args.map, region)
            from PIL import Image
            if args.scale > 1:
                frames = [f.resize((f.width * args.scale, f.height * args.scale), Image.NEAREST) for f in frames]
            frames[0].save(args.out, save_all=True, append_images=frames[1:], duration=ms, loop=0, disposal=1)
            print(f"wrote {args.out} ({len(frames)} frames)")
        elif c == "speed":
            a = ta.set_speed(args.id, args.frames)
            print(f"{a.id}: a new frame every {a.period} game frames")
        elif c == "order":
            a = ta.set_sequence(args.id, [None if v.strip() in ("-", "NULL") else int(v) for v in args.order.split(",")])
            print(f"{a.id}: plays {' '.join('-' if i is None else str(i) for i in a.sequence)}")
        elif c == "frame":
            for n in ta.set_frame(args.id, args.frame, args.image, args.map):
                print(f"note: {n}")
            print(f"updated frame {args.frame} of {args.id}")
        elif c == "add-frame":
            r = ta.add_frame(args.id, args.image, args.copy, args.at, args.map)
            print(f"added frame {r['frame']} ({r['png']}); plays {r['anim']['sequence']}")
        elif c == "remove-frame":
            for n in ta.remove_frame(args.id, args.frame):
                print(f"note: {n}")
            print(f"removed frame {args.frame} from {args.id}")
        elif c == "new":
            r = ta.create(args.map, args.name, int(args.tile, 0), args.count, args.frames, args.speed, args.images)
            for n in r["notes"]:
                print(f"note: {n}")
            print(f"created {r['anim']['id']}: frames in {', '.join(r['pngs'])}")
        return 0
    if args.cmd == "guide":
        from .previews import agents_guide, decomp_guide
        print(decomp_guide(proj) if decomp else agents_guide(proj))
        return 0
    if args.cmd == "sheet":
        from .previews import contact_sheet
        pre = args.prefix.rstrip("/")
        sel = [a for a in sorted(proj.assets.values(), key=lambda a: a.id)
               if (not pre or a.id.startswith(pre) or a.category == pre)
               and (not args.kind or a.kind == args.kind)]
        if not args.kind:
            sel = [a for a in sel if a.kind != "palette"] or sel
        sel = sel[: args.limit]
        contact_sheet(proj, sel, scale=args.scale, max_width=args.width).save(args.out)
        print(f"wrote {args.out} ({len(sel)} assets)")
        return 0
    if args.cmd == "render":
        from PIL import Image
        a = proj.get(args.asset)
        pal = proj.get(args.palette).id if args.palette else None
        im = proj.render_image(a, palette_override=pal)
        if args.scale > 1:
            im = im.resize((im.width * args.scale, im.height * args.scale), Image.NEAREST)
        im.save(args.out)
        print(f"wrote {args.out}")
        return 0
    if args.cmd == "serve":
        from .server import serve
        serve(proj, args.host, args.port)
        return 0
    if args.cmd == "mcp":
        from .mcp import run
        run(proj)
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    sys.exit(main())



def _doctor(proj, fix: bool = False, yes: bool = False) -> int:
    """Report what building and play-testing need, what is missing, and (with fix) fix it."""
    import re
    import shutil
    import subprocess

    if proj.kind != "decomp":
        print("ROM projects need nothing else: `pae build` writes the ROM directly.")
        return 0
    from .playtest import PlaytestError, driver_path, find_rom
    setup = Path(__file__).parent / "tools" / "setup-toolchain.sh"

    def run_setup():
        subprocess.run(["bash", str(setup)], check=False)

    def has_png():
        return any(Path(p).exists() for p in ("/usr/include/png.h", "/usr/local/include/png.h",
                                              "/opt/homebrew/include/png.h"))

    def has_mgba():
        try:
            driver_path(proj.root / ".pae" / "cache")
            return True
        except PlaytestError:
            return False

    cfg = proj.root / "include" / "config" / "quickstart.h"

    def quickstart_on():
        return cfg.exists() and bool(re.search(r"#define\s+ENABLE_QUICKSTART\s+TRUE", cfg.read_text()))

    def turn_quickstart_on():
        proj._backup(str(cfg.relative_to(proj.root)))
        cfg.write_text(re.sub(r"(#define\s+ENABLE_QUICKSTART\s+)FALSE", r"\1TRUE", cfg.read_text()))

    def is_git():
        r = subprocess.run(["git", "-C", str(proj.root), "rev-parse", "--is-inside-work-tree"], capture_output=True,
                           text=True)
        return r.returncode == 0 and r.stdout.strip() == "true"

    def git_init():
        from .history import AUTHOR
        subprocess.run(["git", "-C", str(proj.root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(proj.root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(proj.root), *AUTHOR, "commit", "-qm", "First save point"], check=False)

    def has_rom():
        try:
            find_rom(proj.root)
            return True
        except PlaytestError:
            return False

    def build():
        rep = proj.build_rom()
        print("\n".join(rep.output.rstrip().splitlines()[-8:]))

    tools = all(shutil.which(t) for t in ("make", "arm-none-eabi-gcc", "cc"))
    checks = [
        ("Tools that build your game (make, a C compiler, arm-none-eabi-gcc)", lambda: tools and has_png(),
         "install them", run_setup),
        ("The test-play emulator (libmgba)", has_mgba, "install it", run_setup),
        ("Quickstart (needed to test from a brand-new game)", quickstart_on,
         "set ENABLE_QUICKSTART to TRUE in include/config/quickstart.h", turn_quickstart_on if cfg.exists() else None),
        ("Save points (the game folder is a git repository)", is_git, "run `git init` and make a first save point",
         git_init),
        ("Your game is built (.gba and .elf)", has_rom, "build it with `make` (the first time takes a few minutes)",
         build),
    ]
    missing = 0
    for name, ok, how, action in checks:
        good = ok()
        if not good and fix and action is not None:
            answer = "y" if yes else input(f"{name}: missing. Fix it now ({how})? [Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                action()
                if name.startswith("Tools"):
                    tools = all(shutil.which(t) for t in ("make", "arm-none-eabi-gcc", "cc"))
                    good = tools and has_png()
                else:
                    good = ok()
        print(f"{'✅' if good else '❌'}  {name}" + ("" if good else f"\n      -> {how}" +
                                                       ("" if fix else " (or run `pae doctor --fix`)")))
        missing += not good
    print("\nEverything is ready. Run `pae serve` and open http://localhost:8765" if not missing else
          f"\n{missing} thing(s) still missing.")
    return 0 if not missing else 1

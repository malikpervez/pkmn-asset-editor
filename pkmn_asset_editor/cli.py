"""Command line interface: ``pae <command>``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .decomp import DecompError, open_project
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
    p.add_argument("--patch", action="append", choices=["ups", "ips"], help="patch format(s) to write")
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

    p = with_project(sub.add_parser("map", help="source tree: maps, events and scripts (see `pae map -h`)"))
    msub = p.add_subparsers(dest="map_cmd", required=True)
    q = msub.add_parser("list", help="list maps")
    q.add_argument("query", nargs="?", default="")
    q = msub.add_parser("render", help="render a map (with events) to a PNG")
    q.add_argument("name")
    q.add_argument("-o", "--out", required=True)
    q.add_argument("--scale", type=int, default=1)
    q.add_argument("--no-events", action="store_true")
    q.add_argument("--grid", action="store_true")
    q = msub.add_parser("metatiles", help="render the metatiles a map can use (with ids)")
    q.add_argument("name")
    q.add_argument("-o", "--out", required=True)
    q = msub.add_parser("events", help="print a map's events")
    q.add_argument("name")
    q = msub.add_parser("new", help="create a new map as a copy of an existing one (e.g. a new house)")
    q.add_argument("source")
    q.add_argument("new_name")
    q.add_argument("--group")
    q.add_argument("--share-layout", action="store_true", help="reuse the source's blocks instead of copying them")
    q.add_argument("--id", help="map id constant (default derived from the name)")
    q = msub.add_parser("link", help="connect two warps in both directions")
    q.add_argument("map")
    q.add_argument("warp", type=int)
    q.add_argument("dest_map")
    q.add_argument("dest_warp", type=int)

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
        out = patch.apply_ups(src, data) if data[:4] == b"UPS1" else patch.apply_ips(src, data)
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
        elif args.map_cmd == "link":
            m.link_warps(args.map, args.warp, args.dest_map, args.dest_warp)
            print(f"linked {args.map} warp {args.warp} <-> {args.dest_map} warp {args.dest_warp}")
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


def _exclude_state_dir(root: Path) -> None:
    """Keep the tool's .pae/ folder out of git without touching the repository's .gitignore."""
    excl = root / ".git" / "info" / "exclude"
    try:
        if excl.parent.exists():
            text = excl.read_text() if excl.exists() else ""
            if ".pae/" not in text:
                excl.write_text(text + ("\n" if text and not text.endswith("\n") else "") + ".pae/\n")
    except OSError:
        pass

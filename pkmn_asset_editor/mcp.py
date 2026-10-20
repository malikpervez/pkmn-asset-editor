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
    ("status", "List assets that were edited.", _schema({})),
    ("validate", "Check every edit (sizes, colours, conflicts, tile limits) without writing a ROM.", _schema({})),
    ("build_rom", "ROM project: write the edited ROM and UPS/IPS patches. Source tree: check the edits, then run "
     "`make` and return its output.", _schema({"output": S})),
]


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

"""A small local web editor (no external dependencies)."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from .decomp import DecompError
from .project import ProjectError, file_sha1

ERRORS = (ProjectError, DecompError, ValueError, KeyError)

WEB = Path(__file__).with_name("web")


class _State:
    def __init__(self, proj):
        self.proj = proj
        self.lock = threading.RLock()
        self._sha: dict[str, tuple[float, int, str]] = {}

    def modified(self, asset) -> bool:
        if self.proj.kind == "decomp":
            return self.proj.is_touched(asset)
        p = self.proj.path(asset)
        try:
            st = p.stat()
        except FileNotFoundError:
            return True
        key = str(p)
        hit = self._sha.get(key)
        if hit is None or hit[0] != st.st_mtime or hit[1] != st.st_size:
            hit = (st.st_mtime, st.st_size, file_sha1(p))
            self._sha[key] = hit
        return hit[2] != asset.sha1


def make_handler(state: _State):
    proj = state.proj

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -------------------------------------------------------------- helpers
        def _send(self, body: bytes, ctype: str, status=HTTPStatus.OK, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status=HTTPStatus.OK):
            self._send(json.dumps(data).encode(), "application/json", status)

        def _error(self, msg, status=HTTPStatus.BAD_REQUEST):
            self._json({"error": msg}, status)

        def _body(self) -> bytes:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 << 20:
                raise ProjectError("upload too large")
            return self.rfile.read(n)

        def _q(self):
            u = urlparse(self.path)
            return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

        # -------------------------------------------------------------- GET
        def do_GET(self):
            path, q = self._q()
            try:
                with state.lock:
                    self._get(path, q)
            except ERRORS as e:
                self._error(str(e))
            except Exception as e:  # noqa: BLE001
                self._error(f"{type(e).__name__}: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

        def _get(self, path, q):
            if path in ("/", "/index.html"):
                return self._send((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path in ("/maps", "/maps.html"):
                return self._send((WEB / "maps.html").read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/api/map") or path in ("/api/constants", "/api/labels"):
                return self._map_get(path, q)
            if path == "/api/summary":
                return self._json({"game": proj.config["game"], "categories": proj.summary(),
                                   "notes": proj.config.get("notes", []), "mode": proj.kind})
            if path == "/api/assets":
                cat, query, kind = q.get("category"), (q.get("q") or "").lower(), q.get("kind")
                rows = []
                for a in sorted(proj.assets.values(), key=lambda a: a.id):
                    if cat and a.category != cat or kind and a.kind != kind:
                        continue
                    if query and query not in (a.id + " " + a.label + " " + " ".join(a.aliases)).lower():
                        continue
                    rows.append({"id": a.id, "kind": a.kind, "label": a.label, "category": a.category,
                                 "aliases": a.aliases, "modified": state.modified(a)})
                return self._json(rows)
            if path == "/api/asset":
                a = proj.get(q["id"])
                d = proj.describe(a)
                d["palette_colors"] = {}
                for pid in {l["palette"] for l in a.palettes} | ({a.id} if a.kind == "palette" else set()) | \
                        set(a.meta.get("alt_palettes", [])):
                    try:
                        d["palette_colors"][pid] = proj.palette_colors(pid)
                    except (OSError, *ERRORS):
                        pass
                d["modified"] = state.modified(a)
                return self._json(d)
            if path == "/api/image":
                a = proj.get(q["id"])
                pal = q.get("palette") or None
                im = proj.render_image(a, palette_override=pal)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                return self._send(buf.getvalue(), "image/png")
            if path == "/api/file":
                a = proj.get(q["id"])
                ctype = "text/plain" if a.kind == "palette" else "image/png"
                name = os.path.basename(a.file)
                return self._send(proj.path(a).read_bytes(), ctype,
                                  headers={"Content-Disposition": f'attachment; filename="{name}"'})
            if path == "/api/status":
                return self._json(proj.status())
            if path == "/api/changes":
                if proj.kind != "decomp":
                    return self._json([{"type": "image" if s_["state"] != "missing" else "file", "id": s_["id"],
                                        "file": s_["file"], "state": s_["state"], "views": []} for s_ in proj.status()])
                return self._json(proj.changes())
            if path == "/api/before":
                if "map" in q:
                    im = proj.maps.render_head(q["map"], scale=int(q.get("scale", 1)))
                else:
                    a = proj.get(q["id"])
                    im = proj.render_head(a) if proj.kind == "decomp" else self._rom_before(a)
                if im is None:
                    return self._error("no committed version", HTTPStatus.NOT_FOUND)
                buf = io.BytesIO()
                im.save(buf, "PNG")
                return self._send(buf.getvalue(), "image/png")
            if path == "/api/diff":
                text = proj.diff_text(q["file"]) if proj.kind == "decomp" else ""
                return self._send(text.encode(), "text/plain; charset=utf-8")
            if path == "/api/download":
                which = q.get("what", "rom")
                out = proj.output_rom()
                target = out if which == "rom" or out is None else out.with_suffix("." + which)
                if which not in ("rom", "ups", "ips") or target is None or not target.exists():
                    return self._error("not built yet", HTTPStatus.NOT_FOUND)
                return self._send(target.read_bytes(), "application/octet-stream",
                                  headers={"Content-Disposition": f'attachment; filename="{target.name}"'})
            return self._error("not found", HTTPStatus.NOT_FOUND)

        # -------------------------------------------------------------- POST
        def do_POST(self):
            path, q = self._q()
            try:
                with state.lock:
                    self._post(path, q)
            except ERRORS as e:
                self._error(str(e))
            except Exception as e:  # noqa: BLE001
                self._error(f"{type(e).__name__}: {e}", HTTPStatus.INTERNAL_SERVER_ERROR)

        def _rom_before(self, a):
            if a.kind == "palette":
                return proj.render_image(a, palettes=proj.original_palettes())
            return Image.open(io.BytesIO(self._rom_original_png(a))).convert("RGBA")

        def _rom_original_png(self, a):
            from . import formats
            from .gfx import save_indexed_png
            pixels, plte = formats.render(a, proj.raw_view(), proj.original_palettes())
            buf = io.BytesIO()
            save_indexed_png(buf, pixels, plte, transparent0=a.kind == "sprite" and bool(a.palettes))
            return buf.getvalue()

        def _maps(self):
            m = getattr(proj, "maps", None)
            if m is None:
                raise ProjectError("map editing needs a pokeemerald-style source tree with data/maps")
            return m

        def _map_get(self, path, q):
            m = self._maps()
            if path == "/api/maps":
                return self._json({"groups": m.group_order, "maps": m.list_maps()})
            if path == "/api/map":
                name = m.resolve(q["name"])
                data = m.get_map(name)
                lay = m.layout_of(name)
                atlas = m.layout_atlas(lay)
                script = m.script_path(name)
                return self._json({
                    "name": name, "group": m.maps[name]["group"], "data": data,
                    "layout": {k: lay.get(k) for k in ("id", "width", "height", "primary_tileset", "secondary_tileset",
                                                         "layout_version", "blockdata_filepath")},
                    "blocks": m.read_blocks(lay).ravel().tolist(),
                    "metatiles": {"count": atlas.count, "num_primary": atlas.num_primary,
                                  "behaviors": atlas.behaviors},
                    "script_file": str(script.relative_to(proj.root)),
                    "shared_layout": [n for n, x in m.maps.items() if x["layout"] == lay["id"] and n != name]})
            if path == "/api/map/atlas":
                im = Image.fromarray(m.layout_atlas(m.layout_of(q["name"])).image, "RGBA")
                return self._send(m.png_bytes(im), "image/png")
            if path == "/api/map/render":
                im = m.render(q["name"], events=q.get("events", "1") == "1", scale=int(q.get("scale", 1)))
                return self._send(m.png_bytes(im), "image/png")
            if path == "/api/map/sprite":
                spr = m.object_sprite(q.get("gfx", ""))
                if spr is None:
                    return self._error("no sprite", HTTPStatus.NOT_FOUND)
                return self._send(m.png_bytes(spr), "image/png")
            if path == "/api/map/script":
                name = m.resolve(q["name"])
                return self._json({"file": str(m.script_path(name).relative_to(proj.root)), "text": m.read_script(name)})
            if path == "/api/map/tsinfo":
                te = m.tileset_editor
                info = te.info(q["name"])
                info["behaviors"] = te.behaviors(info["version"])
                info["palettes"] = te.palettes(q["name"])
                return self._json(info)
            if path == "/api/map/metatile":
                return self._json(m.tileset_editor.get_metatile(q["name"], int(q["id"], 0)))
            if path == "/api/map/tilesheet":
                im = m.tileset_editor.tile_sheet(q["name"], int(q.get("bank", 0)), transparent=q.get("alpha") == "1")
                return self._send(m.png_bytes(im), "image/png")
            if path == "/api/map/tile":
                return self._json(m.tileset_editor.get_tile(q["name"], int(q["id"], 0)))
            if path == "/api/constants":
                return self._json(m.constants(q.get("prefix", "")))
            if path == "/api/labels":
                needle = (q.get("q") or "").lower()
                labels = m.labels()
                hits = [k for k in labels if needle in k.lower()][:300]
                return self._json([{"label": k, "file": labels[k]} for k in sorted(hits)])
            return self._error("not found", HTTPStatus.NOT_FOUND)

        def _map_post(self, path, q):
            m = self._maps()
            if path == "/api/map/import_art":
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(self._body())
                try:
                    base = q.get("base")
                    r = m.tileset_editor.import_image(
                        q["name"], tmp.name, base_metatile=int(base, 0) if base else None,
                        bank=int(q["bank"]) if q.get("bank") else None, new_palette=q.get("new_palette") == "1",
                        behavior=q.get("behavior") or 0)
                finally:
                    os.unlink(tmp.name)
                return self._json(r)
            body = json.loads(self._body() or b"{}")
            te = m.tileset_editor
            if path == "/api/map/metatile":
                te.set_metatile(q["name"], int(q["id"], 0), body.get("entries"), body.get("behavior"),
                                body.get("layer_type"))
                return self._json({"ok": True})
            if path == "/api/map/metatile_add":
                return self._json({"ids": te.add_metatiles(q["name"], int(body.get("count", 1)),
                                                           body.get("copy_from"))})
            if path == "/api/map/tile":
                te.set_tile(q["name"], int(q["id"], 0), body["pixels"])
                return self._json({"ok": True})
            if path == "/api/map/tile_add":
                return self._json({"ids": te.add_tiles(q["name"], int(body.get("count", 1)))})
            if path == "/api/map/blocks":
                import numpy as np
                name = m.resolve(q["name"])
                lay = m.layout_of(name)
                arr = np.array(body["blocks"], dtype=np.int32).reshape(lay["height"], lay["width"])
                m.write_blocks(name, arr)
                return self._json({"ok": True})
            if path == "/api/map/save":
                return self._json({"ok": True, "warnings": m.save_map(q["name"], body)})
            if path == "/api/map/resize":
                return self._json({"ok": True, "notes": m.resize(q["name"], int(body["width"]), int(body["height"]))})
            if path == "/api/map/script":
                m.write_script(q["name"], body["text"])
                return self._json({"ok": True})
            if path == "/api/map/new_script":
                return self._json({"label": m.new_script(q["name"], body.get("kind", "npc"), body.get("text", "Hello!"))})
            if path == "/api/map/duplicate":
                notes = m.duplicate_map(body["source"], body["new_name"], body.get("group") or None,
                                        bool(body.get("copy_layout", True)), body.get("map_id") or None)
                return self._json({"ok": True, "name": body["new_name"], "id": m.get_map(body["new_name"])["id"],
                                   "notes": notes})
            if path == "/api/map/link_warp":
                m.link_warps(body["map"], int(body["warp"]), body["dest_map"], int(body["dest_warp"]))
                return self._json({"ok": True})
            if path == "/api/map/revert":
                name = m.resolve(q["name"])
                lay = m.layout_of(name)
                files = [f"data/maps/{name}/map.json", str(m.script_path(name).relative_to(proj.root)),
                         lay["blockdata_filepath"], lay["border_filepath"], "data/layouts/layouts.json"]
                changed = proj.changed_files()
                for f in files:
                    if f in changed:
                        proj.revert_file(f)
                m.reload()
                return self._json({"ok": True})
            return self._error("not found", HTTPStatus.NOT_FOUND)

        def _play_post(self, path, q):
            import base64
            from .playtest import PlaytestError
            if proj.kind != "decomp":
                raise ProjectError("play-testing needs a source tree")
            body = json.loads(self._body() or b"{}")

            def png_url(img):
                buf = io.BytesIO()
                img.save(buf, "PNG")
                return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
            try:
                if path == "/api/play/start":
                    build = None
                    if body.get("build", True):
                        rep = proj.build_rom()
                        build = {"returncode": rep.returncode, "seconds": round(rep.seconds, 1),
                                 "output": "\n".join(rep.output.rstrip().splitlines()[-40:])}
                        if rep.returncode != 0:
                            return self._json({"ok": False, "build": build, "error": "make failed"})
                    name = proj.maps.resolve(body["map"]) if proj.maps else body["map"]
                    img = proj.play.start(name, body.get("x"), body.get("y"))
                    return self._json({"ok": True, "image": png_url(img), "where": proj.play.where, "build": build})
                if path == "/api/play/press":
                    final, _ = proj.play.press(body.get("inputs", ""))
                    return self._json({"ok": True, "image": png_url(final)})
                if path == "/api/play/stop":
                    proj.play.close()
                    return self._json({"ok": True})
            except PlaytestError as e:
                return self._json({"ok": False, "error": str(e)})
            return self._error("not found", HTTPStatus.NOT_FOUND)

        def _post(self, path, q):
            if path.startswith("/api/play/"):
                return self._play_post(path, q)
            if path.startswith("/api/map"):
                return self._map_post(path, q)
            if path == "/api/upload":
                a = proj.get(q["id"])
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(self._body())
                try:
                    notes = proj.import_image(a, tmp.name, new_palette=q.get("new_palette") == "1",
                                              resize=q.get("resize") == "1")
                finally:
                    os.unlink(tmp.name)
                return self._json({"ok": True, "notes": notes})
            if path == "/api/palette":
                colors = [tuple(int(v) for v in c[:3]) for c in json.loads(self._body())["colors"]]
                return self._json({"ok": True, "recoloured": proj.set_palette(proj.get(q["id"]).id, colors)})
            if path == "/api/revert":
                proj.revert(proj.get(q["id"]))
                return self._json({"ok": True})
            if path == "/api/check":
                rep = proj.check() if proj.kind == "decomp" else proj.build(dry_run=True)
                return self._json(rep.to_dict())
            if path == "/api/build":
                rep = proj.build(dry_run=q.get("dry") == "1")
                return self._json(rep.to_dict())
            return self._error("not found", HTTPStatus.NOT_FOUND)

    return Handler


def serve(proj, host: str = "127.0.0.1", port: int = 8765) -> None:
    state = _State(proj)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"pae web editor for {proj.title} on http://{host}:{port}/  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass

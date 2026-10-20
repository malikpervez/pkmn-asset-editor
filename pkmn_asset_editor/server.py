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
    anim_sheets: dict[str, bytes] = {}  # map -> animation preview sheet from the last /api/map/anims

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        # -------------------------------------------------------------- helpers
        def _send(self, body: bytes, ctype: str, status=HTTPStatus.OK, headers: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if "Cache-Control" not in (headers or {}):
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
            if path in ("/help", "/help.html"):
                return self._send((WEB / "help.html").read_bytes(), "text/html; charset=utf-8")
            if path in ("/story", "/story.html"):
                return self._send((WEB / "story.html").read_bytes(), "text/html; charset=utf-8")
            if path in ("/history.js", "/findtext.js"):
                return self._send((WEB / path[1:]).read_bytes(), "text/javascript; charset=utf-8")
            if path == "/api/text/search":
                if proj.kind != "decomp":
                    raise ProjectError("finding text needs a pokeemerald-expansion source tree")
                flag = lambda k, d="0": q.get(k, d) in ("1", "true")  # noqa: E731
                return self._json(proj.textsearch.search(q.get("q", ""), flag("case"), flag("word"),
                                                         flag("places", "1"), flag("trainers", "1")))
            if path == "/api/history":
                h = self._history()
                ok = h.available()
                return self._json({"available": ok, "unsaved": h.unsaved() if ok else [], "points": h.points() if ok else []})
            if path == "/api/history/show":
                return self._json(self._history().show(q["sha"]))
            if path in ("/data", "/data.html"):
                return self._send((WEB / "data.html").read_bytes(), "text/html; charset=utf-8")
            if path.startswith("/api/data/"):
                return self._data_get(path, q)
            if path.startswith(("/api/map", "/api/story")) or path in ("/api/constants", "/api/labels"):
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

        def _history(self):
            from .history import History
            if proj.kind != "decomp":
                raise ProjectError("save points need a source tree (a git repository)")
            return History(proj)

        def _gamedata(self):
            if proj.kind != "decomp":
                raise ProjectError("trainers and Pokémon data need a pokeemerald-expansion source tree")
            return proj.gamedata

        def _data_get(self, path, q):
            gd = self._gamedata()
            if path == "/api/data/trainers":
                return self._json(gd.trainers(q.get("q", "")))
            if path == "/api/data/trainer":
                return self._json(gd.get_trainer(q["id"]))
            if path == "/api/data/species_list":
                return self._json(gd.species_list(q.get("q", "")))
            if path == "/api/data/species":
                return self._json(gd.get_species(q["id"]))
            if path == "/api/data/wild":
                return self._json(gd.wild(q["map"]))
            if path == "/api/data/names":
                pre = q.get("prefix", "SPECIES_")
                known = gd.known(pre)
                names = gd.names().get(pre, {})
                return self._json({c: names.get(c) or c for c in sorted(known or names)})
            if path == "/api/data/icon":
                f = gd.icon_file(q.get("species", ""))
                if not f or f not in proj.assets:
                    return self._error("no icon", HTTPStatus.NOT_FOUND)
                im = proj.render_image(proj.assets[f]).crop((0, 0, 32, 32))
                buf = io.BytesIO()
                im.save(buf, "PNG")
                return self._send(buf.getvalue(), "image/png", headers={"Cache-Control": "max-age=300"})
            if path == "/api/data/trainer_battles":
                return self._json(gd.trainer_maps(q["id"]))
            if path == "/api/data/items":
                return self._json(gd.items(q.get("q", "")))
            if path == "/api/data/item":
                return self._json(gd.get_item(q["id"]))
            if path == "/api/data/moves":
                return self._json(gd.moves(q.get("q", "")))
            if path == "/api/data/move":
                return self._json(gd.get_move(q["id"]))
            if path == "/api/data/tms":
                return self._json(dict(gd.tms(), tutors=gd.tutors()))
            if path == "/api/data/marts":
                return self._json(gd.marts())
            if path == "/api/data/difficulty":
                return self._json(gd.difficulty())
            if path == "/api/data/teachable":
                return self._json(dict(gd.teachable(q["id"]), egg_moves=gd.egg_moves(q["id"])))
            return self._error("not found", HTTPStatus.NOT_FOUND)

        def _data_post(self, path, q):
            gd = self._gamedata()
            body = json.loads(self._body() or b"{}")
            if path == "/api/data/trainer":
                return self._json(gd.set_trainer(q["id"], body))
            if path == "/api/data/wild":
                return self._json(gd.set_wild(q["map"], body["kind"], int(body.get("encounter_rate") or 0),
                                              body.get("mons") or [], int(body.get("entry") or 0)))
            if path == "/api/data/species":
                return self._json(gd.set_species(q["id"], body))
            if path == "/api/data/item":
                return self._json(gd.set_item(q["id"], body))
            if path == "/api/data/move":
                return self._json(gd.set_move(q["id"], body))
            if path == "/api/data/tms":
                return self._json(gd.set_tms(body.get("tms"), body.get("hms")))
            if path == "/api/data/mart":
                return self._json(gd.set_mart(q["label"], body.get("items") or []))
            return self._error("not found", HTTPStatus.NOT_FOUND)

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
            if path == "/api/map/anims":
                name = m.resolve(q["name"])
                ta = m.tileset_anims
                sheet, info = ta.preview(name)
                anim_sheets[name] = m.png_bytes(sheet) if sheet is not None else b""
                anims = ta.list(name)
                for a in anims:
                    a["metatiles"] = ta.animated_metatiles(name, a["id"])
                return self._json({"anims": anims, "preview": info,
                                   "palette": [p for p in ta.parse()["palette"]
                                               if any(cb in ta.parse()["callbacks"] for cb in p["callbacks"])]})
            if path == "/api/map/anim_sheet":
                name = m.resolve(q["name"])
                if name not in anim_sheets:
                    sheet, _ = m.tileset_anims.preview(name)
                    anim_sheets[name] = m.png_bytes(sheet) if sheet is not None else b""
                if not anim_sheets[name]:
                    return self._error("no animations", HTTPStatus.NOT_FOUND)
                return self._send(anim_sheets[name], "image/png")
            if path == "/api/map/anim_frames":
                im = m.tileset_anims.frames_image(q["id"], m.resolve(q["name"]) if q.get("name") else None,
                                                  scale=int(q.get("scale", 3)), labels=q.get("labels", "1") == "1")
                return self._send(m.png_bytes(im), "image/png")
            if path == "/api/story":
                st = m.story
                chapters = st.chapters()
                return self._json({"chapters": chapters, "unplaced": st.unplaced_maps(),
                                   "map_chapter": {n: st.chapter_of(n, chapters) for n in m.maps}})
            if path == "/api/story/flow":
                return self._json(m.story.flow(q.get("temp") == "1"))
            if path == "/api/story/suggest":
                return self._json(m.story.suggest_chapters())
            if path == "/api/story/items":
                return self._json(m.story.catch_up(int(q["chapter"]), q.get("extras") == "1"))
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
            if path == "/api/map/text_preview":
                import base64
                body = json.loads(self._body() or b"{}")
                tb = proj.textbox
                out = []
                for t in tb.texts(body.get("text", "")):
                    buf = io.BytesIO()
                    tb.render(t, scale=1).save(buf, "PNG")
                    out.append(dict(t.as_dict(), image="data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()))
                return self._json(out)
            if path.startswith("/api/story"):
                body = json.loads(self._body() or b"{}")
                if path == "/api/story/map":
                    ch = body.get("chapter")
                    m.story.set_chapter(body["map"], None if ch is None or ch == "" else int(ch))
                    return self._json({"ok": True})
                if path == "/api/story/chapter_add":
                    return self._json({"ok": True, "index": m.story.add_chapter(body["name"], body.get("at"),
                                                                                body.get("flags"))})
                if path == "/api/story/apply_suggestions":
                    return self._json({"ok": True, "placed": m.story.apply_suggestions(body.get("maps"))})
                if path == "/api/story/chapters":
                    m.story.save_chapters(body["chapters"])
                    return self._json({"ok": True})
                return self._error("not found", HTTPStatus.NOT_FOUND)
            ta = m.tileset_anims
            if path in ("/api/map/anim_frame", "/api/map/anim_add_frame"):
                raw = self._body()
                tmp = None
                if raw:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                        tmp.write(raw)
                try:
                    mp = m.resolve(q["name"]) if q.get("name") else None
                    if path == "/api/map/anim_frame":
                        if tmp is None:
                            return self._error("send the frame image as the request body")
                        return self._json({"ok": True, "notes": ta.set_frame(q["id"], int(q["frame"]), tmp.name, mp)})
                    return self._json(ta.add_frame(q["id"], tmp.name if tmp else None,
                                                   int(q["copy"]) if q.get("copy") else None,
                                                   int(q["at"]) if q.get("at") else None, mp))
                finally:
                    if tmp:
                        os.unlink(tmp.name)
            body = json.loads(self._body() or b"{}")
            if path == "/api/map/anim":
                if body.get("speed"):
                    ta.set_speed(body["id"], int(body["speed"]))
                if body.get("order"):
                    ta.set_sequence(body["id"], [None if i is None else int(i) for i in body["order"]])
                return self._json(ta.get(body["id"]).as_dict())
            if path == "/api/map/anim_remove_frame":
                return self._json({"notes": ta.remove_frame(body["id"], int(body["frame"]))})
            if path == "/api/map/anim_new":
                return self._json(ta.create(m.resolve(q["name"]), body["name"], int(str(body["tile"]), 0),
                                            int(body["count"]), int(body.get("frames") or 2),
                                            int(body.get("speed") or 16)))
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
            if path == "/api/play/import_save":
                with tempfile.NamedTemporaryFile(suffix=".sav", delete=False) as tmp:
                    tmp.write(self._body())
                try:
                    return self._json({"ok": True, "checkpoint": proj.play.import_save(tmp.name, q.get("name", ""))})
                except PlaytestError as e:
                    return self._json({"ok": False, "error": str(e)})
                finally:
                    os.unlink(tmp.name)
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
                    name = body.get("map") or None
                    if name and proj.maps:
                        name = proj.maps.resolve(name)
                    state = body.get("state") or None
                    if proj.maps and (body.get("chapter") not in (None, "") or body.get("team")):
                        ch = body.get("chapter")
                        state = proj.maps.story.play_state(None if ch in (None, "") else int(ch),
                                                           bool(body.get("extras")), body.get("skip"),
                                                           body.get("team"), bool(body.get("clear_team")), state)
                    img = proj.play.start(name, body.get("x"), body.get("y"), checkpoint=body.get("checkpoint") or None,
                                          state=state, script=body.get("script") or None)
                    return self._json({"ok": True, "image": png_url(img), "where": proj.play.where, "build": build})
                if path == "/api/play/checkpoints":
                    return self._json({"ok": True, "checkpoints": proj.play.checkpoints()})
                if path == "/api/play/checkpoint":
                    return self._json({"ok": True, "checkpoint": proj.play.save_checkpoint(body["name"], body.get("note", ""))})
                if path == "/api/play/checkpoint_delete":
                    proj.play.delete_checkpoint(body["name"])
                    return self._json({"ok": True})
                if path == "/api/play/script":
                    img = proj.play.run_script(body["label"], int(body.get("frames", 45)))
                    return self._json({"ok": True, "image": png_url(img)})
                if path == "/api/play/state":
                    return self._json({"ok": True, "done": proj.play.apply_state(body.get("state", []))})
                if path == "/api/play/team":
                    return self._json({"ok": True, "team": proj.play.team()})
                if path == "/api/play/battle":
                    double = bool(body.get("double"))
                    if body.get("trainer") and "double" not in body:
                        t = proj.gamedata.get_trainer(body["trainer"])
                        f = {x["key"]: x["value"] for x in t["fields"]}
                        double = f.get("Double Battle", "").lower() == "yes" or f.get("Battle Type", "").lower() == "doubles"
                    img = proj.play.start_battle(trainer=body.get("trainer") or None, species=body.get("species") or None,
                                                 level=int(body.get("level") or 5), item=body.get("item") or None,
                                                 double=double)
                    return self._json({"ok": True, "image": png_url(img), "where": proj.play.where})
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
            if path.startswith("/api/data/"):
                return self._data_post(path, q)
            if path == "/api/text/replace":
                body = json.loads(self._body() or b"{}")
                return self._json(proj.textsearch.replace(
                    body["query"], body["replacement"], bool(body.get("match_case")), bool(body.get("whole_word")),
                    body.get("only"), bool(body.get("keep_case", True)), bool(body.get("dry_run")),
                    bool(body.get("places", True)), bool(body.get("trainers", True))))
            if path == "/api/history/save":
                body = json.loads(self._body() or b"{}")
                return self._json(self._history().save(body.get("message", "")))
            if path == "/api/history/back":
                body = json.loads(self._body() or b"{}")
                r = self._history().go_back(body["sha"])
                if getattr(proj, "maps", None):
                    proj.maps.reload()
                return self._json(r)
            if path.startswith(("/api/map", "/api/story")):
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

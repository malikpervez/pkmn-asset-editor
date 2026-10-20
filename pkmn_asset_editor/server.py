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

        def _post(self, path, q):
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

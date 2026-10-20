import base64
import io
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

from pkmn_asset_editor.mcp import Server
from pkmn_asset_editor.project import Project
from pkmn_asset_editor.server import _State, make_handler


@pytest.fixture()
def proj(tmp_path, rom_file):
    return Project.create(rom_file, tmp_path / "proj", log=lambda *a: None)


def call(srv, name, **args):
    r = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r["result"]


def test_mcp_flow(proj):
    srv = Server(proj)
    init = srv.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
    assert init["result"]["protocolVersion"] == "2025-03-26"
    tools = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    assert {"view_asset", "paint_pixels", "build_rom"} <= {t["name"] for t in tools}
    listed = json.loads(call(srv, "list_assets", category="items", limit=3)["content"][0]["text"])
    assert listed["total"] == 60 and len(listed["assets"]) == 3
    view = call(srv, "view_asset", id="pokemon/001_mon1/front")
    assert view["content"][0]["type"] == "image"
    Image.open(io.BytesIO(base64.b64decode(view["content"][0]["data"])))
    px = json.loads(call(srv, "get_pixels", id="items/001_item1/icon")["content"][0]["text"])
    assert px["w"] == 24 and len(px["rows"]) == 24
    assert not call(srv, "paint_pixels", id="items/001_item1/icon", x=0, y=0, rows=["fff", ".f."],
                    rects=[{"x": 10, "y": 10, "w": 4, "h": 4, "index": 2}])["isError"]
    px2 = json.loads(call(srv, "get_pixels", id="items/001_item1/icon", x=0, y=0, w=3, h=2)["content"][0]["text"])
    assert px2["rows"][0] == "fff" and px2["rows"][1][1] == "f"
    pal = ["#%02x%02x%02x" % (i * 16, 0, 0) for i in range(16)]
    assert not call(srv, "set_palette", id="items/001_item1/palette", colors=pal)["isError"]
    assert call(srv, "set_palette", id="items/001_item1/palette", colors=pal[:3])["isError"]
    rep = json.loads(call(srv, "build_rom")["content"][0]["text"])
    assert rep["ok"] and set(rep["changed_assets"]) == {"items/001_item1/icon", "items/001_item1/palette"}
    assert call(srv, "describe_asset", id="nope")["isError"]


def test_web_api(proj):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_State(proj)))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        get = lambda p: urllib.request.urlopen(base + p).read()
        assert b"Pok" in get("/")
        assets = json.loads(get("/api/assets?category=pokemon"))
        assert assets and all(a["category"] == "pokemon" for a in assets)
        Image.open(io.BytesIO(get("/api/image?id=pokemon/001_mon1/front&palette=pokemon/001_mon1/shiny")))
        buf = io.BytesIO()
        Image.new("RGBA", (64, 64), (255, 0, 0, 255)).save(buf, "PNG")
        req = urllib.request.Request(base + "/api/upload?id=pokemon/002_mon2/front", data=buf.getvalue(), method="POST")
        assert json.loads(urllib.request.urlopen(req).read())["ok"]
        rep = json.loads(urllib.request.urlopen(urllib.request.Request(base + "/api/build", data=b"", method="POST")).read())
        assert rep["ok"] and rep["changed_assets"] == ["pokemon/002_mon2/front"]
        assert len(get("/api/download?what=rom")) == len(proj.rom.data)
    finally:
        httpd.shutdown()

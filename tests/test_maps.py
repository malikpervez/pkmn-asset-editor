import io
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.maps import _upper_snake
from pkmn_asset_editor.mcp import Server
from pkmn_asset_editor.server import _State, make_handler


@pytest.fixture()
def repo(tmp_path):
    return decomp_fixture.build(tmp_path / "game")


@pytest.fixture()
def proj(repo):
    return DecompProject(repo)


def test_index_and_render(proj):
    m = proj.maps
    assert set(m.maps) == {"Town", "Town_House1"}
    assert m.resolve("MAP_TOWN_HOUSE1") == "Town_House1"
    atlas = m.layout_atlas(m.layout_of("Town"))
    assert atlas.num_primary == 512 and atlas.num_secondary == 32
    assert atlas.image[16 * (512 // 8):, :, 3].any()  # secondary metatiles are drawn
    im = m.render("Town")
    assert im.size == (160, 128)
    assert m.object_sprite("OBJ_EVENT_GFX_HERO").size == (16, 32)
    base = m.render("Town", events=False)
    assert np.array(im).tolist() != np.array(base).tolist()  # events were drawn on top


def test_blocks_text_roundtrip(proj):
    m = proj.maps
    t = m.blocks_text("Town", 0, 0, 3, 2)
    assert t["rows"][0] == "00213 00213 00213"  # metatile 2, collision 1, elevation 3
    assert m.apply_blocks("Town", 1, 1, ["005", ". 00703"]) == 2
    b = m.read_blocks(m.layout_of("Town"))
    assert b[1, 1] & 0x3FF == 5 and (b[1, 1] >> 12) == 3  # metatile-only write keeps elevation
    assert b[2, 2] == 7 | (3 << 12)
    with pytest.raises(DecompError):
        m.apply_blocks("Town", 0, 0, ["zz"])


def test_events_validation_and_scripts(proj):
    m = proj.maps
    i = m.add_event("Town", "object_events", 6, 5, graphics_id="OBJ_EVENT_GFX_HERO")
    lbl = m.new_script("Town", "npc", 'Say "hi"')
    assert lbl == "Town_EventScript_Npc" and lbl in m.read_script("Town")
    assert m.new_script("Town", "npc") == "Town_EventScript_Npc2"
    data = m.get_map("Town")
    data["object_events"][i]["script"] = lbl
    assert m.save_map("Town", data) == []
    data["warp_events"][0]["dest_map"] = "MAP_NOWHERE"
    with pytest.raises(DecompError):
        m.save_map("Town", data)
    data["warp_events"][0]["dest_map"] = "MAP_TOWN_HOUSE1"
    data["object_events"][0]["x"] = 99
    warnings = m.save_map("Town", data)
    assert any("outside" in w for w in warnings)
    assert json.loads((proj.root / "data/maps/Town/map.json").read_text())["object_events"][i]["script"] == lbl
    rep = proj.check()
    assert rep.ok and any("outside" in w for w in rep.warnings)


def test_new_house_flow(proj):
    m = proj.maps
    notes = m.duplicate_map("Town_House1", "Town_House2")
    assert any("warp" in n for n in notes)
    d = m.get_map("Town_House2")
    assert d["id"] == "MAP_TOWN_HOUSE2" and d["layout"] == "LAYOUT_TOWN_HOUSE2"
    assert d["object_events"][0]["script"] == "Town_House2_EventScript_Mom"
    assert d["object_events"][0]["local_id"] == "LOCALID_TOWN_HOUSE2_HOUSE_MOM"
    script = m.read_script("Town_House2")
    assert "Town_House2_EventScript_Mom::" in script and "LOCALID_TOWN_HOUSE2_HOUSE_MOM" in script
    assert "Common_Movement_FacePlayer" in script  # shared labels untouched
    groups = json.loads((proj.root / "data/maps/map_groups.json").read_text())
    assert "Town_House2" in groups["gMapGroup_Indoor"]
    assert (proj.root / "data/layouts/Town_House2/map.bin").exists()
    # door in town + two-way link
    w = m.add_event("Town", "warp_events", 6, 2)
    m.link_warps("Town", w, "Town_House2", 0)
    assert m.get_map("Town")["warp_events"][w]["dest_map"] == "MAP_TOWN_HOUSE2"
    assert m.get_map("Town_House2")["warp_events"][0] == {"x": 2, "y": 4, "elevation": 0, "dest_map": "MAP_TOWN",
                                                           "dest_warp_id": str(w)}
    # paint the outside of the house by copying blocks
    assert m.copy_blocks("Town", 0, 0, 3, 1, "Town", 5, 6) == []
    rep = proj.check()
    assert rep.ok, rep.errors
    with pytest.raises(DecompError):
        m.duplicate_map("Town_House1", "Town_House2")
    # revert everything, including the new map and layout folders
    assert main(["revert", "-p", str(proj.root), "--all"]) == 0
    assert not (proj.root / "data/maps/Town_House2").exists()
    assert DecompProject(proj.root).status() == []


def test_resize(proj):
    m = proj.maps
    m.resize("Town_House1", 8, 3)
    lay = m.layout_of("Town_House1")
    assert (lay["width"], lay["height"]) == (8, 3)
    assert m.read_blocks(lay).shape == (3, 8)
    assert (proj.root / lay["blockdata_filepath"]).stat().st_size == 8 * 3 * 2
    assert any("outside" in i.message for i in proj.check().issues)  # the warp at y=4 is now outside


def test_upper_snake():
    assert _upper_snake("PetalburgCity_House1") == "PETALBURG_CITY_HOUSE1"
    assert _upper_snake("LittlerootTown_BrendansHouse_1F") == "LITTLEROOT_TOWN_BRENDANS_HOUSE_1F"
    assert _upper_snake("SSTidalCorridor") == "SS_TIDAL_CORRIDOR"
    assert _upper_snake("PalletTown_Frlg") == "PALLET_TOWN"


def test_mcp_map_tools(proj):
    srv = Server(proj)
    call = lambda tool, **a: srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": tool, "arguments": a}})["result"]
    assert call("view_map", name="Town")["content"][0]["type"] == "image"
    assert call("view_metatiles", name="Town")["content"][0]["type"] == "image"
    r = call("edit_map", name="Town", header={"weather": "WEATHER_RAIN"},
             events=[{"op": "add", "kind": "bg_events", "fields": {"x": 1, "y": 1, "script": "Town_EventScript_Hero"}}])
    assert not r["isError"], r
    d = proj.maps.get_map("Town")
    assert d["weather"] == "WEATHER_RAIN" and d["bg_events"][0]["type"] == "sign"
    assert call("edit_map", name="Town", events=[{"op": "delete", "kind": "bg_events", "index": 5}])["isError"]
    assert not call("set_blocks", name="Town", x=0, y=7, rows=["003 003"])["isError"]
    blocks = json.loads(call("get_blocks", name="Town", x=0, y=7, w=2, h=1)["content"][0]["text"])
    assert blocks["rows"] == ["00303 00303"]
    flags = json.loads(call("find_constants", prefix="FLAG_", query="HERO")["content"][0]["text"])
    assert flags["constants"] == ["FLAG_HIDE_HERO"]
    r = json.loads(call("duplicate_map", source="Town_House1", new_name="Town_Shop")["content"][0]["text"])
    assert r["id"] == "MAP_TOWN_SHOP"
    assert json.loads(call("validate")["content"][0]["text"])["ok"]


def test_web_map_api(proj):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_State(proj)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    get = lambda p: urllib.request.urlopen(base + p).read()
    post = lambda p, body: json.loads(urllib.request.urlopen(urllib.request.Request(
        base + p, data=json.dumps(body).encode(), method="POST")).read())
    try:
        assert b"Map Editor" in get("/maps")
        maps = json.loads(get("/api/maps"))
        assert {m["name"] for m in maps["maps"]} == {"Town", "Town_House1"}
        m = json.loads(get("/api/map?name=Town"))
        assert len(m["blocks"]) == 80 and m["metatiles"]["count"] == 544
        Image.open(io.BytesIO(get("/api/map/atlas?name=Town")))
        Image.open(io.BytesIO(get("/api/map/sprite?gfx=OBJ_EVENT_GFX_HERO")))
        blocks = m["blocks"]
        blocks[0] = 9 | (3 << 12)
        assert post("/api/map/blocks?name=Town", {"blocks": blocks})["ok"]
        m["data"]["object_events"][0]["x"] = 5
        assert post("/api/map/save?name=Town", m["data"])["ok"]
        assert post("/api/map/new_script?name=Town", {"kind": "sign", "text": "A sign"})["label"] == "Town_EventScript_Sign"
        assert "MAP_TOWN" in json.loads(get("/api/constants?prefix=MAP_"))
        assert any(x["label"] == "Town_EventScript_Hero" for x in json.loads(get("/api/labels?q=hero")))
        assert post("/api/check", {})["ok"]
        assert post("/api/map/revert?name=Town", {})["ok"]
        assert proj.changed_files() == {}
    finally:
        httpd.shutdown()

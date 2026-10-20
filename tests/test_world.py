import json
import subprocess

import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject

REGION_C = """\
static const u8 sMapHealLocations[][3] =
{
    [MAPSEC_TOWN] = {MAP_GROUP(MAP_TOWN), MAP_NUM(MAP_TOWN), HEAL_LOCATION_TOWN},
};

static u8 GetMapsecType(mapsec_u16_t mapSecId)
{
    switch (mapSecId)
    {
    case MAPSEC_NONE:
        return MAPSECTYPE_NONE;
    case MAPSEC_TOWN:
        return FlagGet(FLAG_VISITED_TOWN) ? MAPSECTYPE_CITY_CANFLY : MAPSECTYPE_CITY_CANTFLY;
    default:
        return MAPSECTYPE_ROUTE;
    }
}
"""


@pytest.fixture()
def root(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    (root / "data/event_scripts.s").write_text(
        '\t.include "data/maps/Town/scripts.inc"\n\t.include "data/maps/Town_House1/scripts.inc"\n')
    rm = root / "src/data/region_map"
    rm.mkdir(parents=True, exist_ok=True)
    (rm / "region_map_layout.h").write_text(
        "static const mapsec_u8_t sRegionMap_MapSectionLayout[MAP_HEIGHT][MAP_WIDTH] = {\n"
        "    {MAPSEC_NONE, MAPSEC_TOWN, MAPSEC_NONE},\n    {MAPSEC_NONE, MAPSEC_NONE, MAPSEC_NONE},\n};\n")
    (rm / "region_map_sections.json").write_text(json.dumps({"map_sections": [
        {"id": "MAPSEC_TOWN", "name": "TOWN", "x": 1, "y": 0, "width": 1, "height": 1},
        {"id": "MAPSEC_ROUTE_1", "name": "ROUTE 1", "x": 0, "y": 0, "width": 1, "height": 1}]}, indent=2) + "\n")
    (root / "src/region_map.c").write_text(REGION_C)
    (root / "src/data/heal_locations.json").write_text(json.dumps({"heal_locations": [
        {"id": "HEAL_LOCATION_TOWN", "map": "MAP_TOWN", "x": 3, "y": 4, "respawn_map": "MAP_TOWN",
         "respawn_npc": "LOCALID_NONE", "respawn_x": 3, "respawn_y": 4}]}, indent=2) + "\n")
    f = root / "include/constants/flags.h"
    f.write_text(f.read_text() + "#define FLAG_VISITED_TOWN 0x22\n#define FLAG_VISITED_ROUTE 0x23\n")
    t = json.loads((root / "data/maps/Town/map.json").read_text())
    t["region_map_section"] = "MAPSEC_TOWN"
    (root / "data/maps/Town/map.json").write_text(json.dumps(t, indent=2) + "\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "world"], cwd=root, check=True)
    return root


def test_attach_and_layout(root):
    w = DecompProject(root).maps.world
    assert w.layout()["areas"] == []  # nothing is connected yet
    r = w.attach_map("Town", "right", "Town", "Route1", offset=2)
    assert r["id"] == "MAP_ROUTE1"
    w2 = DecompProject(root).maps.world
    L = w2.layout()
    area = L["areas"][0]
    pos = {m["name"]: (m["x"], m["y"]) for m in area["maps"]}
    assert pos == {"Town": (0, 0), "Route1": (10, 2)} and (area["w"], area["h"]) == (20, 10)
    assert L["problems"] == []
    route = json.loads((root / "data/maps/Route1/map.json").read_text())
    assert route["connections"] == [{"map": "MAP_TOWN", "offset": -2, "direction": "left"}]
    # the copied map's scripts are assembled too (data/event_scripts.s)
    assert '.include "data/maps/Town/scripts.inc"\n\t.include "data/maps/Route1/scripts.inc"' in \
        (root / "data/event_scripts.s").read_text()
    with pytest.raises(DecompError, match="already has"):
        w2.attach_map("Town", "right", "Town", "Route2", offset=0)
    # a connection that doesn't come back is reported
    route["connections"] = []
    (root / "data/maps/Route1/map.json").write_text(json.dumps(route))
    L = DecompProject(root).maps.world.layout()
    assert any("doesn't connect left back" in p["message"] for p in L["problems"])
    assert w2.render_area(area, 0.25).size == (80, 40)


def test_region_map_and_fly_spots(root):
    w = DecompProject(root).maps.world
    rm = w.region_map()
    assert rm["grid"] == [["MAPSEC_NONE", "MAPSEC_TOWN", "MAPSEC_NONE"], ["MAPSEC_NONE"] * 3]
    town = next(s for s in rm["sections"] if s["id"] == "MAPSEC_TOWN")
    assert town["maps"] == ["Town"] and town["fly"]["flag"] == "FLAG_VISITED_TOWN" and town["fly"]["x"] == 3
    route = next(s for s in rm["sections"] if s["id"] == "MAPSEC_ROUTE_1")
    assert not route["on_map"] and route["fly"] is None
    rm = w.set_region_grid([["MAPSEC_ROUTE_1", "MAPSEC_TOWN", "MAPSEC_ROUTE_1"], ["MAPSEC_NONE", "MAPSEC_NONE", "MAPSEC_ROUTE_1"]])
    secs = {s["id"]: s for s in json.loads((root / "src/data/region_map/region_map_sections.json").read_text())["map_sections"]}
    assert (secs["MAPSEC_ROUTE_1"]["x"], secs["MAPSEC_ROUTE_1"]["width"], secs["MAPSEC_ROUTE_1"]["height"]) == (0, 3, 2)
    assert "{MAPSEC_ROUTE_1, MAPSEC_TOWN, MAPSEC_ROUTE_1}," in (root / "src/data/region_map/region_map_layout.h").read_text()
    with pytest.raises(DecompError, match="unknown map section"):
        w.set_region_grid([["MAPSEC_NOPE"] * 3, ["MAPSEC_NONE"] * 3])
    # a new place and a fly spot for it, opened by a flag the map turns on
    sec = w.add_section("Sunny Town")
    assert sec == {"id": "MAPSEC_SUNNY_TOWN", "name": "SUNNY TOWN"}
    w.set_fly_spot("MAPSEC_SUNNY_TOWN", "Town_House1", 2, 3, flag="FLAG_VISITED_ROUTE")
    c = (root / "src/region_map.c").read_text()
    assert "[MAPSEC_SUNNY_TOWN] = {MAP_GROUP(MAP_TOWN_HOUSE1), MAP_NUM(MAP_TOWN_HOUSE1), HEAL_LOCATION_TOWN_HOUSE1}," in c
    assert "case MAPSEC_SUNNY_TOWN:\n        return FlagGet(FLAG_VISITED_ROUTE) ? MAPSECTYPE_CITY_CANFLY" in c
    assert c.index("case MAPSEC_SUNNY_TOWN") < c.index("default:")
    heal = json.loads((root / "src/data/heal_locations.json").read_text())["heal_locations"]
    assert heal[-1]["id"] == "HEAL_LOCATION_TOWN_HOUSE1" and (heal[-1]["x"], heal[-1]["y"]) == (2, 3)
    script = (root / "data/maps/Town_House1/scripts.inc").read_text()
    assert "map_script MAP_SCRIPT_ON_TRANSITION, Town_House1_OnTransition" in script
    assert "Town_House1_OnTransition:\n\tsetflag FLAG_VISITED_ROUTE\n\tend" in script
    w.set_fly_spot("MAPSEC_SUNNY_TOWN", "Town_House1", 2, 3)  # again, without a flag: always
    c = (root / "src/region_map.c").read_text()
    assert c.count("[MAPSEC_SUNNY_TOWN]") == 1 and "case MAPSEC_SUNNY_TOWN:\n        return MAPSECTYPE_CITY_CANFLY;" in c
    assert w.rename_section("MAPSEC_SUNNY_TOWN", "Rainy Town")["name"] == "RAINY TOWN"


def test_region_map_picture_and_cli(root, capsys):
    d = root / "graphics/pokenav/region_map"
    d.mkdir(parents=True, exist_ok=True)
    tiles = np.zeros((8, 16), np.uint8)
    tiles[:, 8:] = 1  # tile 1 is colour 1
    im = Image.fromarray(tiles, "P")
    im.putpalette([0, 0, 0, 200, 40, 40] + [0] * 762)
    im.save(d / "map.png")
    m = np.zeros((64, 64), np.uint8)
    m[0, 1] = 1
    (d / "map.bin").write_bytes(m.tobytes())
    img = DecompProject(root).maps.world.region_map_image()
    assert img.size == (240, 160) and img.getpixel((10, 3)) == (200, 40, 40) and img.getpixel((3, 3)) == (0, 0, 0)
    assert main(["world", "-p", str(root)]) == 0
    assert "TOWN" in capsys.readouterr().out

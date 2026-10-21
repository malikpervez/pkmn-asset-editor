"""Making a map from scratch: geometry, new tilesets built from artwork, painting, connection seams, doors."""
import json

import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor import doors, seams
from pkmn_asset_editor import tilesetbuild as tb
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.gfx import write_jasc

PAL = [(0, 0, 0), (24, 40, 32), (112, 176, 80), (200, 168, 104), (248, 248, 232), (200, 64, 56)] + [(0, 0, 0)] * 10


@pytest.fixture()
def root(tmp_path):
    return decomp_fixture.build(tmp_path / "game")


def _art(path, cells):
    """cells: list of (16x16 index arrays) side by side; index 0 transparent."""
    w = 16 * len(cells)
    img = np.zeros((16, w, 4), np.uint8)
    for i, c in enumerate(cells):
        for idx, col in enumerate(PAL):
            if idx:
                img[:, i * 16:(i + 1) * 16][c == idx] = (*col, 255)
    Image.fromarray(img, "RGBA").save(path)


def test_geometry_of_a_map_with_its_own(root):
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["firered_geometry"] = {"width": 10, "height": 8, "primary_tileset": "gTileset_General",
                                "secondary_tileset": "gTileset_Town", "blockdata_filepath": "data/layouts/Town/map.bin",
                                "border_filepath": "data/layouts/Town/border.bin", "layout_version": "hns"}
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    m = DecompProject(root).maps
    lay = m.layout_of("Town")
    assert lay["layout_version"] == "hns" and lay["_geometry_map"] == "Town"
    m.resize("Town", 12, 8)
    saved = json.loads((root / "data/maps/Town/map.json").read_text())["firered_geometry"]
    assert (saved["width"], saved["height"]) == (12, 8)
    assert json.loads((root / "data/layouts/layouts.json").read_text())["layouts"][0]["width"] == 10  # layout unchanged
    assert m.read_blocks(m.layout_of("Town")).shape == (8, 12)
    with pytest.raises(DecompError, match="unknown tileset"):
        m.set_geometry("Town", secondary_tileset="gTileset_Nope")


def _scratch(root, tmp_path):
    proj = DecompProject(root)
    tb.new_tileset(proj, "gTileset_NewGeneral", secondary=False)
    tb.new_tileset(proj, "gTileset_NewTown", secondary=True)
    with pytest.raises(DecompError, match="already exists"):
        tb.new_tileset(proj, "gTileset_NewTown", secondary=True)
    assert "extern" not in (root / "src/data/tilesets/headers.h").read_text()
    proj = DecompProject(root)
    m = proj.maps
    assert m.tilesets["gTileset_NewTown"].secondary and m.tilesets["gTileset_NewGeneral"].tiles
    art = tmp_path / "art"
    art.mkdir()
    grass = np.full((16, 16), 2, np.uint8)
    path_full = np.full((16, 16), 3, np.uint8)
    path_edge = path_full.copy()
    path_edge[:3] = 2  # grass along the top: no path to the north
    _art(art / "ground.png", [grass, path_full, path_edge])
    house = np.zeros((32, 32), np.uint8)
    house[4:, 2:30] = 4
    house[0:6, :] = 5
    house[20:32, 4:12] = 1
    img = np.zeros((32, 32, 4), np.uint8)
    for idx, col in enumerate(PAL):
        if idx:
            img[house == idx] = (*col, 255)
    Image.fromarray(img, "RGBA").save(art / "house.png")
    write_jasc(art / "p.pal", PAL)
    spec = {"version": "emerald", "primary": "gTileset_NewGeneral", "secondary": "gTileset_NewTown",
            "palettes": {"primary": ["p.pal"], "secondary": ["p.pal"]}, "reserve": {"primary": [[3, 4]]},
            "metatiles": [
                {"name": "grass", "tileset": "primary", "image": "ground.png", "x": 0},
                {"name": "path#15", "tileset": "primary", "image": "ground.png", "x": 16},
                {"name": "path#14", "tileset": "primary", "image": "ground.png", "x": 32},
                {"name": "house", "image": "house.png", "w": 2, "h": 2, "ground": "grass", "solid": True,
                 "open": ["0,1"], "behaviors": {"0,1": 1}}]}
    (art / "spec.json").write_text(json.dumps(spec))
    out = tb.build(m, art / "spec.json")
    return proj, m, art, out


def test_build_and_paint(root, tmp_path):
    proj, m, art, out = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    assert names["metatiles"]["house"]["ids"] == [512, 513, 514, 515] and names["metatiles"]["grass"]["ids"] == [0]
    te = m.tileset_editor
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    # grass is one tile used 4 times; the reserved slots 3-4 stay free
    g = te.get_metatile("Town", 0)
    assert len({e["tile"] for e in g["entries"][:4]}) == 1
    used = {e["tile"] for mid in range(3) for e in te.get_metatile("Town", mid)["entries"]}
    assert not used & {3, 4}
    h = te.get_metatile("Town", 512)
    assert h["entries"][:4] == g["entries"][:4] and h["layer_type"] == 1  # grass under the house art
    assert te.get_metatile("Town", 514)["behavior"] == 1
    grid = "....\n====\n====\n..HH\n..HH\n" + "....\n" * 3
    legend = {".": "grass", "=": {"auto": "path"}, "H": "house"}
    b = tb.paint(m, "Town", grid, legend, names)
    assert b.shape == (8, 4)
    assert b[1, 1] & 0x3FF == 2  # grass to the north: path#14
    assert b[2, 1] & 0x3FF == 1  # path#11 wasn't drawn: the plain path#15
    assert (b[3, 2] & 0x3FF, b[3, 2] >> 10 & 3) == (512, 1) and (b[4, 2] & 0x3FF, b[4, 2] >> 10 & 3) == (514, 0)
    with pytest.raises(DecompError, match="needs a whole 2x2"):
        tb.paint(m, "Town", "H.\n..\n", legend, names)
    assert main(["map", "paint", "Town", str(_w(art / "g.txt", "....\n" * 8)), str(_w(art / "l.json",
                 json.dumps(legend))), "--names", str(art / "names.json"), "-p", str(root)]) == 0


def _w(p, text):
    p.write_text(text)
    return p


def test_seams_and_doors(root, tmp_path):
    proj, m, art, out = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown",
                   layout_version="hns")
    tb.paint(m, "Town", "..HH\n..HH\n" + "....\n" * 6, {".": "grass", "H": "house"}, names)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["connections"] = [{"map": "MAP_TOWN_HOUSE1", "direction": "up", "offset": 0}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    house = json.loads((root / "data/maps/Town_House1/map.json").read_text())
    house["connections"] = [{"map": "MAP_TOWN", "direction": "down", "offset": 0}]
    (root / "data/maps/Town_House1/map.json").write_text(json.dumps(house))
    (root / "src/kanto_connection_seam.c").write_text(
        '#include "global.h"\n#include "kanto_connection_seam.h"\n\n'
        'static const struct KantoSeam sKantoSeams[] = {\n};\n')
    m = DecompProject(root).maps
    r = {x["target"]: x for x in seams.build(m, "Town")}
    assert set(r) == {"Town", "Town_House1"} and r["Town"]["sources"] == ["Town_House1"]
    header = (root / "src/data/pae_seams.h").read_text()
    assert "PAE_SEAMS" in header and "&Town_Layout" in header and ".sourceLayout = &House1_Layout" in header
    c = (root / "src/kanto_connection_seam.c").read_text()
    assert c.count('#include "data/pae_seams.h"') == 1 and "    PAE_SEAMS\n" in c
    seams.build(m, "Town")  # again: still once
    assert (root / "src/kanto_connection_seam.c").read_text().count("PAE_SEAMS") == 1
    (root / "src/field_door.c").write_text(
        "static const struct DoorGraphics sDoorAnimGraphicsTable[] =\n{\n    {},\n};\n")
    d = doors.add(m, "Town", 2, 1, (4, 4, 12, 16))
    assert d["metatile"] == 514 and d["tileset"] == "gTileset_NewTown"
    rec = json.loads((root / "src/data/pae_doors.json").read_text())
    assert len(rec) == 1 and len(rec[0]["frames"]) == 3
    last = np.array(rec[0]["frames"][2][2])  # bottom-left tile of the open door: the panel is doorway now
    assert (last[:, 4:] == last[0, 4]).all()
    assert "PAE_DOORS" in (root / "src/field_door.c").read_text()
    assert "{ 514, &gTileset_NewTown, DOOR_SOUND_NORMAL, 1," in (root / "src/data/pae_doors.h").read_text()

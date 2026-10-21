"""Making a map from scratch: geometry, new tilesets built from artwork, painting, connection seams, doors."""
import json

import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor import doors, seams, walk
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


def test_pattern_paint(root, tmp_path):
    proj, m, art, out = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    b = tb.paint(m, "Town", "HHH.\nHHH.\nHHH.\n" + "....\n" * 5, {"H": {"tile": "house"}, ".": "grass"}, names)
    ids = names["metatiles"]["house"]["ids"]
    for y in range(3):
        for x in range(3):  # any rectangle: each block is the piece at (x mod 2, y mod 2)
            assert b[y, x] & 0x3FF == ids[(y % 2) * 2 + x % 2]
    assert b[1, 0] & 0x3FF == ids[2] and (b[1, 0] >> 10) & 3 == 0  # the door piece stays open

    # variants of an edge piece ("grass~1"): spread over the area by position, the same each time
    names["metatiles"]["grass~1"] = dict(names["metatiles"]["grass"], ids=names["metatiles"]["path#15"]["ids"])
    b2 = tb.paint(m, "Town", "....\n" * 8, {".": {"auto": "grass"}}, names)
    found = {int(v) & 0x3FF for v in b2.ravel()}
    assert found == {names["metatiles"]["grass"]["ids"][0], names["metatiles"]["path#15"]["ids"][0]}
    assert (tb.paint(m, "Town", "....\n" * 8, {".": {"auto": "grass"}}, names) == b2).all()


ANIMS_C = """\
#include "global.h"

static void QueueAnimTiles_General_Flower(u16);

const u16 gTilesetAnims_General_Flower_Frame0[] = INCBIN_U16("data/tilesets/primary/general/anim/flower/0.4bpp");

const u16 *const gTilesetAnims_General_Flower[] = {
    gTilesetAnims_General_Flower_Frame0
};

static void QueueAnimTiles_General_Flower(u16 timer)
{
    u16 i = timer % ARRAY_COUNT(gTilesetAnims_General_Flower);
    AppendTilesetAnimToBuffer(gTilesetAnims_General_Flower[i],
                              (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(8)), 4 * TILE_SIZE_4BPP);
}
"""


def _water(path, shift, cells=2):
    """Water blocks side by side, a highlight moving right with ``shift``."""
    img = np.zeros((16, 16 * cells, 4), np.uint8)
    img[..., :3] = PAL[2]
    img[..., 3] = 255
    for c in range(cells):
        img[4, c * 16 + (shift + 3 * c) % 16] = (*PAL[4], 255)
    Image.fromarray(img, "RGBA").save(path)


def test_animated_tiles(root, tmp_path):
    (root / "src/tileset_anims.c").write_text(ANIMS_C)
    (root / "include/tileset_anims.h").write_text("#ifndef GUARD_TILESET_ANIMS_H\n#define GUARD_TILESET_ANIMS_H\n\n"
                                                  "#endif // GUARD_TILESET_ANIMS_H\n")
    proj, m, art, out = _scratch(root, tmp_path)
    for k in range(3):
        _water(art / f"water{k}.png", 4 * k)
    spec = json.loads((art / "spec.json").read_text())
    spec["animations"] = [{"name": "Pond", "image": "water0.png", "frames": ["water1.png", "water2.png"], "speed": 8}]
    spec["metatiles"] += [{"name": "water", "tileset": "primary", "image": "water0.png", "x": 0},
                          {"name": "water2", "tileset": "primary", "image": "water0.png", "x": 16}]
    (art / "spec.json").write_text(json.dumps(spec))
    out = tb.build(DecompProject(root).maps, art / "spec.json")
    a = out["animations"][0]
    assert a["name"] == "NewGeneral_Pond" and a["frames"] == 3
    m = DecompProject(root).maps
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    te = m.tileset_editor
    first, n = a["first_tile"], a["tiles"]
    assert first >= out["tiles"]["primary"] - n  # after the other tiles, in one run
    used = {e["tile"] for mid in out["metatiles"]["water"]["ids"] + out["metatiles"]["water2"]["ids"]
            for e in te.get_metatile("Town", mid)["entries"][:4]}
    assert used <= set(range(first, first + n))
    text = (root / "src/tileset_anims.c").read_text()
    # the style the file already uses: INCBIN of the .4bpp built from the PNG
    assert 'INCBIN_U16("data/tilesets/primary/new_general/anim/pond/2.4bpp")' in text
    assert f"TILE_OFFSET_4BPP({first})), {n} * TILE_SIZE_4BPP" in text
    assert (root / "data/tilesets/primary/new_general/anim/pond/2.png").exists()
    # more art before it: the build moves the run and the code follows
    spec["metatiles"].insert(1, {"name": "grass_b", "tileset": "primary", "image": "ground.png", "x": 32})
    spec["metatiles"].insert(1, {"name": "path_b", "tileset": "primary", "image": "house.png", "x": 0, "y": 0})
    (art / "spec.json").write_text(json.dumps(spec))
    out2 = tb.build(DecompProject(root).maps, art / "spec.json")
    first2 = out2["animations"][0]["first_tile"]
    assert first2 != first
    assert f"TILE_OFFSET_4BPP({first2})), {n} * TILE_SIZE_4BPP" in (root / "src/tileset_anims.c").read_text()
    # another frame and another speed: the code follows
    _water(art / "water3.png", 12)
    spec["animations"][0].update(frames=["water1.png", "water2.png", "water3.png"], speed=4)
    (art / "spec.json").write_text(json.dumps(spec))
    out3 = tb.build(DecompProject(root).maps, art / "spec.json")
    assert out3["animations"][0]["frames"] == 4
    text = (root / "src/tileset_anims.c").read_text()
    assert "gTilesetAnims_NewGeneral_Pond_Frame3" in text.split("gTilesetAnims_NewGeneral_Pond[] = {")[1]
    assert "timer % 4 ==" in text
    assert (root / "data/tilesets/primary/new_general/anim/pond/3.png").exists()


def test_seam_borrows_what_the_map_never_shows(root, tmp_path, monkeypatch):
    proj, m, art, out = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown",
                   layout_version="hns")
    tb.paint(m, "Town", "....\n" * 8, {".": "grass"}, names)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["connections"] = [{"map": "MAP_TOWN_HOUSE1", "direction": "up", "offset": 0}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    (root / "src/kanto_connection_seam.c").write_text(
        '#include "global.h"\n#include "kanto_connection_seam.h"\n\n'
        'static const struct KantoSeam sKantoSeams[] = {\n};\n')
    m = DecompProject(root).maps
    info = {"npt": 512}
    spare_tiles, spare_banks = seams._spare(m, "Town", info)
    assert 0 not in spare_banks and 1 in spare_banks  # Town shows only grass (palette 0)
    assert all(t >= 512 for t in spare_tiles) and not set(spare_tiles) & set(range(1008, 1024))
    # with every palette slot used by the tilesets, the seam still fits by borrowing
    real = seams._used

    def all_used(maps, name):
        tiles, banks, inf = real(maps, name)
        return tiles, set(range(13)), inf
    monkeypatch.setattr(seams, "_used", all_used)
    p = seams.plan(m, "Town", ["Town_House1"])
    assert p["palettes"] and p["borrowed"]["palettes"] == sorted(p["palettes"])
    assert 0 not in p["palettes"]


def test_place_path_and_connect(root):
    m = DecompProject(root).maps
    lay = m.layout_of("Town")
    b = m.read_blocks(lay).copy()
    b[:, :] = b[0, 0] & ~0x0C00  # walkable everywhere ...
    b[2, 1:4] |= 1 << 10  # ... but a wall in the way
    m.write_blocks("Town", b)
    walk.place(m, "Town", "object", "0", 1, 1)
    assert (m.get_map("Town")["object_events"][0]["x"], m.get_map("Town")["object_events"][0]["y"]) == (1, 1)
    with pytest.raises(DecompError, match="no object event"):
        walk.place(m, "Town", "object", "LOCALID_NOPE", 1, 1)
    dirs, end = walk.walk(m, "Town", [(1, 1), (2, 4)])
    assert end == (2, 4) and dirs == ["left", "down", "down", "down", "right", "right"]  # around the wall
    follow, fend = walk.walk(m, "Town", [(1, 0), (1, 1), (2, 4)], stop_short=1)
    assert follow[1:] == dirs[:-1]  # one step behind the leader
    p = root / "data/maps/Town/scripts.inc"
    p.write_text(p.read_text() + "\nTown_Movement_Walk::\n\twalk_up\n\tstep_end\n\nTown_Movement_Other::\n\tstep_end\n")
    assert main(["map", "path", "Town", "1,1", "2,4", "--then", "face_up", "--write", "Town_Movement_Walk",
                 "-p", str(root)]) == 0
    text = p.read_text()
    body = text.split("Town_Movement_Walk::\n")[1].split("step_end")[0]
    assert body.count("walk_") == 6 and "\tface_up\n" in body and "Town_Movement_Other::\n\tstep_end" in text
    a, back = m.connect("Town", "up", "Town_House1", -3)
    assert (a["direction"], a["offset"], back["direction"], back["offset"]) == ("up", -3, "down", 3)
    assert main(["map", "connect", "Town", "up", "Town_House1", "--align", "4,2", "-p", str(root)]) == 0
    conns = DecompProject(root).maps.get_map("Town")["connections"]
    assert [c["offset"] for c in conns if c["map"] == "MAP_TOWN_HOUSE1"] == [2]

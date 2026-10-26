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


def test_new_blocks_drop_the_old_blocks_fixes(root):
    """The old block file's fixes go with it: a map's own geometry clears the layout's (null), a layout drops its own."""
    lays = json.loads((root / "data/layouts/layouts.json").read_text())
    lays["layouts"][0]["blockdata_overrides"] = [{"x": 1, "y": 1, "expected": 1, "value": 2}]
    (root / "data/layouts/layouts.json").write_text(json.dumps(lays))
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["firered_geometry"] = {"width": 10, "height": 8, "primary_tileset": "gTileset_General",
                                "secondary_tileset": "gTileset_Town", "blockdata_filepath": "data/layouts/Town/map.bin",
                                "border_filepath": "data/layouts/Town/border.bin", "layout_version": "hns"}
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    m = DecompProject(root).maps
    m.set_geometry("Town", secondary_tileset="gTileset_Town")  # the same blocks: the fixes stay
    assert "blockdata_overrides" not in json.loads((root / "data/maps/Town/map.json").read_text())["firered_geometry"]
    m.set_geometry("Town", blockdata_filepath="data/layouts/Town/new.bin")
    geo = json.loads((root / "data/maps/Town/map.json").read_text())["firered_geometry"]
    assert "blockdata_overrides" in geo and geo["blockdata_overrides"] is None
    assert m.layout_of("Town").get("blockdata_overrides") is None
    town.pop("firered_geometry")
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    m = DecompProject(root).maps
    m.set_geometry("Town", blockdata_filepath="data/layouts/Town/other.bin")
    assert "blockdata_overrides" not in json.loads((root / "data/layouts/layouts.json").read_text())["layouts"][0]


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
                 "open": ["0,1"], "behaviors": {"0,1": "MB_DOOR"}}]}
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
    assert te.get_metatile("Town", 514)["behavior"] == 0x11  # MB_DOOR
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
    # a neighbour skipped (no room in it yet): no seams either way, until a build that doesn't skip it
    assert seams.build(m, "Town", skip=["Town_House1"]) == []
    assert json.loads((root / "src/data/pae_seams.json").read_text()) == {}
    assert {x["target"] for x in seams.build(m, "Town")} == {"Town", "Town_House1"}
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
    # a tileset rebuild moved a door's block: its old record (now a grass block) goes at the next add
    stale = dict(rec[0], metatile=names["metatiles"]["grass"]["ids"][0], tileset="gTileset_NewGeneral")
    (root / "src/data/pae_doors.json").write_text(json.dumps(rec + [stale]))
    assert doors._still_a_door(m, rec[0]) and not doors._still_a_door(m, stale)
    doors.add(m, "Town", 2, 1, (4, 4, 12, 16))
    assert [r["metatile"] for r in json.loads((root / "src/data/pae_doors.json").read_text())] == [514]
    # drawn frames for a tall door: the door block and the one above, as they are, then the doorway darkened
    closed = [doors._block_rgb(m, "Town", int(m.read_blocks(m.layout_of("Town"))[y, 2]) & 0x3FF) for y in (0, 1)]
    block = np.zeros((32, 16, 4), np.uint8)
    for j, (rgbs, _, _) in enumerate(closed):
        for q, t in enumerate(rgbs):
            block[j * 16 + (q // 2) * 8:j * 16 + (q // 2) * 8 + 8, (q % 2) * 8:(q % 2) * 8 + 8, :3] = t
    block[..., 3] = 255
    strip = np.concatenate([block, block, block], axis=1)
    dark = block[0, 0, :3].copy()  # a colour the house's palette has
    strip[20:32, 36:44, :3] = dark  # the third frame: the doorway open
    strip[0:4, 32:48, 3] = 0  # transparent: the block as it is
    d = doors.add(m, "Town", 2, 1, frames_image=strip)
    assert d["size"] == 2
    r = json.loads((root / "src/data/pae_doors.json").read_text())[0]
    assert r["size"] == 2 and len(r["frames"]) == 3 and len(r["frames"][0]) == 8 and len(r["banks"]) == 8
    h = (root / "src/data/pae_doors.h").read_text()
    assert "{ 514, &gTileset_NewTown, DOOR_SOUND_NORMAL, 2," in h
    assert h.count("sPaeDoorTiles_0[768]") == 1  # three frames of 8 tiles
    strip[0, 0, :3] = (1, 2, 3)  # a colour no palette of the block has
    with pytest.raises(DecompError, match="colours"):
        doors.add(m, "Town", 2, 1, frames_image=strip)


def test_wide_door(root, tmp_path, monkeypatch):
    """A double door (96x32 frames): its two door blocks open together, whichever of them is walked into."""
    _, m, art, _ = _scratch(root, tmp_path)
    spec = json.loads((art / "spec.json").read_text())
    house = spec["metatiles"][3]
    house.update(open=["0,1", "1,1"], behaviors={"0,1": "MB_DOOR", "1,1": "MB_DOOR"})
    (art / "spec.json").write_text(json.dumps(spec))
    names = tb.build(m, art / "spec.json")
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown",
                   layout_version="hns")
    tb.paint(m, "Town", "..HH\n..HH\n" + "....\n" * 6, {".": "grass", "H": "house"}, names)
    m = DecompProject(root).maps
    b = m.read_blocks(m.layout_of("Town"))
    blocks = [(2 + i // 2, i % 2) for i in range(4)]  # the left column, then the right; each top block, then door
    closed = [doors._block_rgb(m, "Town", int(b[y, x]) & 0x3FF) for x, y in blocks]
    frame = np.zeros((32, 32, 4), np.uint8)
    for j, (rgbs, _, _) in enumerate(closed):
        x0, y0 = (j // 2) * 16, (j % 2) * 16
        for q, t in enumerate(rgbs):
            frame[y0 + (q // 2) * 8:y0 + (q // 2) * 8 + 8, x0 + (q % 2) * 8:x0 + (q % 2) * 8 + 8, :3] = t
    frame[..., 3] = 255
    strip = np.concatenate([frame, frame, frame], axis=1)
    strip[20:32, 72:88, :3] = frame[0, 0, :3]  # the third frame: open across both door blocks
    (root / "src/field_door.c").write_text(
        "static const struct DoorGraphics sDoorAnimGraphicsTable[] =\n{\n    {},\n};\n")
    with pytest.raises(DecompError, match="no wide doors"):
        doors.add(m, "Town", 2, 1, frames_image=strip)
    (root / "src/field_door.c").write_text("#define DOOR_WIDE_LEFT 3\n"
                                           "static const struct DoorGraphics sDoorAnimGraphicsTable[] =\n{\n    {},\n};\n")
    monkeypatch.setitem(doors.WIDE_SLOTS, "hns", range(16))  # slots the tileset draws with
    with pytest.raises(DecompError, match="reserve them"):
        doors.add(m, "Town", 2, 1, frames_image=strip)
    monkeypatch.undo()
    d = doors.add(m, "Town", 2, 1, frames_image=strip)
    left, right = int(b[1, 2]) & 0x3FF, int(b[1, 3]) & 0x3FF
    assert d["size"] == doors.WIDE_LEFT and (d["metatile"], d["right"]) == (left, right)
    rec = json.loads((root / "src/data/pae_doors.json").read_text())
    assert [(r["metatile"], r["size"]) for r in rec] == [(left, 3), (right, 4)]
    assert len(rec[0]["frames"]) == 3 and len(rec[0]["frames"][2]) == 16 and len(rec[0]["banks"]) == 16
    opened = np.array(rec[0]["frames"][2][14])  # the right door block's bottom-left tile: open, the doorway's colour
    assert (opened == opened[0, 0]).all() and not (np.array(rec[0]["frames"][0][14]) == opened[0, 0]).all()
    h = (root / "src/data/pae_doors.h").read_text()
    assert h.count("sPaeDoorTiles_0[1536]") == 1 and "sPaeDoorTiles_1" not in h  # 3 frames of 16; shared
    assert f"{{ {left}, &gTileset_NewTown, DOOR_SOUND_NORMAL, 3, sPaeDoorTiles_0, sPaeDoorPalettes_0 }}" in h
    assert f"{{ {right}, &gTileset_NewTown, DOOR_SOUND_NORMAL, 4, sPaeDoorTiles_0, sPaeDoorPalettes_1 }}" in h
    with pytest.raises(DecompError, match="isn't a door block"):
        doors.add(m, "Town", 1, 1, frames_image=strip)  # grass, then the house's left door block


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

    # edge pieces that are stamps (2x2 trees): the variant comes from the neighbouring 2x2 cells
    g0, p15, p14 = (names["metatiles"][n]["ids"][0] for n in ("grass", "path#15", "path#14"))
    names["metatiles"]["tree#15"] = {"ids": names["metatiles"]["house"]["ids"], "w": 2, "h": 2, "solid": True}
    names["metatiles"]["tree#11"] = {"ids": [g0, p15, p14, g0], "w": 2, "h": 2, "solid": True}  # open below
    b3 = tb.paint(m, "Town", "TTTT\n" * 4 + "....\n" * 4, {"T": {"auto": "tree"}, ".": "grass"}, names)
    house = names["metatiles"]["house"]["ids"]
    assert [int(b3[y, x]) & 0x3FF for y, x in ((0, 0), (1, 1), (0, 3))] == [house[0], house[3], house[1]]
    assert [int(b3[y, x]) & 0x3FF for y, x in ((2, 0), (2, 1), (3, 0), (3, 3))] == [g0, p15, p14, g0]
    assert (b3[:4] >> 10 & 3 == 1).all()
    # an inside corner: forest north and east, open to the north-east; drawn as "tree#15.1"
    names["metatiles"]["tree#15.1"] = {"ids": [p14] * 4, "w": 2, "h": 2, "solid": True}
    b4 = tb.paint(m, "Town", "TT..\nTT..\n" + "TTTT\n" * 4 + "....\n" * 2, {"T": {"auto": "tree"}, ".": "grass"},
                  names)
    assert int(b4[2, 0]) & 0x3FF == p14 and int(b4[0, 0]) & 0x3FF != p14
    # variants of a stamp ("tree#15~1"): chosen once for each 2x2 cell, never block by block
    names["metatiles"]["tree#15~1"] = {"ids": [p15] * 4, "w": 2, "h": 2, "solid": True}
    b5 = tb.paint(m, "Town", "TTTTTTTT\n" * 8, {"T": {"auto": "tree"}}, names)
    cells = {tuple(int(v) & 0x3FF for v in b5[y:y + 2, x:x + 2].ravel()) for y in range(0, 8, 2) for x in range(0, 8, 2)}
    assert cells == {tuple(house), (p15,) * 4}


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
    # sideways stairs (they move you diagonally) are kept off, like the wall
    stairs = int(m.read_blocks(lay)[3, 1]) & 0x3FF
    real = m.tileset_editor.behaviors
    names = list(real("emerald"))
    names[m.tileset_editor.get_metatile("Town", stairs)["behavior"]] = "MB_SIDEWAYS_STAIRS_LEFT_SIDE"
    m.tileset_editor.behaviors = lambda version: names
    assert not walk.walkable(m, "Town")[3][1] and not any(walk.walkable(m, "Town")[3])  # every block is that one
    m.tileset_editor.behaviors = real
    a, back = m.connect("Town", "up", "Town_House1", -3)
    assert (a["direction"], a["offset"], back["direction"], back["offset"]) == ("up", -3, "down", 3)
    assert main(["map", "connect", "Town", "up", "Town_House1", "--align", "4,2", "-p", str(root)]) == 0
    conns = DecompProject(root).maps.get_map("Town")["connections"]
    assert [c["offset"] for c in conns if c["map"] == "MAP_TOWN_HOUSE1"] == [2]


def test_tall_grass_effect(root, tmp_path):
    _, m, art, _ = _scratch(root, tmp_path)
    (root / "src/field_effect_helpers.c").write_text(
        '#include "global.h"\n#include "constants/songs.h"\n\nu32 FldEff_TallGrass(void)\n{\n'
        '    template = gFieldEffectObjectTemplatePointers[FLDEFFOBJ_TALL_GRASS];\n}\n')
    fx = np.zeros((16, 80, 4), np.uint8)
    fx[8:, :16] = (*PAL[2], 255)  # frame 0: grass over the feet, in colour 2
    fx[3, 20] = (*PAL[5], 255)
    Image.fromarray(fx, "RGBA").save(art / "grass_fx.png")
    spec = json.loads((art / "spec.json").read_text())
    spec["field_effects"] = {"tall_grass": {"image": "grass_fx.png", "bank": 0}}
    (art / "spec.json").write_text(json.dumps(spec))
    tb.build(m, art / "spec.json")
    tb.build(m, art / "spec.json")  # again: one record, hooked once
    rec = json.loads((root / "src/data/pae_field_effects.json").read_text())
    assert [r["tileset"] for r in rec] == ["gTileset_NewGeneral"]
    f0 = np.array(rec[0]["frames"][0])
    assert (f0[8:] == 2).all() and (f0[:8] == 0).all() and rec[0]["frames"][1][3][4] == 5
    header = (root / "src/data/pae_field_effects.h").read_text()
    assert "sPaeTallGrassPic_0[640]" in header
    assert "{ &gTileset_NewGeneral, PAE_TALL_GRASS_ANY, sPaeTallGrassFrames_0, &sPaeTallGrassPalette_0 }" in header
    assert "0x1040" in header and "PaeTallGrassTemplate" in header
    c = (root / "src/field_effect_helpers.c").read_text()
    assert c.count('#include "data/pae_field_effects.h"') == 1
    assert "template = PaeTallGrassTemplate(gFieldEffectObjectTemplatePointers[FLDEFFOBJ_TALL_GRASS], x, y);" in c
    # a grass block with a sprite of its own: found before the tileset's
    fx2 = fx.copy()
    fx2[8:, :16] = (*PAL[3], 255)
    Image.fromarray(fx2, "RGBA").save(art / "grass_fx2.png")
    spec["field_effects"]["tall_grass"]["metatiles"] = {"path#15": "grass_fx2.png"}
    (art / "spec.json").write_text(json.dumps(spec))
    tb.build(m, art / "spec.json")
    rec = json.loads((root / "src/data/pae_field_effects.json").read_text())
    assert list(rec[0]["by_metatile"]) == ["1"] and rec[0]["by_metatile"]["1"][0][15][0] == 3
    header = (root / "src/data/pae_field_effects.h").read_text()
    assert header.index("{ &gTileset_NewGeneral, 1, sPaeTallGrassFrames_0_1") < header.index("PAE_TALL_GRASS_ANY, sPaeT")
    assert "MapGridGetMetatileIdAt(x, y)" in header
    # an older hook (no place) is brought up to date
    (root / "src/field_effect_helpers.c").write_text(c.replace(", x, y);", ");"))
    tb.build(m, art / "spec.json")
    assert "FLDEFFOBJ_TALL_GRASS], x, y);" in (root / "src/field_effect_helpers.c").read_text()
    del spec["field_effects"]["tall_grass"]["metatiles"]
    assert c.index('"constants/songs.h"') < c.index("pae_field_effects.h")
    spec["field_effects"]["tall_grass"]["image"] = "house.png"
    (art / "spec.json").write_text(json.dumps(spec))
    with pytest.raises(DecompError, match="80x16"):
        tb.build(m, art / "spec.json")


def test_paint_pick_checker(root, tmp_path):
    _, m, art, _ = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    b = tb.paint(m, "Town", "....\n" * 8, {".": {"pick": ["grass", "path#15"], "order": "checker"}}, names)
    g, p = names["metatiles"]["grass"]["ids"][0], names["metatiles"]["path#15"]["ids"][0]
    assert [[int(v) & 0x3FF for v in r] for r in b[:2]] == [[g, p, g, p], [p, g, p, g]]


def test_paint_joins_with(root, tmp_path):
    _, m, art, _ = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    names["metatiles"]["path#15+lily"] = dict(names["metatiles"]["path#15"], ids=[512])
    legend = {".": "grass", "=": {"auto": "path"}, "v": {"auto": "path", "joins": "=", "with": "lily"}}
    b = tb.paint(m, "Town", "....\n====\n=v==\n====\n" + "....\n" * 4, legend, names)
    assert b[2, 1] & 0x3FF == 512  # surrounded by path: its "+lily" piece
    assert b[2, 0] & 0x3FF == 1 and b[2, 2] & 0x3FF == 1  # its neighbours see it as path: no edge toward it
    assert b[1, 1] & 0x3FF == 2  # path#14: grass to the north, the lily cell below counts as path


def test_seam_packs_copied_colours(root, tmp_path, monkeypatch):
    """A neighbour's copied tiles take their colours wherever a slot has them: two of its palettes whose strip
    colours fit in 15 share one slot, and the strip draws as it did."""
    _, m, art, _ = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown",
                   layout_version="hns")
    tb.paint(m, "Town", "....\n" * 8, {".": "grass"}, names)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["connections"] = [{"map": "MAP_TOWN_HOUSE1", "direction": "up", "offset": 0}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    m = DecompProject(root).maps
    te = m.tileset_editor
    for t in range(2, 12):  # the neighbour's blocks 1 (palette 1) and 2 (palette 2): three colours each
        te.set_tile("Town_House1", t, [[1 + (x + y + t) % 3 for x in range(8)] for y in range(8)])
    m = DecompProject(root).maps
    real = seams._used

    def one_free(maps, name):  # every palette slot but 12 taken, none to borrow
        tiles, _, inf = real(maps, name)
        return tiles, set(range(12)), inf
    monkeypatch.setattr(seams, "_used", one_free)
    monkeypatch.setattr(seams, "_spare", lambda maps, name, info: ([], []))
    p = seams.plan(m, "Town", ["Town_House1"])
    assert list(p["palettes"]) == [12]
    slot = p["palettes"][12]
    spal = m.tileset_editor.palettes("Town_House1")
    for mt in p["metatiles"]:
        src = m.tileset_editor.get_metatile("Town_House1", mt["mid"])["entries"]
        for e, word in zip(src, mt["tiles"]):
            want = np.array(te.get_tile("Town_House1", e["tile"]))
            if not want.any():
                continue
            assert word >> 12 == 12
            got = p["glyphs"][word & 0x3FF]
            assert [[slot[v] for v in row] for row in got.tolist()] == \
                [[spal[e["palette"]][v] for v in row] for row in want.tolist()]
    # the slots a copied palette goes in: one with its colours already, one with room, a new one, or none
    own = {0: [(0, 0, 0), (1, 1, 1), (2, 2, 2)] + [(0, 0, 0)] * 13}
    cols = [(0, 0, 0), (2, 2, 2), (1, 1, 1), (5, 5, 5)] + [(9, 9, 9)] * 12
    bank_colors, packed, free = {}, {}, [11, 12]
    assert seams._pack({1, 2}, cols, own, bank_colors, packed, free) == 0
    assert seams._pack({1, 3}, cols, own, bank_colors, packed, free) == 11 and packed == {11: 2}
    big = [(0, 0, 0)] + [(k, 0, 0) for k in range(10, 25)]
    assert seams._pack(set(range(1, 14)), big, own, bank_colors, packed, free) == 11 and packed == {11: 15}
    assert seams._pack({14, 15}, big, own, bank_colors, packed, free) == 12
    assert seams._pack(set(range(1, 16)), [(0, 0, 0)] + [(k, 1, 1) for k in range(15)], own, bank_colors, packed,
                       free) is None


def test_seams_share_the_kit(root, tmp_path):
    """Two maps with the same primary tileset: the strip uses the primary's tiles and the map's own palettes."""
    proj, m, art, _ = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    tb.new_tileset(proj, "gTileset_NewTown2", secondary=True)
    m = DecompProject(root).maps
    spec = json.loads((art / "spec.json").read_text())
    spec["secondary"] = "gTileset_NewTown2"
    (art / "spec.json").write_text(json.dumps(spec))
    names2 = tb.build(m, art / "spec.json")
    for name, sec, nm in (("Town", "gTileset_NewTown", names), ("Town_House1", "gTileset_NewTown2", names2)):
        m.set_geometry(name, primary_tileset="gTileset_NewGeneral", secondary_tileset=sec, layout_version="hns")
        tb.paint(m, name, "..HH\n..HH\n" + "....\n" * 6, {".": "grass", "H": "house"}, nm)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["connections"] = [{"map": "MAP_TOWN_HOUSE1", "direction": "up", "offset": 0}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    (root / "src/kanto_connection_seam.c").write_text(
        '#include "global.h"\n#include "kanto_connection_seam.h"\n\n'
        'static const struct KantoSeam sKantoSeams[] = {\n};\n')
    m = DecompProject(root).maps
    p = seams.plan(m, "Town", ["Town_House1"])
    assert p["palettes"] == {}  # the neighbour's palettes are the map's own
    grass_tiles = {e & 0x3FF for mt in p["metatiles"] if mt["mid"] == 0 for e in mt["tiles"]}
    assert grass_tiles and max(grass_tiles) < 512  # primary tiles used as they are
    house = [mt for mt in p["metatiles"] if mt["mid"] >= 512]
    assert house and any((e & 0x3FF) >= 512 for mt in house for e in mt["tiles"])  # its secondary tiles copied


def test_seams_beside_a_hand_made_one(root, tmp_path):
    proj, m, art, _ = _scratch(root, tmp_path)
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
    hand = ('#include "global.h"\n#include "kanto_connection_seam.h"\n\n'
            "static const struct KantoSeamGlyphRun sHand_GlyphRuns[] = {\n    { sHand_GlyphRun0, 1023, 32 },\n};\n"
            "static const struct KantoSeamPalette ALIGNED(4) sHand_Palettes[] = {\n    { { 0x0000 }, 192 },\n};\n"
            "static const struct KantoSeam sKantoSeams[] = {\n"
            "    { &Town_Layout, sHand_Metatiles, sHand_GlyphRuns, sHand_Palettes, NULL, 1000, 23, 1, 1, 0, 0 },\n"
            "};\n")
    (root / "src/kanto_connection_seam.c").write_text(hand)
    m = DecompProject(root).maps
    with pytest.raises(DecompError, match="KantoSeam_FindNext"):
        seams.build(m, "Town")
    (root / "src/kanto_connection_seam.c").write_text(hand + "// KantoSeam_FindNext\n")
    m = DecompProject(root).maps
    assert seams.hand_made(m, "Town_Layout") == {"first": 1000, "count": 23, "tiles": {1023}, "banks": {12}}
    r = {x["target"]: x for x in seams.build(m, "Town")}
    header = (root / "src/data/pae_seams.h").read_text()
    p = seams.plan(m, "Town", ["Town_House1"], seams.hand_made(m, "Town_Layout"))
    assert p["first"] + len(p["metatiles"]) == 1000  # right below the hand-made range
    assert 1023 not in p["glyphs"] and 12 not in p["palettes"]
    assert r["Town"]["sources"] == ["Town_House1"] and f", {p['first']}, " in header
    c = (root / "src/kanto_connection_seam.c").read_text()
    assert c.index("&Town_Layout, sHand") < c.index("    PAE_SEAMS\n};")  # pae's entries after the hand-made one


def test_ground_that_covers(root, tmp_path):
    """A metatile on a ground whose art covers it (a barrel on a dock) shows that art under its own."""
    _, m, art, _ = _scratch(root, tmp_path)
    boards = np.full((16, 16), 3, np.uint8)
    barrel = np.zeros((16, 16), np.uint8)
    barrel[4:12, 4:12] = 5
    _art(art / "dock.png", [boards, barrel])
    spec = json.loads((art / "spec.json").read_text())
    spec["metatiles"] += [{"name": "dock", "tileset": "primary", "image": "dock.png", "x": 0, "ground": "grass"},
                          {"name": "barrel", "tileset": "primary", "image": "dock.png", "x": 16, "ground": "dock"}]
    (art / "spec.json").write_text(json.dumps(spec))
    names = tb.build(m, art / "spec.json")
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    te = m.tileset_editor
    dock = te.get_metatile("Town", names["metatiles"]["dock"]["ids"][0])["entries"]
    barrel_e = te.get_metatile("Town", names["metatiles"]["barrel"]["ids"][0])["entries"]
    assert barrel_e[:4] == dock[4:]  # the boards, not the grass under them


def test_ground_for_each_block(root, tmp_path):
    """A stamp's ground can be a list, one for each block (row by row); a list of the wrong length is refused."""
    _, m, art, _ = _scratch(root, tmp_path)
    a, b = np.full((16, 16), 3, np.uint8), np.full((16, 16), 4, np.uint8)
    roof = np.zeros((16, 16), np.uint8)
    roof[8:, :] = 5
    _art(art / "rock.png", [a, b])
    _art(art / "roof.png", [roof, roof])
    spec = json.loads((art / "spec.json").read_text())
    spec["metatiles"] += [{"name": "rock_a", "tileset": "primary", "image": "rock.png", "x": 0},
                          {"name": "rock_b", "tileset": "primary", "image": "rock.png", "x": 16},
                          {"name": "roof", "tileset": "primary", "image": "roof.png", "w": 2,
                           "ground": ["rock_a", "rock_b"]}]
    (art / "spec.json").write_text(json.dumps(spec))
    names = tb.build(m, art / "spec.json")
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown")
    te = m.tileset_editor
    ids = names["metatiles"]["roof"]["ids"]
    for i, under in zip(ids, ("rock_a", "rock_b")):
        rock = te.get_metatile("Town", names["metatiles"][under]["ids"][0])["entries"]
        assert te.get_metatile("Town", i)["entries"][:4] == rock[:4]
    spec["metatiles"][-1]["ground"] = ["rock_a"]
    (art / "spec.json").write_text(json.dumps(spec))
    with pytest.raises(DecompError, match="1 grounds for its 2 blocks"):
        tb.build(m, art / "spec.json")


def test_tilesets_by_alias_and_inline_arrays(root):
    """A tileset whose data is another's (``__attribute__((alias(...)))``) or whose metatiles are written out as a C
    array resolves like the others: maps using it render and walk; the inline metatiles are read only."""
    g = root / "src/data/tilesets/graphics.h"
    g.write_text(g.read_text() + 'extern const u32 gTilesetTiles_Shop[] __attribute__((alias("gTilesetTiles_Town")));\n'
                 'extern const u16 gTilesetPalettes_Shop[16][16] __attribute__((alias("gTilesetPalettes_Town")));\n')
    mt = root / "src/data/tilesets/metatiles.h"
    words = ", ".join("0x0201" for _ in range(8 * 8))
    mt.write_text(mt.read_text() + 'extern const u16 gMetatiles_Shop[] __attribute__((alias("gMetatiles_Town")));\n'
                  f"const u16 gMetatiles_Cave[] =\n{{\n    {words}, // eight metatiles (the house uses its sixth)\n}};\n")
    h = root / "src/data/tilesets/headers.h"
    for sym, metas in (("Shop", "gMetatiles_Shop"), ("Cave", "gMetatiles_Cave")):
        h.write_text(h.read_text() + f"const struct Tileset gTileset_{sym} =\n{{\n    .isCompressed = TRUE,\n"
                     "    .isSecondary = TRUE,\n    .tiles = gTilesetTiles_Shop,\n    .palettes = gTilesetPalettes_Shop,\n"
                     f"    .metatiles = {metas},\n    .metatileAttributes = gMetatileAttributes_Town,\n}};\n")
    m = DecompProject(root).maps
    shop, cave, town = (m.tilesets[f"gTileset_{s}"] for s in ("Shop", "Cave", "Town"))
    assert (shop.tiles, shop.palettes, shop.metatiles) == (town.tiles, town.palettes, town.metatiles)
    assert (root / cave.metatiles).read_bytes() == b"\x01\x02" * 64
    for ts in ("gTileset_Shop", "gTileset_Cave"):
        m.set_geometry("Town_House1", secondary_tileset=ts)
        assert walk.walk(m, "Town_House1", [(1, 2), (4, 3)])[1] == (4, 3)
        m.render("Town_House1")
    te = m.tileset_editor
    _, sec, version = te.sides(m.layout_of("Town_House1"))
    with pytest.raises(DecompError, match="C array"):
        te._write_metas(sec, version, te._read_metas(sec), [0] * 8)


def test_seam_takes_nearest_colours_with_no_room(root, tmp_path, monkeypatch):
    """With no palette slot free or to borrow, a neighbour's copied tiles take each colour's nearest in the slot
    closest to them, and the plan says which palette was drawn so."""
    _, m, art, _ = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown",
                   layout_version="hns")
    tb.paint(m, "Town", "....\n" * 8, {".": "grass"}, names)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["connections"] = [{"map": "MAP_TOWN_HOUSE1", "direction": "up", "offset": 0}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    m = DecompProject(root).maps
    real = seams._used

    def none_free(maps, name):
        tiles, _, inf = real(maps, name)
        return tiles, set(range(13)), inf
    monkeypatch.setattr(seams, "_used", none_free)
    monkeypatch.setattr(seams, "_spare", lambda maps, name, info: ([], []))
    monkeypatch.setattr(seams, "_pack", lambda *a: None)  # (nothing fits as it is)
    p = seams.plan(m, "Town", ["Town_House1"])
    assert p["approximated"] and not p["palettes"]
    tpal = m.tileset_editor.palettes("Town")
    for a in p["approximated"]:
        assert a["source"] == "Town_House1" and a["slot"] < len(tpal)
    # the nearest: each colour to its closest in the slot nearest overall
    own = {0: [(0, 0, 0), (10, 10, 10), (200, 0, 0)] + [(0, 0, 0)] * 13,
           1: [(0, 0, 0), (0, 0, 250), (90, 90, 90)] + [(0, 0, 0)] * 13}
    cols = [(0, 0, 0), (12, 12, 12), (190, 5, 5)] + [(0, 0, 0)] * 13
    assert seams._nearest({1, 2}, cols, own, {}, {}) == (0, {1: 1, 2: 2}, 12)


def test_seam_borrows_metatile_ids_the_map_never_shows(root, tmp_path, monkeypatch):
    """A map whose tilesets fill every metatile id still takes a seam: its adapters go in the top ids it never
    shows, and an id it shows between them is an empty entry (the game draws the map's own metatile there)."""
    _, m, art, _ = _scratch(root, tmp_path)
    names = json.loads((art / "names.json").read_text())
    m.set_geometry("Town", primary_tileset="gTileset_NewGeneral", secondary_tileset="gTileset_NewTown",
                   layout_version="hns")
    tb.paint(m, "Town", "....\n" * 8, {".": "grass"}, names)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["connections"] = [{"map": "MAP_TOWN_HOUSE1", "direction": "up", "offset": 0}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    m = DecompProject(root).maps
    real_used, real_shown = seams._used, seams._shown

    def full(maps, name):
        tiles, banks, inf = real_used(maps, name)
        return tiles, banks, dict(inf, metatiles=1024)
    monkeypatch.setattr(seams, "_used", full)
    monkeypatch.setattr(seams, "_shown", lambda maps, name: real_shown(maps, name) | {1022})
    n = len(seams.strip(m, "Town", "Town_House1"))
    p = seams.plan(m, "Town", ["Town_House1"])
    ids = [p["first"] + i for i, e in enumerate(p["metatiles"]) if e]
    assert len(ids) == n and 1022 not in ids and p["metatiles"][1022 - p["first"]] is None
    assert p["first"] + len(p["metatiles"]) == 1024 and p["borrowed"]["metatiles"] == n
    assert "{ .sourceLayout = NULL }," in seams.render_header([p])
    monkeypatch.setattr(seams, "_shown", lambda maps, name: None)  # can't tell what it shows: no room
    with pytest.raises(DecompError, match="don't fit"):
        seams.plan(m, "Town", ["Town_House1"])

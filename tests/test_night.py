"""Night: maps as the game shows them after dark, where each colour is used, and light colours on any tileset."""
import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor import night, walk
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.gfx import read_jasc, write_jasc


@pytest.fixture()
def root(tmp_path):
    return decomp_fixture.build(tmp_path / "game")


def test_night_colours_are_the_games():
    """The game's sums, in 5 bits: unlit x 0.456 / 0.456 / 0.615; lit halfway to the light; colour 0 kept."""
    pal = [(0, 0, 0), (248, 248, 248), (8, 16, 24), (248, 0, 0)]
    got = night.night_palette(pal, [3])
    assert got[:4] == [(0, 0, 0), (112, 112, 152), (0, 0, 8), (248, 112, 56)]  # 3: halfway to (248, 224, 120)
    own = night.night_palette([(232, 200, 248)] + pal[1:], [0, 3])
    assert own[0] == (232, 200, 248) and own[3] == (240, 96, 120)  # halfway to its own light, colour 0
    assert len(got) == 16


def test_a_map_at_night(root):
    m = DecompProject(root).maps
    lay = m.layout_of("Town")
    blocks = m.read_blocks(lay)
    where = m.where(lay, blocks)
    day = np.array(m.render("Town", events=False))[..., :3]
    dark = np.array(m.render("Town", events=False, night=True))[..., :3]
    assert day.shape == dark.shape == (8 * 16, 10 * 16, 3)
    assert np.array_equal(m.palette_lut(lay)[where], day)  # every pixel is the colour it says it is
    assert dark.astype(int).sum() < day.astype(int).sum()
    # light the colours of one bank: its pixels go to the warm light, the rest stay as dark as before
    bank = int(np.bincount((where >> 4).ravel()).argmax())
    sym, pal_file = m.palette_files(lay)[bank]
    assert m.tileset_lights(sym, {bank: (list(range(1, 16)), None)}) == {bank: list(range(1, 16))}
    lit = np.array(m.render("Town", events=False, night=True))[..., :3]
    cols = read_jasc(root / pal_file)
    want = np.array(night.night_palette(cols, range(1, 16)), np.uint8)
    mine = ((where >> 4) == bank) & ((where & 15) != 0)
    assert np.array_equal(lit[mine], want[where[mine] & 15])
    assert np.array_equal(lit[~mine], dark[~mine])
    assert main(["map", "render", "Town", "--night", "-o", str(root / "n.png"), "-p", str(root)]) == 0
    assert Image.open(root / "n.png").size == (160, 128)


def test_glows_at_night(root):
    m = DecompProject(root).maps
    consts = root / "include/constants/event_objects.h"
    consts.parent.mkdir(parents=True, exist_ok=True)
    consts.write_text((consts.read_text() if consts.exists() else "") + "#define OBJ_EVENT_GFX_LIGHT_SPRITE 241\n")
    plain = np.array(m.render("Town", events=False, night=True), np.int32)
    walk.lights(m, "Town", [(4, 4, "ball")])
    assert np.array_equal(np.array(m.render("Town", events=False, night=True), np.int32), plain)  # no picture: none
    pics = root / "graphics/object_events/pics/misc"
    pics.mkdir(parents=True, exist_ok=True)
    idx = np.zeros((32, 32), np.uint8)
    idx[8:24, 8:24] = 1
    im = Image.frombytes("P", (32, 32), idx.tobytes())
    im.putpalette([0, 0, 0, 248, 224, 120] + [0] * 42)
    im.save(pics / "light.png")
    write_jasc(root / "graphics/object_events/palettes/light.pal", [(0, 0, 0), (248, 224, 120)] + [(0, 0, 0)] * 14)
    glow = np.array(m.render("Town", events=False, night=True), np.int32)
    x0, y0 = 4 * 16 + night.GLOWS[0][1][0], 4 * 16 + night.GLOWS[0][1][1]
    inside = (slice(y0 + 8, y0 + 24), slice(x0 + 8, x0 + 24))
    want = np.minimum(31, ((np.array([248, 224, 120]) >> 3) * 7 + (plain[inside][..., :3] >> 3) * 12) >> 4) << 3
    assert np.array_equal(glow[inside][..., :3], want)  # the GBA's blend, 7/16 over 12/16
    outside = np.ones(glow.shape[:2], bool)
    outside[inside] = False
    assert np.array_equal(glow[outside], plain[outside])
    day = np.array(m.render("Town", events=False), np.int32)
    assert np.array_equal(day, np.array(DecompProject(root).maps.render("Town", events=False), np.int32))


def test_where_each_colour_is_used(root):
    m = DecompProject(root).maps
    rows = m.colours("Town")
    lay = m.layout_of("Town")
    where = m.where(lay, m.read_blocks(lay))
    assert sum(r["pixels"] for r in rows) == int(((where & 15) != 0).sum())
    assert all(not r["lit"] for r in rows) and all(r["metatiles"] for r in rows)
    r = rows[0]
    sym, _f = m.palette_files(lay)[r["bank"]]
    m.tileset_lights(sym, {r["bank"]: ([r["colour"]], None)})
    assert [x for x in m.colours("Town") if x["lit"]] == [dict(r, lit=True)]
    sheet = m.colour_sheet("Town", r["bank"])
    assert sheet.width > 16 and sheet.height > 16
    out = root / "sheet.png"
    assert main(["map", "colours", "Town", "--bank", str(r["bank"]), "-o", str(out), "-p", str(root)]) == 0
    assert out.exists()
    assert main(["map", "colours", "Town", "-o", str(out), "-p", str(root)]) != 0  # pictures need a bank


def test_light_colours_on_any_tileset(root):
    m = DecompProject(root).maps
    ts = m.tilesets["gTileset_Town"]
    assert m.tileset_lights("gTileset_Town") == {}
    got = m.tileset_lights("gTileset_Town", {7: ([3, 1, 3], None), 8: ([2], (232, 200, 248))})
    assert got == {7: [1, 3], 8: [0, 2]}
    pla = root / night.pla_of(root / ts.palettes[8]).relative_to(root)
    assert all(len(ln) < 64 for ln in pla.read_text().splitlines())  # what gbagfx reads
    assert read_jasc(root / ts.palettes[8])[0] == (232, 200, 248)  # the light colour, in colour 0
    assert night.read_lights(root / ts.palettes[7]) == [1, 3]
    assert m.tileset_lights("gTileset_Town", clear=[7]) == {8: [0, 2]}
    for bad, why in (({0: ([1], (1, 2, 3))}, "backdrop"), ({7: ([0], None)}, "indices 1-15"),
                     ({7: ([16], None)}, "indices 1-15"), ({7: ([1], (1, 2))}, "r,g,b"), ({20: ([1], None)}, "no JASC")):
        with pytest.raises(DecompError, match=why):
            m.tileset_lights("gTileset_General" if 0 in bad else "gTileset_Town", bad)
    with pytest.raises(DecompError, match="no tileset"):
        m.tileset_lights("gTileset_Nowhere")
    assert main(["tileset", "lights", "gTileset_Town", "7:4,5", "9:1@200,180,248", "-p", str(root)]) == 0
    again = DecompProject(root).maps
    assert again.tileset_lights("gTileset_Town") == {7: [4, 5], 8: [0, 2], 9: [0, 1]}
    assert main(["tileset", "lights", "gTileset_Town", "--clear", "8", "--clear", "9", "-p", str(root)]) == 0
    assert DecompProject(root).maps.tileset_lights("gTileset_Town") == {7: [4, 5]}
    assert main(["tileset", "lights", "gTileset_Town", "7:x", "-p", str(root)]) != 0


def test_glass_on_a_lit_copy_of_its_palette(root):
    """rebank: some tiles drawn with a copy of their palette in another bank, so only the copy glows; refused
    until the copy holds the same colours, and a door block stays as it is."""
    m = DecompProject(root).maps
    lay = m.layout_of("Town")
    day = np.array(m.render("Town", events=False))
    with pytest.raises(DecompError, match="doesn't hold bank 1's colour"):
        m.rebank("Town", 1, 9, blocks=[(0, 1)])  # metatile 1, bank 1 (the primary's), onto bank 9 as it is
    with pytest.raises(DecompError, match="no blocks"):
        m.rebank("Town", 1, 9)
    with pytest.raises(DecompError, match="off Town"):
        m.rebank("Town", 1, 9, blocks=[(99, 0)])
    assert main(["tileset", "copy-bank", "gTileset_General", "1", "gTileset_Town", "9", "-p", str(root)]) == 0
    r = m.rebank("Town", 1, 9, blocks=[(0, 1)])
    assert r == {"metatiles": [1], "doors": []}
    assert {e["palette"] for e in m.tileset_editor.get_metatile("Town", 1)["entries"]} == {9}
    assert np.array_equal(np.array(m.render("Town", events=False)), day)  # by day nothing changes
    m.tileset_lights("gTileset_Town", {9: (list(range(1, 16)), None)})
    where = m.where(lay, m.read_blocks(lay))
    assert ((where >> 4) == 9).any() and not ((where >> 4) == 1).any()  # the copy draws them now
    # only the tiles that show a colour, but not those that also show another (a roof's own)
    cols = {int(v) & 15 for v in np.unique(where[(where >> 4) == 2])} - {0}
    one, other = sorted(cols)[0], sorted(cols)[-1]
    m.copy_bank("gTileset_General", 2, "gTileset_Town", 10)
    r = m.rebank("Town", 2, 10, on=(2, one), colours=[one], except_colours=[other] if other != one else None)
    te = m.tileset_editor
    for e in te.get_metatile("Town", 2)["entries"]:
        u = {v for row in te.get_tile("Town", e["tile"]) for v in row} - {0}
        assert (e["palette"] == 10) == (one in u and (other == one or other not in u))
    # a door block is left as it is
    beh = root / "include/constants/metatile_behaviors.h"
    beh.parent.mkdir(parents=True, exist_ok=True)
    beh.write_text("enum {\n    MB_NORMAL,\n    MB_ANIMATED_DOOR,\n};\n")
    m2 = DecompProject(root).maps
    m2.tileset_editor.set_metatile("Town", 512 + 5, behavior="MB_ANIMATED_DOOR")
    m2.copy_bank("gTileset_Town", 8, "gTileset_Town", 11)
    assert m2.rebank("Town", 8, 11, blocks=[(3, 2)]) == {"metatiles": [], "doors": [517]}
    with pytest.raises(DecompError, match="two different banks"):
        m2.rebank("Town", 8, 8, blocks=[(3, 2)])
    assert main(["map", "rebank", "Town", "0,1", "--from", "9", "--to", "1", "-p", str(root)]) == 0  # and back
    assert {e["palette"] for e in DecompProject(root).maps.tileset_editor.get_metatile("Town", 1)["entries"]} == {1}


def test_a_glow_on_every_lamp(root):
    """map lights --on BANK:COLOUR: a glow on every block that shows that colour (a street lamp's glass)."""
    m = DecompProject(root).maps
    consts = root / "include/constants/event_objects.h"
    consts.parent.mkdir(parents=True, exist_ok=True)
    consts.write_text((consts.read_text() if consts.exists() else "") + "#define OBJ_EVENT_GFX_LIGHT_SPRITE 241\n")
    lay = m.layout_of("Town")
    where = m.where(lay, m.read_blocks(lay))
    v = int(np.unique(where[(where >> 4) == 8])[-1])  # a colour of the secondary's block at (3, 2) only
    assert walk.blocks_showing(m, "Town", 8, v & 15) == [(3, 2)]
    assert main(["map", "lights", "Town", "1,1", "--on", f"8:{v & 15}", "-p", str(root)]) == 0
    assert walk.lights(DecompProject(root).maps, "Town") == [{"x": 1, "y": 1, "kind": "ball"},
                                                           {"x": 3, "y": 2, "kind": "ball"}]
    assert main(["map", "lights", "Town", "--on", "8:x", "-p", str(root)]) != 0


def test_repaint_a_colour_as_its_unlit_twin(root):
    """recolour: a colour repainted as another alike in the chosen blocks' tiles; refused if they differ by day or a
    tile is drawn elsewhere too."""
    m = DecompProject(root).maps
    ts = m.tilesets["gTileset_Town"]
    te = m.tileset_editor
    # a block of its own at (5, 5): a new tile in colours 3 and 4 of bank 8, in a new metatile
    px = [[3 if (x + y) % 2 else 4 for x in range(8)] for y in range(8)]
    # (the fixture's blocks draw tiles up to 575, past the 60 it has: the new one goes after them)
    tile = te.add_tiles("Town", 5, "secondary", pixels=[np.zeros((8, 8), np.uint8)] * 4 + [np.array(px, np.uint8)])[-1]
    mid = te.add_metatiles("Town", 1)[0]
    te.set_metatile("Town", mid, entries=[{"tile": tile, "palette": 8}] * 4 + [{"tile": 0, "palette": 8}] * 4)
    lay = m.layout_of("Town")
    blocks = m.read_blocks(lay)
    blocks[5, 5] = mid
    m.write_blocks("Town", blocks)
    with pytest.raises(DecompError, match="aren't the same colour"):
        m.recolour("Town", 8, 3, 9, blocks=[(5, 5)])
    cols = read_jasc(root / ts.palettes[8])
    cols[9] = cols[3]
    write_jasc(root / ts.palettes[8], cols)
    m = DecompProject(root).maps
    day = np.array(m.render("Town", events=False))
    r = m.recolour("Town", 8, 3, 9, blocks=[(5, 5)])
    assert r == {"tiles": [tile], "metatiles": [mid]}
    assert np.array_equal(np.array(m.render("Town", events=False)), day)  # by day nothing changes
    assert {v for row in m.tileset_editor.get_tile("Town", tile) for v in row} == {4, 9}
    # a tile another block draws too is refused
    md = m.tileset_editor.get_metatile("Town", 517)
    c = next(v for e in md["entries"] for row in m.tileset_editor.get_tile("Town", e["tile"]) for v in row if v)
    cols[15] = cols[c]
    write_jasc(root / ts.palettes[8], cols)
    m = DecompProject(root).maps
    with pytest.raises(DecompError, match="also drawn by metatile"):
        m.recolour("Town", 8, c, 15, blocks=[(3, 2)])
    assert main(["map", "recolour", "Town", "5,5", "--bank", "8", "--colour", "9", "--to", "3", "-p", str(root)]) == 0


def test_a_ports_lamp_sprites_become_glows(root):
    m = DecompProject(root).maps
    with pytest.raises(DecompError, match="no light sprite"):
        walk.adopt_lights(m, "Town", "OBJ_EVENT_GFX_BOY_1")
    consts = root / "include/constants/event_objects.h"
    consts.parent.mkdir(parents=True, exist_ok=True)
    consts.write_text((consts.read_text() if consts.exists() else "") + "#define OBJ_EVENT_GFX_LIGHT_SPRITE 241\n")
    data = m.get_map("Town")
    data["object_events"] = data.get("object_events", []) + [
        dict(DEFAULT_LAMP, x=2, y=3, flag="FLAG_X"), dict(DEFAULT_LAMP, x=4, y=1)]
    m.save_map("Town", data)
    before = m.get_map("Town")["object_events"]
    assert main(["map", "lights", "Town", "--adopt", "OBJ_EVENT_GFX_OLD_LAMP", "-p", str(root)]) == 0
    after = DecompProject(root).maps.get_map("Town")["object_events"]
    assert len(after) == len(before) and after[:-2] == before[:-2]  # the others, and every local id, as they were
    assert [(o["graphics_id"], o["x"], o["y"], o["flag"], o["trainer_sight_or_berry_tree_id"]) for o in after[-2:]] == [
        ("OBJ_EVENT_GFX_LIGHT_SPRITE", 2, 3, "FLAG_X", "0"), ("OBJ_EVENT_GFX_LIGHT_SPRITE", 4, 1, "0", "0")]


DEFAULT_LAMP = {"graphics_id": "OBJ_EVENT_GFX_OLD_LAMP", "x": 0, "y": 0, "elevation": 0,
                "movement_type": "MOVEMENT_TYPE_NONE", "movement_range_x": 0, "movement_range_y": 0,
                "trainer_type": "TRAINER_TYPE_NONE", "trainer_sight_or_berry_tree_id": "3", "script": "NULL",
                "flag": "0"}

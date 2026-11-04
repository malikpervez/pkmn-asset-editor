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

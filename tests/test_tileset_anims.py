import json
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.mcp import Server

ANIMS_C = """\
#include "global.h"

static void QueueAnimTiles_General_Water(u16);
static void QueueAnimTiles_General_Steam(u16);

const u16 gTilesetAnims_General_Water_Frame0[] = INCGFX_U16("data/tilesets/primary/general/anim/water/0.png", ".4bpp");
const u16 gTilesetAnims_General_Water_Frame1[] = INCGFX_U16("data/tilesets/primary/general/anim/water/1.png", ".4bpp");
const u16 gTilesetAnims_General_Water_Frame2[] = INCGFX_U16("data/tilesets/primary/general/anim/water/2.png", ".4bpp");

const u16 *const gTilesetAnims_General_Water[] = {
    gTilesetAnims_General_Water_Frame0,
    gTilesetAnims_General_Water_Frame1,
    gTilesetAnims_General_Water_Frame2,
    gTilesetAnims_General_Water_Frame1
};

const u16 gTilesetAnims_General_Steam_Frame0[] = INCGFX_U16("data/tilesets/primary/general/anim/steam/0.png", ".4bpp");
const u16 gTilesetAnims_General_Steam_Frame1[] = INCGFX_U16("data/tilesets/primary/general/anim/steam/1.png", ".4bpp");

const u16 *const gTilesetAnims_General_Steam[] = {
    gTilesetAnims_General_Steam_Frame0,
    gTilesetAnims_General_Steam_Frame1
};

static void AppendTilesetAnimToBuffer(const u16 *src, u16 *dest, u16 size) {}

void InitTilesetAnim_General(void)
{
    sPrimaryTilesetAnimCounter = 0;
    sPrimaryTilesetAnimCounterMax = 256;
    sPrimaryTilesetAnimCallback = TilesetAnim_General;
}

static void TilesetAnim_General(u16 timer)
{
    if (timer % 16 == 1)
        QueueAnimTiles_General_Water(timer / 16);
    if (timer % 8 == 0)
    {
        QueueAnimTiles_General_Steam(timer / 8);
    }
}

static void QueueAnimTiles_General_Water(u16 timer)
{
    u16 i = timer % ARRAY_COUNT(gTilesetAnims_General_Water);
    AppendTilesetAnimToBuffer(gTilesetAnims_General_Water[i], (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(8)), 4 * TILE_SIZE_4BPP);
}

static void QueueAnimTiles_General_Steam(u16 timer)
{
    u16 i = timer % ARRAY_COUNT(gTilesetAnims_General_Steam);
    AppendTilesetAnimToBuffer(gTilesetAnims_General_Steam[i], (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(20)), 2 * TILE_SIZE_4BPP);
    i = (timer + 1) % ARRAY_COUNT(gTilesetAnims_General_Steam);
    AppendTilesetAnimToBuffer(gTilesetAnims_General_Steam[i], (u16 *)(BG_VRAM + TILE_OFFSET_4BPP(22)), 2 * TILE_SIZE_4BPP);
}
"""


def frame(path, w, h, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.frombytes("P", (w, h), bytes([value]) * (w * h))
    im.putpalette([v for i in range(16) for v in (i * 16, i * 16, i * 16)])
    im.save(path)


@pytest.fixture()
def root(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    (root / "src/tileset_anims.c").write_text(ANIMS_C)
    (root / "include/tileset_anims.h").write_text("#ifndef GUARD_TILESET_ANIMS_H\n#define GUARD_TILESET_ANIMS_H\n\n"
                                                  "void InitTilesetAnim_General(void);\n\n#endif // GUARD\n")
    hp = root / "src/data/tilesets/headers.h"
    hp.write_text(hp.read_text().replace("    .metatileAttributes = gMetatileAttributes_General,\n",
                                         "    .metatileAttributes = gMetatileAttributes_General,\n"
                                         "    .callback = InitTilesetAnim_General,\n"))
    anim = root / "data/tilesets/primary/general/anim"
    for i in range(3):
        frame(anim / f"water/{i}.png", 16, 16, 3 + i)
    for i in range(2):
        frame(anim / f"steam/{i}.png", 16, 8, 9 + i)
    return root


@pytest.fixture()
def ta(root):
    return DecompProject(root).maps.tileset_anims


def test_parse(ta):
    water = ta.get("General_Water")
    assert water.sequence == [0, 1, 2, 1] and len(water.frames) == 3
    assert water.period == 16 and water.count == 4 and [(s.dest, s.phase) for s in water.slots] == [(8, 1)]
    assert water.tilesets == ["gTileset_General"] and water.counter_max == 256
    assert water.pngs[water.frames[2]] == "data/tilesets/primary/general/anim/water/2.png"
    steam = ta.get("general_steam")  # ids are case-insensitive
    assert [(s.dest, s.phase, s.offset) for s in steam.slots] == [(20, 0, 0), (22, 0, 1)]
    assert steam.period == 8
    assert [a["id"] for a in ta.list("Town")] == ["General_Water", "General_Steam"]
    with pytest.raises(DecompError, match="no tileset animation"):
        ta.get("Nope")


def test_preview_timeline(ta):
    sheet, info = ta.preview("Town")
    mts = info["metatiles"]
    assert mts and sheet.height == 16 * len(info["times"]) and sheet.width == 16 * len(mts)
    assert info["cycle"] == 256 and info["times"][0] == 0
    # water changes every 16 frames on phase 1, steam every 8 on phase 0
    assert 1 in info["times"] and 17 in info["times"] and 8 in info["times"]
    rows = np.array(sheet).reshape(len(info["times"]), 16, -1, 4)
    assert not np.array_equal(rows[0], rows[1])
    frames, ms = ta.map_frames("Town")
    assert 1 < len(frames) <= len(info["times"]) and len(ms) == len(frames)
    assert abs(sum(ms) - 256 * 1000 / 60) < 20 * len(ms)
    img = ta.frames_image("General_Water", "Town")
    assert img.width > 3 * 16 * 3
    assert set(ta.animated_metatiles("Town", "General_Water")) <= set(mts)


def test_speed_and_order(ta, root):
    a = ta.set_speed("General_Water", 8)
    assert a.period == 8 and a.slots[0].phase == 1
    text = (root / "src/tileset_anims.c").read_text()
    assert "if (timer % 8 == 1)\n        QueueAnimTiles_General_Water(timer / 8);" in text
    # steam shares nothing, but sits in a braced block: still rewritten in place
    a = ta.set_speed("General_Steam", 4)
    assert a.period == 4
    assert "QueueAnimTiles_General_Steam(timer / 4);" in (root / "src/tileset_anims.c").read_text()
    a = ta.set_sequence("General_Water", [0, 2, 2, 1])
    assert [a.frames[i][-1] for i in a.sequence] == ["0", "2", "2", "1"]
    with pytest.raises(DecompError, match="does not exist"):
        ta.set_sequence("General_Water", [0, 7])
    with pytest.raises(DecompError, match="between 1 and 255"):
        ta.set_speed("General_Water", 0)


def test_speed_in_shared_block(ta, root):
    p = root / "src/tileset_anims.c"
    p.write_text(p.read_text().replace("    if (timer % 8 == 0)\n    {\n        QueueAnimTiles_General_Steam(timer / 8);\n",
                                       "    if (timer % 8 == 0)\n    {\n        QueueAnimTiles_General_Steam(timer / 8);\n"
                                       "        QueueAnimTiles_General_Water(timer / 8);\n"))
    p.write_text(p.read_text().replace("    if (timer % 16 == 1)\n        QueueAnimTiles_General_Water(timer / 16);\n", ""))
    ta._cache = None
    a = ta.set_speed("General_Steam", 12)
    text = p.read_text()
    assert "QueueAnimTiles_General_Water(timer / 8);" in text  # the other call keeps its block
    assert "    if (timer % 12 == 0)\n        QueueAnimTiles_General_Steam(timer / 12);\n}" in text
    assert a.period == 12 and ta.get("General_Water").period == 8


def test_frames_add_replace_remove(ta, root):
    r = ta.add_frame("General_Water")
    assert r["symbol"] == "gTilesetAnims_General_Water_Frame3"
    assert r["png"] == "data/tilesets/primary/general/anim/water/3.png" and (root / r["png"]).exists()
    a = ta.get("General_Water")
    assert len(a.frames) == 4 and a.sequence[-1] == 3
    text = (root / "src/tileset_anims.c").read_text()
    assert 'const u16 gTilesetAnims_General_Water_Frame3[] = INCGFX_U16("data/tilesets/primary/general/anim/water/3.png"' in text
    # replace its art with an RGB image: colours are matched to the palette the tiles use on the map
    Image.new("RGB", (16, 16), (255, 0, 0)).save(root / "red.png")
    ta.set_frame("General_Water", 3, root / "red.png", "Town")
    _, _, pal, bank, _ = ta._context("Town")
    d = ((pal[bank[8]].astype(int) - (255, 0, 0)) ** 2 * (3, 4, 2)).sum(axis=1)
    want = 1 + int(np.argmin(d[1:]))  # nearest colour (index 0 is the transparent/backdrop colour)
    assert (np.array(Image.open(root / r["png"])) == want).all()
    Image.new("RGB", (8, 8)).save(root / "small.png")
    with pytest.raises(DecompError, match="must be 16x16"):
        ta.set_frame("General_Water", 0, root / "small.png")
    notes = ta.remove_frame("General_Water", 3)
    assert "no longer used" in notes[0]
    assert "Frame3" not in (root / "src/tileset_anims.c").read_text()
    assert len(ta.get("General_Water").frames) == 3


def test_create_animation(ta, root):
    r = ta.create("Town", "Pond", 512 + 4, 4, frames=3, period=10)
    a = ta.get("Town_Pond")
    assert a.tilesets == ["gTileset_Town"] and a.side == "secondary" and a.period == 10
    assert [s.dest for s in a.slots] == [516] and len(a.frames) == 3
    assert all((root / p).exists() for p in r["pngs"])
    assert "InitTilesetAnim_Town" in (root / "src/data/tilesets/headers.h").read_text()
    assert "void InitTilesetAnim_Town(void);" in (root / "include/tileset_anims.h").read_text()
    text = (root / "src/tileset_anims.c").read_text()
    assert "TILE_OFFSET_4BPP(NUM_TILES_IN_PRIMARY + 4)" in text and "static void TilesetAnim_Town(u16);" in text
    # the frames start as the current tiles
    assert np.array_equal(ta.frame_tiles(a, a.frames[0]), ta._context("Town")[0][516:520])
    assert "Town_Pond" in [x["id"] for x in ta.list("Town")]
    # a second one on the same tileset joins the existing callback
    ta.create("Town", "Lamp", 512 + 10, 2, frames=2, period=10)
    cb = ta.functions(open(root / "src/tileset_anims.c").read())["TilesetAnim_Town"]["body"]
    assert "QueueAnimTiles_Town_Pond" in cb and "QueueAnimTiles_Town_Lamp" in cb and "timer % 10 == 1" in cb
    with pytest.raises(DecompError, match="already exists"):
        ta.create("Town", "Pond", 512 + 4, 4)
    with pytest.raises(DecompError, match="outside"):
        ta.create("Town", "Big", 1020, 8)


def test_check(root):
    proj = DecompProject(root)
    ta = proj.maps.tileset_anims
    assert ta.check() == []  # nothing changed
    assert not [m for _, _, m in ta.check(only_changed=False) if "divide" in m]
    ta.set_sequence("General_Water", [0, 1, 2])  # 16 steps per cycle, 3 frames
    msgs = [m for _, _, m in ta.check()]
    assert any("jumps back to frame 0" in m for m in msgs)
    frame(root / "data/tilesets/primary/general/anim/water/1.png", 8, 8, 4)
    proj._changed = None
    msgs = [(lvl, m) for lvl, _, m in ta.check()]
    assert ("error", "General_Water: has 1 tiles but 4 are copied each frame (the game would copy garbage)") in msgs
    rep = proj.check()
    assert any("copy garbage" in i.message for i in rep.issues)


def test_cli_and_mcp(root, tmp_path, capsys):
    assert main(["anim", "list", "-p", str(root), "--map", "Town"]) == 0
    assert "General_Water" in capsys.readouterr().out
    gif = tmp_path / "t.gif"
    assert main(["anim", "preview", "Town", "-o", str(gif), "-p", str(root)]) == 0
    assert Image.open(gif).n_frames > 1
    assert main(["anim", "speed", "General_Water", "12", "-p", str(root)]) == 0
    assert main(["anim", "order", "General_Water", "0,1", "-p", str(root)]) == 0
    assert main(["anim", "new", "Town", "Fountain", "--tile", "0x204", "--count", "2", "-p", str(root)]) == 0
    capsys.readouterr()
    srv = Server(DecompProject(root))
    out = srv.call("list_tileset_anims", {"map": "Town"})
    ids = [a["id"] for a in json.loads(out[0]["text"])]
    assert ids[:2] == ["General_Water", "General_Steam"] and "Town_Fountain" in ids
    out = srv.call("view_tileset_anim", {"id": "General_Water", "map": "Town"})
    assert out[0]["type"] == "image" and json.loads(out[1]["text"])["period"] == 12
    out = srv.call("edit_tileset_anim", {"id": "General_Steam", "speed": 6})
    assert json.loads(out[0]["text"])["period"] == 6
    out = srv.call("add_anim_frame", {"id": "General_Steam"})
    assert json.loads(out[1]["text"])["frame"] == 2


# Needs a built expansion ROM: PAE_TEST_EXPANSION=/path/to/built/pokeemerald-expansion
EXPANSION = os.environ.get("PAE_TEST_EXPANSION")


@pytest.mark.skipif(not EXPANSION, reason="set PAE_TEST_EXPANSION to a built pokeemerald-expansion checkout")
@pytest.mark.parametrize("map_name,x,y", [("Route109", 10, 25), ("Route104", 10, 30)])
def test_preview_matches_game(tmp_path, map_name, x, y):
    """The preview's frame for each animation equals what the game has in VRAM at that moment."""
    from pkmn_asset_editor import playtest as pt
    from pkmn_asset_editor.gfx import decode_tiles
    root = Path(EXPANSION)
    ta = DecompProject(root).maps.tileset_anims
    session = pt.PlaySession(root, cache_dir=tmp_path)
    try:
        session.start(map_name, x, y)
        g = session.game
        anims = [a for a in ta.for_map(map_name) if a.period and a.slots]
        cycle, states = ta._states(map_name, anims)
        slots = [(a, s) for a in anims for s in a.slots]
        checked = 0
        for trial in range(10):
            g.emu.frames(7 + trial)
            c = g.emu.read(g.sym["sPrimaryTilesetAnimCounter"], 2)
            # the copies queued at counter c reach VRAM in the next VBlank
            st = [x for t, x in states if t <= (c - 1) % cycle][-1]
            for (a, s), sym in zip(slots, st):
                if sym is None:
                    continue
                raw = b"".join(g.emu.read(0x06000000 + s.dest * 32 + i * 4, 4).to_bytes(4, "little")
                               for i in range(a.count * 8))
                assert np.array_equal(decode_tiles(raw, 4), ta.frame_tiles(a, sym)[:a.count]), (a.id, c)
                checked += 1
        assert checked > 20
    finally:
        session.close()

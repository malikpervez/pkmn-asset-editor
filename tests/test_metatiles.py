import json

import numpy as np
import pytest
from PIL import Image, ImageDraw

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.mcp import Server


@pytest.fixture()
def proj(tmp_path):
    return DecompProject(decomp_fixture.build(tmp_path / "game"))


def art(path, w=32, h=32):
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.polygon([(0, 14), (w // 2, 0), (w, 14)], fill=(200, 40, 40, 255))
    d.rectangle([3, 14, w - 4, h - 1], fill=(240, 230, 200, 255))
    d.rectangle([w // 2 - 3, h - 10, w // 2 + 2, h - 1], fill=(90, 50, 20, 255))
    im.save(path)
    return path


def test_info_and_behaviors(proj):
    te = proj.maps.tileset_editor
    info = te.info("Town")
    assert info["primary"]["metatiles"] == 32 and info["secondary"]["metatile_limit"] == 512
    assert info["secondary"]["tiles"] == 60 and info["secondary"]["num_tiles_arg"] == 60
    names = te.behaviors("emerald")
    assert names[:3] == ["MB_NORMAL", "MB_SECRET_BASE_WALL", "MB_TALL_GRASS"]
    assert names[0x10] == "MB_WATER" and names[0x11] == "MB_DOOR"


def test_frlg_behaviors_from_defines(proj):
    """A FireRed header lists its behaviours as #defines (MB_FRLG_X 0x..), not an enum: they're read, named like
    Emerald's (water, one-way edges and spin tiles mean the same on both kinds of map), and the header's own
    spelling is still taken when setting one."""
    te = proj.maps.tileset_editor
    (te.root / "include" / "constants" / "metatile_behaviors_frlg.h").write_text(
        "#define MB_FRLG_NORMAL 0x00\n//\n#define MB_FRLG_POND_WATER 0x10\n"
        "#define MB_FRLG_IMPASSABLE_EAST 0x30\n#define MB_FRLG_SPIN_RIGHT 0x54 // spinner\n")
    names = te.behaviors("frlg")
    assert names[0] == "MB_NORMAL" and names[0x10] == "MB_POND_WATER"
    assert names[0x30] == "MB_IMPASSABLE_EAST" and names[0x54] == "MB_SPIN_RIGHT" and names[1] == "0x01"
    assert te._behavior_value("MB_FRLG_SPIN_RIGHT", "frlg") == te._behavior_value("MB_SPIN_RIGHT", "frlg") == 0x54
    assert te.behaviors("emerald")[0x10] == "MB_WATER"  # Emerald's names are kept apart


def test_metatile_roundtrip(proj):
    te = proj.maps.tileset_editor
    mt = te.get_metatile("Town", 512)  # first secondary metatile
    assert mt["tileset"] == "gTileset_Town" and len(mt["entries"]) == 8
    entries = mt["entries"]
    entries[4] = {"tile": 513, "hflip": True, "vflip": False, "palette": 7}
    te.set_metatile("Town", 512, entries, behavior="MB_DOOR", layer_type="COVERED")
    again = te.get_metatile("Town", 512)
    assert again["entries"][4] == {"tile": 513, "hflip": True, "vflip": False, "palette": 7}
    assert again["behavior"] == 0x11 and again["layer_type"] == 1
    with pytest.raises(DecompError):
        te.set_metatile("Town", 512, entries[:3])
    with pytest.raises(DecompError):
        te.set_metatile("Town", 512, behavior="MB_NOPE")


def test_add_metatiles_and_tiles(proj):
    te = proj.maps.tileset_editor
    ids = te.add_metatiles("Town", 2, copy_from=512)
    assert ids == [512 + 32, 512 + 33]
    assert te.get_metatile("Town", ids[0])["entries"] == te.get_metatile("Town", 512)["entries"]
    tiles = te.add_tiles("Town", 3)
    assert tiles == [512 + 60, 512 + 61, 512 + 62]
    graphics = (proj.root / "src/data/tilesets/graphics.h").read_text()
    assert "-num_tiles 63" in graphics  # gbagfx would otherwise drop the new tiles
    te.set_tile("Town", tiles[0], [[5] * 8] * 8)
    assert te.get_tile("Town", tiles[0])[3] == [5] * 8
    assert "src/data/tilesets/graphics.h" in proj.changed_files()
    assert proj.check().ok, proj.check().errors


def test_import_art_dedupes_and_validates(proj, tmp_path):
    te = proj.maps.tileset_editor
    src = art(tmp_path / "house.png")
    before = te.info("Town")["secondary"]
    r = te.import_image("Town", src, base_metatile=1)
    assert (r["w"], r["h"]) == (2, 2) and len(r["ids"]) == 4
    after = te.info("Town")["secondary"]
    assert after["metatiles"] == before["metatiles"] + 4
    assert after["tiles"] > before["tiles"]
    mt = te.get_metatile("Town", r["ids"][0])
    assert mt["layer_type"] == 1  # covered: drawn below the player, ground shows through
    assert mt["entries"][:4] == te.get_metatile("Town", 1)["entries"][:4]
    # importing the same art again reuses everything
    r2 = te.import_image("Town", src, base_metatile=1)
    assert r2["ids"] == r["ids"] and te.info("Town")["secondary"] == after
    # the atlas and map render pick the new metatiles up
    m = proj.maps
    b = m.read_blocks(m.layout_of("Town")).copy()
    b[4:6, 6:8] = np.array(r["ids"]).reshape(2, 2) | (3 << 12)
    m.write_blocks("Town", b)
    assert m.layout_atlas(m.layout_of("Town")).count == 512 + after["metatiles"]
    assert proj.check().ok, proj.check().errors
    # a broken attributes file is reported
    attrs = proj.root / "data/tilesets/secondary/town/metatile_attributes.bin"
    attrs.write_bytes(attrs.read_bytes()[:-2])
    assert any("attribute entries" in e for e in proj.check().errors)


def test_import_art_new_palette(proj, tmp_path):
    te = proj.maps.tileset_editor
    r = te.import_image("Town", art(tmp_path / "h.png", 16, 16), bank=9, new_palette=True)
    assert r["bank"] == 9 and any("palette 9" in n for n in r["notes"])
    assert "data/tilesets/secondary/town/palettes/09.pal" in proj.changed_files()
    with pytest.raises(DecompError):
        te.import_image("Town", art(tmp_path / "h2.png", 16, 16), bank=2, new_palette=True)  # primary bank


def test_cli_and_mcp(proj, tmp_path, capsys):
    root = str(proj.root)
    src = art(tmp_path / "house.png")
    assert main(["map", "import-art", "-p", root, "Town", str(src), "--x", "6", "--y", "4", "--solid"]) == 0
    out = capsys.readouterr().out
    assert "stamp 2x2" in out and "placed at (6, 4)" in out
    b = proj.maps.read_blocks(proj.maps.layout_of("Town"))
    assert (b[4, 6] >> 10) & 3 == 1  # solid
    srv = Server(DecompProject(proj.root))
    call = lambda tool, **a: srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": tool, "arguments": a}})["result"]
    info = json.loads(call("tileset_info", name="Town")["content"][0]["text"])
    assert info["secondary"]["symbol"] == "gTileset_Town"
    assert call("view_tiles", name="Town", bank=7)["content"][0]["type"] == "image"
    r = json.loads(call("import_art_as_metatiles", name="Town", path=str(art(tmp_path / "b.png", 48, 32)),
                        base_metatile=1)["content"][0]["text"])
    assert len(r["rows_for_set_blocks"]) == 2 and len(r["rows_for_set_blocks"][0].split()) == 3
    assert not call("set_blocks", name="Town", x=0, y=5, rows=r["rows_for_set_blocks"])["isError"]
    mt = json.loads(call("get_metatile", name="Town", id=r["ids"][0])["content"][0]["text"])
    assert not call("set_metatile", name="Town", id=r["ids"][0], behavior="MB_DOOR")["isError"]
    assert json.loads(call("validate")["content"][0]["text"])["ok"]


def test_import_art_keeps_each_blocks_ground(proj, tmp_path):
    te = proj.maps.tileset_editor
    src = art(tmp_path / "mill.png")
    r = te.import_image("Town", src, bases=[0, 1, 1, 0])
    for mid, base in zip(r["ids"], [0, 1, 1, 0]):
        assert te.get_metatile("Town", mid)["entries"][:4] == te.get_metatile("Town", base)["entries"][:4]
        assert te.get_metatile("Town", mid)["layer_type"] == 1
    with pytest.raises(DecompError, match="one per block"):
        te.import_image("Town", src, bases=[0, 1])
    # placing with the CLI keeps the ground of the blocks it covers
    m = proj.maps
    b = m.read_blocks(m.layout_of("Town")).copy()
    b[2, 2], b[2, 3], b[3, 2], b[3, 3] = 0, 1, 0, 1
    m.write_blocks("Town", b)
    assert main(["map", "import-art", "Town", str(src), "--x", "2", "--y", "2", "-p", str(proj.root)]) == 0
    m = DecompProject(proj.root).maps
    te = m.tileset_editor
    placed = m.read_blocks(m.layout_of("Town"))
    for (y, x), base in {(2, 2): 0, (2, 3): 1, (3, 2): 0, (3, 3): 1}.items():
        assert te.get_metatile("Town", int(placed[y, x]) & 0x3FF)["entries"][:4] == \
            te.get_metatile("Town", base)["entries"][:4]

import numpy as np
import pytest
from PIL import Image

from pkmn_asset_editor import lz77, patch
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.gfx import decode_palette, load_png, read_jasc, write_jasc
from pkmn_asset_editor.project import Project
from pkmn_asset_editor.rom import ROM_BASE, Rom


@pytest.fixture()
def proj(tmp_path, rom_file):
    return Project.create(rom_file, tmp_path / "proj", log=lambda *a: None)


def reextract(proj, rom_path, tmp_path, name="re"):
    return Project.create(rom_path, tmp_path / name, log=lambda *a: None)


def test_extract_layout(proj):
    assert (proj.root / "graphics/pokemon/001_mon1/front.png").exists()
    assert (proj.root / "graphics/pokemon/001_mon1/normal.pal").exists()
    assert (proj.root / "AGENTS.md").exists()
    a = proj.get("pokemon/150_mon150/front")  # alias of the shared placeholder sprite
    assert a.id == "pokemon/000_none/front"
    with Image.open(proj.path(proj.get("pokemon/001_mon1/front"))) as im:
        assert im.mode == "P" and im.size == (64, 64)
    with Image.open(proj.path(proj.get("battle_anims/sprites/10004/gfx"))) as im:
        assert im.size == (16, 96)  # 3 frames of 16x32 stacked
    with Image.open(proj.path(proj.get("overworld/003/sprite"))) as im:
        assert im.size == (16 * 6, 32)


def test_noop_build_is_identical(proj, rom_file):
    rep = proj.build()
    assert rep.ok and not rep.changed_assets
    assert (proj.root / "build/rom.gba").read_bytes() == rom_file.read_bytes()


def test_touched_but_unchanged(proj):
    p = proj.path(proj.get("pokemon/001_mon1/front"))
    with Image.open(p) as im:
        im.convert("RGBA").save(p)  # same pixels, different encoding
    st = proj.status()
    assert [s["state"] for s in st] == ["touched"]
    rep = proj.build()
    assert rep.ok and not rep.changed_assets


def test_sprite_edit_in_place_and_relocated(proj, tmp_path, synth):
    data, lay = synth
    front = proj.get("pokemon/001_mon1/front")
    p = proj.path(front)
    # simpler art compresses smaller and is written into the old slot
    img = load_png(p)
    idx = np.zeros_like(img.indices)
    idx[8:56, 8:56] = 3
    _save(p, idx, img.palette)
    rep = proj.build()
    assert rep.ok, rep.errors
    assert len(rep.in_place) == 1 and not rep.relocated
    # big edit: random noise compresses badly and must move to free space
    noise = np.random.default_rng(5).integers(0, 16, (64, 64), dtype=np.uint8)
    _save(p, noise, img.palette)
    rep = proj.build()
    assert rep.ok, rep.errors
    assert len(rep.relocated) == 1
    assert rep.rom_size == len(data)  # fits in the existing free space, no expansion
    out = Rom.load(rep.output)
    new = int(rep.relocated[0]["to"], 16)
    # the species table and the extra "code" reference were both repointed
    assert out.u32(lay.front_table + 8) == ROM_BASE + new
    assert out.u32(lay.extra_refs[0]) == ROM_BASE + new
    re = reextract(proj, rep.output, tmp_path)
    got = load_png(re.path(re.get("pokemon/001_mon1/front"))).indices
    assert (got == noise).all()
    pal = decode_palette(lz77.decompress(out.data, out.ptr(lay.pal_table + 8))[0])
    assert pal == proj.original_palettes()["pokemon/001_mon1/normal"]


def test_palette_edit_via_pal_and_png(proj, tmp_path):
    pal_asset = proj.get("pokemon/002_mon2/normal")
    cols = read_jasc(proj.path(pal_asset))
    cols[5] = (248, 0, 0)
    write_jasc(proj.path(pal_asset), cols)
    # also change a colour through the shiny-less PNG palette of mon3
    p3 = proj.path(proj.get("pokemon/003_mon3/front"))
    img = load_png(p3)
    plte = list(img.palette[:16])
    plte[7] = (0, 248, 0)
    _save(p3, img.indices, plte)
    rep = proj.build()
    assert rep.ok, rep.errors
    assert set(rep.changed_assets) == {"pokemon/002_mon2/normal", "pokemon/003_mon3/normal"}
    re = reextract(proj, rep.output, tmp_path)
    assert read_jasc(re.path(re.get("pokemon/002_mon2/normal")))[5] == (255, 0, 0)
    assert read_jasc(re.path(re.get("pokemon/003_mon3/normal")))[7] == (0, 255, 0)


def test_conflicting_palette_edits_are_rejected(proj):
    for which, color in (("front", (248, 0, 0)), ("back", (0, 0, 248))):
        p = proj.path(proj.get(f"pokemon/004_mon4/{which}"))
        img = load_png(p)
        plte = list(img.palette[:16])
        plte[2] = color
        _save(p, img.indices, plte)
    rep = proj.build()
    assert not rep.ok and any("colour 2" in e for e in rep.errors)


def test_import_rgba_and_new_palette(proj, tmp_path):
    art = Image.new("RGBA", (128, 128), (0, 0, 0, 0))
    for y in range(20, 100):
        for x in range(30, 90):
            art.putpixel((x, y), (200, (x * 3) % 256, (y * 2) % 256, 255))
    src = tmp_path / "art.png"
    art.save(src)
    a = proj.get("pokemon/005_mon5/front")
    with pytest.raises(Exception):
        proj.import_image(a, src)  # wrong size without --resize
    notes = proj.import_image(a, src, resize=True, new_palette=True)
    assert any("shared" in n for n in notes)  # back sprite uses the same palette
    with Image.open(proj.path(a)) as im:
        assert im.mode == "P" and im.size == (64, 64)
    rep = proj.build()
    assert rep.ok, rep.errors
    assert {"pokemon/005_mon5/front", "pokemon/005_mon5/normal"} <= set(rep.changed_assets)


def test_rgba_edit_is_mapped_to_palette(proj, tmp_path):
    a = proj.get("items/003_item3/icon")
    p = proj.path(a)
    img = load_png(p)
    rgba = img.rgba.copy()
    rgba[0:3, 0:3] = list(img.palette[9]) + [255]
    Image.fromarray(rgba, "RGBA").save(p)
    rep = proj.build()
    assert rep.ok and rep.changed_assets == ["items/003_item3/icon"]
    re = reextract(proj, rep.output, tmp_path)
    assert (load_png(re.path(re.get(a.id))).indices[0:3, 0:3] == 9).all()


def test_tileset_edit(proj, tmp_path):
    a = proj.get("tilesets/secondary_01/tiles")  # uncompressed tiles: written in place
    p = proj.path(a)
    img = load_png(p)
    idx = img.indices.copy()
    idx[0:8, 0:8] = (idx[0:8, 0:8] & 0xF0) | 0x0E
    _save(p, idx, img.palette)
    rep = proj.build()
    assert rep.ok, rep.errors
    re = reextract(proj, rep.output, tmp_path)
    assert ((load_png(re.path(re.get(a.id))).indices[0:8, 0:8] & 0xF) == 0x0E).all()


def test_bg_retile(proj, tmp_path):
    a = proj.get("battle_anims/backgrounds/02/bg")
    assert a.layout["retile"]
    p = proj.path(a)
    img = load_png(p)
    # new art built from a few distinct 8x8 tiles (some of them flips of each other)
    idx = np.full_like(img.indices, 5)
    idx[0:64, 0:64] = 7
    idx[64:72, 64:69] = 9
    idx[80:88, 83:88] = 9
    _save(p, idx, img.palette)
    rep = proj.build()
    assert rep.ok, rep.errors
    re = reextract(proj, rep.output, tmp_path)
    assert (load_png(re.path(re.get(a.id))).indices == idx).all()


def test_bg_too_many_tiles(proj):
    a = proj.get("battle_anims/backgrounds/03/bg")
    p = proj.path(a)
    img = load_png(p)
    _save(p, np.random.default_rng(3).integers(0, 16, img.indices.shape, dtype=np.uint8), img.palette)
    rep = proj.build()
    assert not rep.ok and "unique 8x8 tiles" in rep.errors[0]


def test_shared_tiles_bg_fixed_mode(proj, tmp_path):
    scene = proj.get("battle_backgrounds/00/scene")
    entry = proj.get("battle_backgrounds/00/entry")
    assert not scene.layout["retile"] and not entry.layout["retile"]
    p = proj.path(entry)
    img = load_png(p)
    idx = img.indices.copy()
    idx[:, :] = (idx & 0xF0) | (idx & 0x0F) ^ 1  # recolour every tile consistently
    _save(p, idx, img.palette)
    rep = proj.build()
    assert rep.ok, rep.errors
    re = reextract(proj, rep.output, tmp_path)
    assert (load_png(re.path(re.get(entry.id))).indices == idx).all()


def test_overworld_edit(proj, tmp_path):
    a = proj.get("overworld/004/sprite")
    p = proj.path(a)
    img = load_png(p)
    idx = img.indices.copy()
    idx[:, 32:64] = 0  # erase the 2nd frame of 32x32 frames
    _save(p, idx, img.palette)
    rep = proj.build()
    assert rep.ok, rep.errors
    re = reextract(proj, rep.output, tmp_path)
    assert (load_png(re.path(re.get(a.id))).indices == idx).all()


def test_wrong_size_is_an_error(proj):
    p = proj.path(proj.get("pokemon/001_mon1/back"))
    Image.new("P", (32, 32)).save(p)
    rep = proj.build()
    assert not rep.ok and "must be 64x64" in rep.errors[0]


def test_ups_patch_reproduces_rom(proj, rom_file):
    p = proj.path(proj.get("pokemon/001_mon1/back"))
    img = load_png(p)
    _save(p, np.random.default_rng(1).integers(0, 16, (64, 64), dtype=np.uint8), img.palette)
    rep = proj.build()
    assert rep.ok
    built = (proj.root / "build/rom.gba").read_bytes()
    ups = (proj.root / "build/rom.ups").read_bytes()
    assert patch.apply_ups(rom_file.read_bytes(), ups) == built


def test_revert_and_relayout(proj):
    a = proj.get("unknown/" + [x for x in proj.assets if x.startswith("unknown/")][0].split("/")[1])
    proj.relayout(a, frame_w=4, frame_h=4)
    with Image.open(proj.path(a)) as im:
        assert im.size == (32, 32)
    p = proj.path(proj.get("pokemon/001_mon1/front"))
    Image.new("P", (64, 64)).save(p)
    proj.revert(proj.get("pokemon/001_mon1/front"))
    assert proj.status() == []


def test_cli_smoke(proj, tmp_path, capsys):
    root = str(proj.root)
    assert main(["list", "-p", root, "-c", "items"]) == 0
    assert main(["describe", "-p", root, "pokemon/001_mon1/front"]) == 0
    assert '"width": 64' in capsys.readouterr().out
    assert main(["sheet", "-p", root, "pokemon", "-o", str(tmp_path / "s.png")]) == 0
    assert main(["render", "-p", root, "pokemon/001_mon1/front", "--palette", "pokemon/001_mon1/shiny",
                 "-o", str(tmp_path / "shiny.png")]) == 0
    assert main(["validate", "-p", root]) == 0
    assert main(["build", "-p", root, "--no-patch"]) == 0
    assert main(["scan", str(proj.root / "original.gba")]) == 0
    assert "pokemon front pics" in capsys.readouterr().out
    drop = tmp_path / "drop" / "items" / "004_item4"
    drop.mkdir(parents=True)
    Image.new("RGBA", (48, 48), (10, 200, 30, 255)).save(drop / "icon.png")
    assert main(["import-dir", "-p", root, str(tmp_path / "drop"), "--resize"]) == 0
    assert [s["id"] for s in proj.status()] == ["items/004_item4/icon"]


def _save(path, indices, palette):
    im = Image.frombytes("P", (indices.shape[1], indices.shape[0]), np.ascontiguousarray(indices, np.uint8).tobytes())
    im.putpalette([v for c in palette for v in c[:3]])
    im.save(path)

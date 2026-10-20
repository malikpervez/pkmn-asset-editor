import os

import numpy as np
import pytest
from PIL import Image

from pkmn_asset_editor import lz77, patch
from pkmn_asset_editor.discovery import discover
from pkmn_asset_editor.gfx import Layout, decode_tiles, image_to_tiles, read_jasc, tiles_to_image, write_jasc
from pkmn_asset_editor.rom import Rom


@pytest.mark.parametrize("data", [b"a", bytes(5000), os.urandom(3000), bytes(range(256)) * 40,
                                  b"ab" * 900 + os.urandom(50)])
def test_lz77_roundtrip(data):
    comp = lz77.compress(data)
    assert len(comp) % 4 == 0
    raw, used = lz77.decompress(comp)
    assert raw == data and used <= len(comp)


def test_lz77_is_vram_safe():
    comp = lz77.compress(bytes(4000))
    pos, flags_left = 4, 0
    raw_len = 0
    while raw_len < 4000:
        flags = comp[pos]
        pos += 1
        for bit in range(8):
            if raw_len >= 4000:
                break
            if flags & (0x80 >> bit):
                disp = (((comp[pos] & 0xF) << 8) | comp[pos + 1]) + 1
                assert disp >= 2
                raw_len += (comp[pos] >> 4) + 3
                pos += 2
            else:
                raw_len += 1
                pos += 1


def test_layout_roundtrip():
    tiles = np.random.default_rng(0).integers(0, 16, (24, 8, 8), dtype=np.uint8)
    for lay in (Layout.frames_of(24, 2, 3, 2), Layout.sheet(24, 5), Layout.frames_of(24, 2, 2, 6)):
        assert (image_to_tiles(tiles_to_image(tiles, lay), lay) == tiles).all()


def test_jasc(tmp_path):
    cols = [(i * 8, 255 - i * 8, 16) for i in range(16)]
    write_jasc(tmp_path / "a.pal", cols)
    assert read_jasc(tmp_path / "a.pal") == cols


def test_patches():
    a = os.urandom(10000)
    b = bytearray(a)
    b[5:9] = b"abcd"
    b[9000] ^= 0xFF
    b = bytes(b) + b"\xFF" * 100
    assert patch.apply_ups(a, patch.make_ups(a, b)) == b
    assert patch.apply_ips(a, patch.make_ips(a, b)) == b


def test_discovery(synth):
    data, lay = synth
    d = discover(Rom(data))
    assert d.mon_pic_tables["front"].offset == lay.front_table
    assert d.mon_pic_tables["back"].offset == lay.back_table
    assert d.mon_palettes.offset == lay.pal_table
    assert d.mon_shiny_palettes.offset == lay.shiny_table
    assert d.species_names[1] == "MON1"
    assert len(d.mon_icons) == 300 and len(d.mon_icon_palettes.entries) == 6
    assert len(d.trainer_pics.entries) == 60 and len(d.trainer_palettes.entries) == 60
    assert len(d.item_icons) == 30 and d.item_names[2] == "ITEM2"
    assert len(d.anim_pics.entries) == 30 and d.anim_frame_dims[10004] == (2, 4)
    assert len(d.anim_backgrounds) == 10 and len(d.battle_backgrounds) == 5
    assert len(d.ow_graphics) == 12 and len(d.ow_graphics_ids) == 12 and len(d.ow_palettes) == 4
    assert sorted(t.offset for t in d.tilesets) == sorted(lay.tilesets)
    assert len(d.tileset_pairs) == 2

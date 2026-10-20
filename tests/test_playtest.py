import json
import os
import struct
from pathlib import Path

import numpy as np
import pytest

import decomp_fixture
from pkmn_asset_editor import playtest as pt


def make_elf(path: Path, symbols: dict[str, tuple[int, int]]) -> None:
    """Write a minimal ELF32 with a .symtab ({name: (value, type)}, type 1=OBJECT 2=FUNC)."""
    strtab = b"\0"
    syms = [b"\0" * 16]
    for name, (value, typ) in symbols.items():
        off = len(strtab)
        strtab += name.encode() + b"\0"
        syms.append(struct.pack("<IIIBBH", off, value, 4, (1 << 4) | typ, 0, 1))
    symtab = b"".join(syms)
    data_off = 52
    sym_off, str_off = data_off, data_off + len(symtab)
    sh_off = str_off + len(strtab)
    sh_off += (-sh_off) % 4
    header = b"\x7fELF" + bytes([1, 1, 1]) + bytes(9)
    header += struct.pack("<HHIIIIIHHHHHH", 2, 40, 1, 0, 0, sh_off, 0, 52, 0, 0, 40, 3, 0)
    sections = [bytes(40),
                struct.pack("<IIIIIIIIII", 0, 2, 0, 0, sym_off, len(symtab), 2, 0, 4, 16),
                struct.pack("<IIIIIIIIII", 0, 3, 0, 0, str_off, len(strtab), 0, 0, 1, 0)]
    blob = header + symtab + strtab
    blob += b"\0" * (sh_off - len(blob))
    path.write_bytes(blob + b"".join(sections))


def test_elf_symbols(tmp_path):
    make_elf(tmp_path / "x.elf", {"gMain": (0x030022C0, 1), "CB2_Overworld": (0x08085A31, 2), "file": (0, 4)})
    syms = pt.elf_symbols(tmp_path / "x.elf")
    assert syms == {"gMain": 0x030022C0, "CB2_Overworld": 0x08085A31}
    with pytest.raises(pt.PlaytestError):
        (tmp_path / "bad.elf").write_bytes(b"nope")
        pt.elf_symbols(tmp_path / "bad.elf")


def test_parse_inputs():
    assert pt.parse_inputs("A") == [(1, 6), (0, 10)]
    assert pt.parse_inputs("UP*32, wait 20, A+B 4") == [(64, 32), (0, 20), (3, 4)]
    assert pt.parse_inputs(["down*16", "start"]) == [(128, 16), (8, 6), (0, 10)]
    with pytest.raises(pt.PlaytestError):
        pt.parse_inputs("JUMP")


def test_map_numbers_and_spawn(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    assert pt.map_numbers(root) == {"Town": (0, 0), "Town_House1": (1, 0)}
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    assert pt.spawn_point(town, None, None) == (2, 2)  # first warp
    assert pt.spawn_point(town, 5, 6) == (5, 6)
    with pytest.raises(pt.PlaytestError, match="no built ROM"):
        pt.find_rom(root)


# A real end-to-end run needs a built expansion ROM: PAE_TEST_EXPANSION=/path/to/built/pokeemerald-expansion
EXPANSION = os.environ.get("PAE_TEST_EXPANSION")


@pytest.mark.skipif(not EXPANSION, reason="set PAE_TEST_EXPANSION to a built pokeemerald-expansion checkout")
def test_play_real_rom(tmp_path):
    session = pt.PlaySession(Path(EXPANSION), cache_dir=tmp_path)
    try:
        first = session.start("PetalburgCity", 17, 11)
        assert first.size == (240, 160)
        final, frames = session.press("UP, wait 10, A, wait 60", record_every=8)
        assert frames and final.size == (240, 160)
        # the sign's textbox makes the bottom of the screen mostly white
        bottom = np.asarray(final.crop((16, 120, 224, 150)).convert("L"))
        assert bottom.mean() > 180
        assert session.where["map"] == "PetalburgCity"
        again = session.start("PetalburgCity_House1", 3, 7)
        assert again.getbbox() is not None
    finally:
        session.close()

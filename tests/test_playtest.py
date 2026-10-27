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


def test_parse_state():
    assert pt.parse_state("FLAG_BADGE01_GET, !flag_hide_x\nVAR_STORY=3\nITEM_POTION*5\n# note\nITEM_ANTIDOTE") == [
        ("flag", "FLAG_BADGE01_GET", 1), ("flag", "FLAG_HIDE_X", 0), ("var", "VAR_STORY", 3),
        ("item", "ITEM_POTION", 5), ("item", "ITEM_ANTIDOTE", 1)]
    assert pt.parse_state(["FLAG_A=false", "VAR_B = 0x10"]) == [("flag", "FLAG_A", 0), ("var", "VAR_B", 16)]
    with pytest.raises(pt.PlaytestError, match="can't read"):
        pt.parse_state("give me a pokemon")


def test_constants_cache(tmp_path, monkeypatch):
    (tmp_path / "include/constants").mkdir(parents=True)
    (tmp_path / "include/constants/flags.h").write_text("#define FLAG_A 0x20\n")
    calls = []

    def fake(self, names, version):
        calls.append(list(names))
        return {n: i for i, n in enumerate(names)}

    monkeypatch.setattr(pt.Constants, "_compile", fake)
    c = pt.Constants(tmp_path, tmp_path / "cache")
    assert c.values(["FLAG_A", "FLAG_B"]) == {"FLAG_A": 0, "FLAG_B": 1}
    assert pt.Constants(tmp_path, tmp_path / "cache").values(["FLAG_B"]) == {"FLAG_B": 1}  # from disk
    assert calls == [["FLAG_A", "FLAG_B"]]


@pytest.mark.skipif(not pt.shutil.which("arm-none-eabi-gcc"), reason="needs arm-none-eabi-gcc")
def test_constants_compiled(tmp_path):
    inc = tmp_path / "include/constants"
    inc.mkdir(parents=True)
    (inc / "flags.h").write_text("#define FLAG_BASE 0x50\n#define FLAG_X (FLAG_BASE + 3)\n")
    (inc / "items.h").write_text("enum { ITEM_NONE, ITEM_POTION = 13, ITEM_SUPER };\n")
    c = pt.Constants(tmp_path, tmp_path / "cache")
    assert c.values(["FLAG_X", "ITEM_SUPER"]) == {"FLAG_X": 0x53, "ITEM_SUPER": 14}
    with pytest.raises(pt.PlaytestError, match="unknown constant.*FLAG_NOPE"):
        c.values(["FLAG_NOPE"])


def test_checkpoints_import_delete(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    s = pt.PlaySession(root, cache_dir=tmp_path / "cache")
    (tmp_path / "game.sav").write_bytes(b"\xff" * 0x20000)
    meta = s.import_save(tmp_path / "game.sav", "before gym")
    assert meta["source"] == "import"
    assert [c["name"] for c in s.checkpoints()] == ["before gym"]
    (tmp_path / "bad.sav").write_bytes(b"\0" * 100)
    with pytest.raises(pt.PlaytestError, match="64 or 128 KiB"):
        s.import_save(tmp_path / "bad.sav", "bad")
    with pytest.raises(pt.PlaytestError, match="names may use"):
        s.import_save(tmp_path / "game.sav", "../escape")
    s.delete_checkpoint("before gym")
    assert s.checkpoints() == []
    with pytest.raises(pt.PlaytestError, match="no checkpoint"):
        s.delete_checkpoint("before gym")
    with pytest.raises(pt.PlaytestError, match="start a play-test"):
        s.save_checkpoint("x")


@pytest.mark.skipif(not EXPANSION, reason="set PAE_TEST_EXPANSION to a built pokeemerald-expansion checkout")
def test_checkpoint_real_rom(tmp_path):
    root = Path(EXPANSION)
    session = pt.PlaySession(root, cache_dir=tmp_path)
    session.checkpoint_dir = tmp_path / "checkpoints"
    try:
        session.start("PetalburgCity", 17, 11, state=["FLAG_BADGE01_GET", "VAR_PETALBURG_GYM_STATE=2", "ITEM_POTION*2"])
        g = session.game
        values = session.constants.values(["VAR_PETALBURG_GYM_STATE", "FLAG_BADGE01_GET"])
        session.save_checkpoint("t", "test")
        session.start(checkpoint="t")
        assert session.location()["map"] == "PetalburgCity"
        assert g is not session.game
        assert session.game.call("VarGet", values["VAR_PETALBURG_GYM_STATE"]) == 2
        assert session.game.call("FlagGet", values["FLAG_BADGE01_GET"]) == 1
        # from a checkpoint, warping elsewhere keeps the progress
        session.start("PetalburgCity_House1", 3, 7, checkpoint="t")
        assert session.location()["map"] == "PetalburgCity_House1"
        assert session.game.call("FlagGet", values["FLAG_BADGE01_GET"]) == 1
    finally:
        session.close()


@pytest.mark.skipif(not EXPANSION, reason="set PAE_TEST_EXPANSION to a built pokeemerald-expansion checkout")
def test_play_shows_flags_and_vars(tmp_path, capsys):
    from pkmn_asset_editor.cli import main
    assert main(["play", "PetalburgCity", "-p", EXPANSION, "--no-build", "--x", "17", "--y", "11",
                 "--state", "FLAG_BADGE01_GET", "--state", "VAR_PETALBURG_GYM_STATE=2", "--inputs", "wait 5",
                 "--show-flag", "FLAG_BADGE01_GET", "--show-var", "VAR_PETALBURG_GYM_STATE",
                 "-o", str(tmp_path / "x.png")]) == 0
    out = capsys.readouterr().out
    assert "flag FLAG_BADGE01_GET = True" in out and "var VAR_PETALBURG_GYM_STATE = 2" in out


def test_flag_and_var_names_must_be_flags_and_vars():
    """A trainer's name is a number too (its ID), and reading it as a flag reads some other flag: refuse it, and point
    at --show-trainer. Region-prefixed names (JOHTO_FLAG_...) and plain numbers are fine."""
    pt._check_names(["FLAG_BADGE01_GET", "JOHTO_FLAG_HIDE_TOTODILEBALL", "0x50", "12"], "FLAG")
    pt._check_names(["VAR_RESULT", "JOHTO_VAR_STARTER_MON"], "VAR")
    with pytest.raises(pt.PlaytestError, match="--show-trainer"):
        pt._check_names(["TRAINER_BURGLAR_LEWIS"], "FLAG")
    with pytest.raises(pt.PlaytestError, match="isn't a variable"):
        pt._check_names(["FLAG_BADGE01_GET"], "VAR")
    with pytest.raises(pt.PlaytestError, match="isn't a flag"):
        pt._check_names(["ITEM_POTION"], "FLAG")


def test_clock_env(monkeypatch):
    monkeypatch.delenv("PAE_CLOCK", raising=False)
    env = pt.clock_env()  # noon (UTC) unless told otherwise: the same tint on every computer
    assert env["PAE_RTC"] == str(pt.CLOCK_DAY + 12 * 3600) and env["TZ"] == "UTC"
    assert pt.clock_env("20:30")["PAE_RTC"] == str(pt.CLOCK_DAY + 20 * 3600 + 30 * 60)
    monkeypatch.setenv("PAE_CLOCK", "wall")
    monkeypatch.setenv("PAE_RTC", "123")
    assert "PAE_RTC" not in pt.clock_env()
    with pytest.raises(pt.PlaytestError, match="time of day"):
        pt.clock_env("25:00")


def test_sessions_have_their_own_scratch_save(tmp_path):
    """Two plays in one project at once (two terminals, a script beside the live server) each play on their own
    scratch save, so neither loads or saves over the other; closing a session removes its own."""
    a, b = pt.PlaySession(tmp_path), pt.PlaySession(tmp_path)
    assert a.scratch != b.scratch and a.scratch.parent == b.scratch.parent == tmp_path / ".pae" / "cache"
    a.scratch.parent.mkdir(parents=True)
    a.scratch.write_bytes(b"a")
    b.scratch.write_bytes(b"b")
    a.close()
    assert not a.scratch.exists() and b.scratch.read_bytes() == b"b"
    given = tmp_path / "mine.sav"
    given.write_bytes(b"x")
    c = pt.PlaySession(tmp_path, scratch=given)
    c.close()
    assert given.exists()  # a scratch save someone passed in is theirs to keep

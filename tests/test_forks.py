"""Things found by testing on a pokehns-expansion FireRed fork: more than one game target, no Quickstart, prebuilt
graphics without a source PNG, file-local stat macros and several wild tables per map."""

from pathlib import Path

from pkmn_asset_editor.decomp import _source_of, default_build_command
from pkmn_asset_editor.gamedata import _label_wild_variants
from pkmn_asset_editor.playtest import quickstart_enabled


def test_gbapal_prefers_the_pal_file(tmp_path):
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "blank.png").write_bytes(b"")
    assert _source_of("g/blank.gbapal", tmp_path) == "g/blank.png"
    (tmp_path / "g" / "blank.pal").write_text("JASC-PAL\n")
    # the Makefile's `%.gbapal: %.pal` rule comes first, so the .pal is what the game uses
    assert _source_of("g/blank.gbapal", tmp_path) == "g/blank.pal"
    assert _source_of("g/blank.4bpp.smol", tmp_path) == "g/blank.png"


def _rom(root: Path, stem: str) -> None:
    (root / f"{stem}.gba").write_bytes(b"")
    (root / f"{stem}.elf").write_bytes(b"")


def test_build_command_follows_the_last_built_target(tmp_path):
    (tmp_path / "Makefile").write_text("ifeq (firered, $(or $(BUILD), $(MAKECMDGOALS)))\n  GAME_VERSION := FIRERED\n"
                                       "endif\nemerald: all\nfirered: all\n")
    assert default_build_command(tmp_path).startswith("make -j")
    _rom(tmp_path, "pokefirered")
    assert default_build_command(tmp_path).startswith("make firered -j")
    (tmp_path / "Makefile").write_text("all:\n")  # no such target: plain make
    assert default_build_command(tmp_path).startswith("make -j")


def test_quickstart_detection(tmp_path):
    assert not quickstart_enabled(tmp_path)  # forks without the config start new games through the intro
    cfg = tmp_path / "include" / "config"
    cfg.mkdir(parents=True)
    (cfg / "quickstart.h").write_text("#define ENABLE_QUICKSTART FALSE\n")
    assert not quickstart_enabled(tmp_path)
    (cfg / "quickstart.h").write_text("#define ENABLE_QUICKSTART TRUE\n")
    assert quickstart_enabled(tmp_path)


def test_wild_tables_are_named_by_what_differs():
    e = [{"base_label": "sRoute1_FireRed"}, {"base_label": "sRoute1_LeafGreen"},
         {"base_label": "sRoute1_FireRed_Night"}]
    _label_wild_variants(e)
    assert [x["variant"] for x in e] == ["FireRed", "LeafGreen", "FireRed Night"]
    one = [{"base_label": "gRoute101"}]
    _label_wild_variants(one)
    assert one[0]["variant"] == ""


def test_stats_from_macros_in_the_species_file():
    from pkmn_asset_editor.gamedata import GameData

    class G(GameData):
        def __init__(self):
            self._defines = {"P_UPDATED_STATS": "9", "GEN_6": "6", "GEN_1": "1"}
            self._local_defines = {"PIKACHU_DEFENSE": "(P_UPDATED_STATS >= GEN_6 ? 40 : 30)",
                                   "PIKACHU_EXP_YIELD": "112", "ALIAS": "PIKACHU_EXP_YIELD"}

    g = G()
    assert g._value("PIKACHU_DEFENSE") == 40
    assert g._value("ALIAS") == 112
    assert g._value("(35)") == 35
    assert g._value("UNKNOWN_MACRO") is None

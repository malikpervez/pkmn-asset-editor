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


def test_party_files_are_the_ones_the_game_builds(tmp_path):
    from pkmn_asset_editor.gamedata import built_party_files
    d = tmp_path / "src" / "data"
    d.mkdir(parents=True)
    for n in ("trainers", "trainers_frlg", "trainers_hns", "battle_partners"):
        (d / f"{n}.party").write_text("")
    (tmp_path / "src" / "data.c").write_text('#include "data/trainers_frlg.h"\n#include "data/trainers_firered_johto.h"\n'
                                             '#include "data/battle_partners.h"\n')
    (tmp_path / "trainer_rules.mk").write_text("src/data/trainers_firered_johto.h: src/data/trainers_hns.h "
                                               "src/data/trainers_hns.party tools/x.py\n")
    # Emerald's trainers.party is not built; the Johto one is, through its make rule
    assert built_party_files(tmp_path) == ["src/data/battle_partners.party", "src/data/trainers_frlg.party",
                                           "src/data/trainers_hns.party"]


def test_second_region_trainers_use_their_game_ids(tmp_path):
    from pkmn_asset_editor.playtest import game_trainer
    c = tmp_path / "include" / "constants"
    c.mkdir(parents=True)
    (c / "johto_trainers.h").write_text("#define JOHTO_TRAINER_ID_BASE 1024\n"
                                        "#define TRAINER_JOHTO_AL_HNS (JOHTO_TRAINER_ID_BASE + 170)\n")
    assert game_trainer(tmp_path, "TRAINER_AL_HNS") == "TRAINER_JOHTO_AL_HNS"
    assert game_trainer(tmp_path, "leader_brock") == "TRAINER_LEADER_BROCK"


def test_removed_layouts_and_maps_are_skipped(tmp_path):
    import json
    from pkmn_asset_editor.decomp import DecompProject
    root = tmp_path
    (root / "Makefile").write_text("all:\n")
    (root / "src").mkdir()
    (root / "graphics").mkdir()
    (root / "data" / "maps" / "Town").mkdir(parents=True)
    (root / "data" / "layouts").mkdir(parents=True)
    (root / "data" / "maps" / "map_groups.json").write_text(json.dumps(
        {"group_order": ["gMapGroup_A"], "gMapGroup_A": ["Town", "GoneTown"]}))
    (root / "data" / "maps" / "Town" / "map.json").write_text(json.dumps(
        {"id": "MAP_TOWN", "name": "Town", "layout": "LAYOUT_TOWN", "region": "REGION_JOHTO"}))
    (root / "data" / "layouts" / "layouts.json").write_text(json.dumps({"layouts": [
        {"id": "LAYOUT_TOWN", "name": "Town_Layout", "width": 1, "height": 1, "primary_tileset": "a",
         "secondary_tileset": "b", "border_filepath": "x", "blockdata_filepath": "y"},
        {"id": "LAYOUT_GONE", "name": "Gone_Layout", "removed": True}]}))
    m = DecompProject(root).maps
    assert list(m.maps) == ["Town"] and m.maps["Town"]["region"] == "REGION_JOHTO"
    assert list(m.layouts) == ["LAYOUT_TOWN"]

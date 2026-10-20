import json
import os
from pathlib import Path

import pytest

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.gamedata import active_lines, const_of
from pkmn_asset_editor.mcp import Server

PARTY = """\
/* Trainers */

=== TRAINER_NONE ===
Name:
Class: Pkmn Trainer 1
Pic: Hiker
Double Battle: No

=== TRAINER_ROXANNE_1 ===
Name: ROXANNE
Class: Leader
Pic: Leader Roxanne
Items: Potion / Potion
Double Battle: No
AI: Basic Trainer

Geodude
Level: 12
IVs: 12 HP / 12 Atk / 12 Def / 12 SpA / 12 SpD / 12 Spe
- Tackle
- Rock Tomb

Rocky (Nosepass) (F) @ Oran Berry
Brave Nature
Level: 15
- Block

=== TRAINER_JOSH ===
Name: JOSH
Class: Youngster
Pic: Youngster
Double Battle: No

SPECIES_GEODUDE
Level: 10
"""

SPECIES = """\
const struct SpeciesInfo gSpeciesInfoGen3[] =
{
#if P_FAMILY_MUDKIP
    [SPECIES_MUDKIP] =
    {
        .baseHP        = 50,
        .baseAttack    = 70,
        .baseDefense   = 50,
        .baseSpeed     = 40,
        .baseSpAttack  = 50,
        .baseSpDefense = 50,
        .types = MON_TYPES(TYPE_WATER),
        .catchRate = 45,
    #if P_UPDATED_EXP_YIELDS >= GEN_5
        .expYield = 62,
    #else
        .expYield = 65,
    #endif
        .abilities = { ABILITY_TORRENT, ABILITY_NONE, ABILITY_DAMP },
        .speciesName = _("Mudkip"),
        .levelUpLearnset = sMudkipLevelUpLearnset,
        .evolutions = EVOLUTION({EVO_LEVEL, 16, SPECIES_MARSHTOMP}),
    },
    [SPECIES_MARSHTOMP] =
    {
        .baseHP        = P_UPDATED_STATS >= GEN_5 ? 70 : 60,
        .baseAttack    = 85,
        .baseDefense   = 70,
        .baseSpeed     = 50,
        .baseSpAttack  = 60,
        .baseSpDefense = 70,
        .types = MON_TYPES(TYPE_WATER, TYPE_GROUND),
        .catchRate = 45,
        .expYield = 142,
        .abilities = { ABILITY_TORRENT, ABILITY_NONE, ABILITY_DAMP },
        .speciesName = _("Marshtomp"),
        .levelUpLearnset = sMarshtompLevelUpLearnset,
        .evolutions = NULL,
    },
#endif
#if P_FAMILY_MISSINGNO
    [SPECIES_GEODUDE] =
    {
        .baseHP        = 1,
    },
#endif
    [SPECIES_GEODUDE] =
    {
        .baseHP        = 40,
        .types = MON_TYPES(TYPE_ROCK, TYPE_GROUND),
        .speciesName = _("Geodude"),
    },
    [SPECIES_NOSEPASS] = { .speciesName = _("Nosepass"), },
    [SPECIES_ZIGZAGOON] = { .speciesName = _("Zigzagoon"), },
};
"""

LEARN = """\
static const struct LevelUpMove sMudkipLevelUpLearnset[] = {
    LEVEL_UP_MOVE( 1, MOVE_TACKLE),
    LEVEL_UP_MOVE( 6, MOVE_WATER_GUN),
    LEVEL_UP_END
};

static const struct LevelUpMove sMarshtompLevelUpLearnset[] = {
    LEVEL_UP_MOVE( 1, MOVE_MUD_SHOT),
    LEVEL_UP_END
};
"""


@pytest.fixture()
def root(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    (root / "src/data/trainers.party").write_text(PARTY)
    (root / "src/data/pokemon/species_info").mkdir(parents=True, exist_ok=True)
    (root / "src/data/pokemon/species_info/gen_3_families.h").write_text(SPECIES)
    (root / "src/data/pokemon/level_up_learnsets").mkdir(parents=True, exist_ok=True)
    (root / "src/data/pokemon/level_up_learnsets/gen_9.h").write_text(LEARN)
    (root / "include/config/pokemon.h").write_text(
        "#define P_FAMILY_MUDKIP TRUE\n#define P_FAMILY_MISSINGNO FALSE\n#define P_UPDATED_EXP_YIELDS GEN_LATEST\n"
        "#define P_UPDATED_STATS GEN_LATEST\n#define P_LVL_UP_LEARNSETS GEN_LATEST\n")
    (root / "include/config/general.h").write_text("#define GEN_3 2\n#define GEN_5 4\n#define GEN_9 8\n"
                                                    "#define GEN_LATEST GEN_9\n")
    (root / "include/constants/species.h").write_text(
        "enum __attribute__((packed)) Species {\n    SPECIES_NONE,\n    SPECIES_GEODUDE,\n    SPECIES_NOSEPASS,\n"
        "    SPECIES_MUDKIP,\n    SPECIES_MARSHTOMP,\n    SPECIES_ZIGZAGOON,\n};\n")
    (root / "include/constants/moves.h").write_text(
        "enum Move {\n    MOVE_NONE,\n    MOVE_TACKLE,\n    MOVE_ROCK_TOMB,\n    MOVE_BLOCK,\n    MOVE_WATER_GUN,\n"
        "    MOVE_MUD_SHOT,\n    MOVE_SELF_DESTRUCT,\n};\n")
    (root / "include/constants/items.h").write_text("enum Item {\n    ITEM_NONE,\n    ITEM_POTION,\n    ITEM_ORAN_BERRY,\n};\n")
    (root / "include/constants/pokemon.h").write_text(
        "enum Type {\n    TYPE_NONE,\n    TYPE_WATER,\n    TYPE_GROUND,\n    TYPE_ROCK,\n    TYPE_FIRE,\n};\n"
        "enum Ability {\n    ABILITY_NONE,\n    ABILITY_TORRENT,\n    ABILITY_DAMP,\n    ABILITY_SWIFT_SWIM,\n};\n")
    (root / "src/data/moves_info.h").write_text(
        '[MOVE_TACKLE] = { .name = COMPOUND_STRING("Tackle"), },\n'
        '[MOVE_SELF_DESTRUCT] = { .name = COMPOUND_STRING("Self-Destruct"), },\n')
    wild = {"wild_encounter_groups": [{"label": "gWildMonHeaders", "for_maps": True, "fields": [
        {"type": "land_mons", "encounter_rates": [60, 40]},
        {"type": "water_mons", "encounter_rates": [100]},
        {"type": "rock_smash_mons", "encounter_rates": [100]},
        {"type": "fishing_mons", "encounter_rates": [70, 30], "groups": {"old_rod": [0, 1]}}],
        "encounters": [{"map": "MAP_TOWN", "base_label": "gTown", "land_mons": {"encounter_rate": 20, "mons": [
            {"min_level": 2, "max_level": 3, "species": "SPECIES_ZIGZAGOON"},
            {"min_level": 4, "max_level": 4, "species": "SPECIES_GEODUDE"}]}}]}]}
    (root / "src/data/wild_encounters.json").write_text(json.dumps(wild, indent=2) + "\n")
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "data"], cwd=root, check=True)
    return root


@pytest.fixture()
def gd(root):
    return DecompProject(root).gamedata


def test_const_of_and_preprocessor():
    assert const_of("MOVE", "Rock Tomb") == "MOVE_ROCK_TOMB"
    assert const_of("SPECIES", "Farfetch'd") == "SPECIES_FARFETCHD"
    assert const_of("SPECIES", "SPECIES_MR_MIME") == "SPECIES_MR_MIME"
    text = "#if A >= 2\nyes\n#elif B\nno\n#else\nno2\n#endif\n#ifdef C\nc\n#endif"
    assert active_lines(text, {"A": "3", "B": "1"}) == [False, True, False, False, False, False, False, False, False,
                                                       False]
    assert active_lines(text, {"A": "1", "C": ""})[5] and active_lines(text, {"A": "1", "C": ""})[8]


def test_trainers(gd, root):
    names = [t["id"] for t in gd.trainers()]
    assert names == ["TRAINER_NONE", "TRAINER_ROXANNE_1", "TRAINER_JOSH"]
    t = gd.get_trainer("roxanne_1")
    assert {f["key"]: f["value"] for f in t["fields"]}["Items"] == "Potion / Potion"
    geo, nose = t["mons"]
    assert geo["species"] == "SPECIES_GEODUDE" and geo["level"] == 12 and geo["moves"] == ["MOVE_TACKLE", "MOVE_ROCK_TOMB"]
    assert nose["nickname"] == "Rocky" and nose["gender"] == "F" and nose["item"] == "ITEM_ORAN_BERRY"
    assert nose["extra"] == ["Brave Nature"] and nose["order"] == ["extra", "level"]
    # writing it back unchanged keeps the file byte for byte
    before = (root / "src/data/trainers.party").read_text()
    gd.set_trainer("TRAINER_ROXANNE_1", {"mons": t["mons"], "fields": t["fields"]})
    assert (root / "src/data/trainers.party").read_text() == before
    # edits: names or constants both work; the file keeps its own spelling
    mons = t["mons"]
    mons[0]["level"] = 14
    mons[0]["moves"] = ["Tackle", "Self-Destruct"]
    mons.append({"species": "Mudkip", "level": 9, "item": "ITEM_POTION"})
    gd.set_trainer("TRAINER_ROXANNE_1", {"mons": mons, "fields": [{"key": "Items", "value": "Potion"},
                                                                  *[f for f in t["fields"] if f["key"] != "Items"]]})
    text = (root / "src/data/trainers.party").read_text()
    assert "Level: 14\nIVs: 12 HP" in text and "- Self-Destruct" in text and "Mudkip @ Potion\nLevel: 9" in text
    assert "Items: Potion\n" in text and text.count("=== TRAINER_JOSH ===") == 1
    assert "\n\n\n" not in text
    assert gd.get_trainer("TRAINER_JOSH")["mons"][0]["species"] == "SPECIES_GEODUDE"
    with pytest.raises(DecompError, match="unknown species SPECIES_PIKACHU"):
        gd.set_trainer("TRAINER_JOSH", {"mons": [{"species": "Pikachu", "level": 5}]})
    with pytest.raises(DecompError, match="at most 6"):
        gd.set_trainer("TRAINER_JOSH", {"mons": [{"species": "Geodude"}] * 7})
    with pytest.raises(DecompError, match="level must be 1-100"):
        gd.set_trainer("TRAINER_JOSH", {"mons": [{"species": "Geodude", "level": 101}]})
    with pytest.raises(DecompError, match="no trainer"):
        gd.get_trainer("TRAINER_NOBODY")


def test_wild(gd, root):
    w = gd.wild("Town")
    land = w["entries"][0]["kinds"]["land_mons"]
    assert land["encounter_rate"] == 20 and land["slot_rates"] == [60, 40]
    assert [m["species"] for m in land["mons"]] == ["SPECIES_ZIGZAGOON", "SPECIES_GEODUDE"]
    assert w["slot_counts"] == {"land_mons": 2, "water_mons": 1, "rock_smash_mons": 1, "fishing_mons": 2}
    gd.set_wild("Town", "water_mons", 4, [{"species": "Mudkip", "min_level": 5, "max_level": 10}])
    data = json.loads((root / "src/data/wild_encounters.json").read_text())
    e = data["wild_encounter_groups"][0]["encounters"][0]
    assert e["water_mons"] == {"encounter_rate": 4, "mons": [{"min_level": 5, "max_level": 10, "species": "SPECIES_MUDKIP"}]}
    with pytest.raises(DecompError, match="exactly 2 slots"):
        gd.set_wild("Town", "land_mons", 20, [{"species": "Mudkip", "min_level": 5}])
    with pytest.raises(DecompError, match="min ≤ max"):
        gd.set_wild("Town", "water_mons", 4, [{"species": "Mudkip", "min_level": 9, "max_level": 3}])
    gd.set_wild("Town", "water_mons", 0, [])
    assert "water_mons" not in json.loads((root / "src/data/wild_encounters.json").read_text())[
        "wild_encounter_groups"][0]["encounters"][0]
    # a map without wild Pokémon gets an entry
    gd.set_wild("Town_House1", "rock_smash_mons", 50, [{"species": "Geodude", "min_level": 3, "max_level": 6}])
    assert gd.wild("Town_House1")["entries"][0]["kinds"]["rock_smash_mons"]["encounter_rate"] == 50


def test_species(gd, root):
    d = gd.get_species("SPECIES_MUDKIP")
    assert d["name"] == "Mudkip" and d["types"] == ["TYPE_WATER"] and d["expYield"] == 62
    assert d["stats"]["baseSpeed"] == 40 and d["total"] == 310
    assert d["abilities"] == ["ABILITY_TORRENT", "ABILITY_NONE", "ABILITY_DAMP"]
    assert d["evolutions"] == [{"method": "EVO_LEVEL", "param": "16", "target": "SPECIES_MARSHTOMP",
                                "target_name": "Marshtomp", "extra": []}]
    assert [m["move"] for m in d["learnset"]] == ["MOVE_TACKLE", "MOVE_WATER_GUN"]
    assert gd.get_species("marshtomp")["stats"]["baseHP"] == 70  # P_UPDATED_STATS >= GEN_5 ? 70 : 60
    assert gd.get_species("SPECIES_GEODUDE")["stats"]["baseHP"] == 40  # the #if FALSE copy is ignored
    gd.set_species("SPECIES_MUDKIP", {"baseSpeed": 60, "expYield": 70, "types": ["TYPE_WATER", "TYPE_FIRE"],
                                      "abilities": ["ABILITY_TORRENT", "", "ABILITY_SWIFT_SWIM"],
                                      "learnset": [{"level": 1, "move": "MOVE_TACKLE"}, {"level": 3, "move": "Mud Shot"}],
                                      "evolutions": [{"method": "EVO_LEVEL", "param": 12, "target": "SPECIES_MARSHTOMP"}]})
    text = (root / "src/data/pokemon/species_info/gen_3_families.h").read_text()
    assert ".baseSpeed     = 60," in text and ".expYield = 70," in text and ".expYield = 65," in text
    assert "MON_TYPES(TYPE_WATER, TYPE_FIRE)" in text
    assert "{ ABILITY_TORRENT, ABILITY_NONE, ABILITY_SWIFT_SWIM }" in text
    assert "EVOLUTION({EVO_LEVEL, 12, SPECIES_MARSHTOMP})" in text
    learn = (root / "src/data/pokemon/level_up_learnsets/gen_9.h").read_text()
    assert "LEVEL_UP_MOVE( 3, MOVE_MUD_SHOT),\n    LEVEL_UP_END" in learn and "sMarshtompLevelUpLearnset" in learn
    gd.set_species("SPECIES_MARSHTOMP", {"baseHP": 75})
    assert ".baseHP        = 75," in (root / "src/data/pokemon/species_info/gen_3_families.h").read_text()
    with pytest.raises(DecompError, match="0-255"):
        gd.set_species("SPECIES_MUDKIP", {"baseHP": 300})
    with pytest.raises(DecompError, match="give 1 or 2 types"):
        gd.set_species("SPECIES_MUDKIP", {"types": ["TYPE_WATER", "TYPE_FIRE", "TYPE_ROCK"]})
    with pytest.raises(DecompError, match="unknown move"):
        gd.set_species("SPECIES_MUDKIP", {"learnset": [{"level": 1, "move": "MOVE_HYPER_BEAM"}]})


def test_check(root):
    proj = DecompProject(root)
    text = (root / "src/data/trainers.party").read_text().replace("SPECIES_GEODUDE\nLevel: 10", "Pikachu\nLevel: 10")
    (root / "src/data/trainers.party").write_text(text)
    w = json.loads((root / "src/data/wild_encounters.json").read_text())
    w["wild_encounter_groups"][0]["encounters"][0]["land_mons"]["mons"].pop()
    (root / "src/data/wild_encounters.json").write_text(json.dumps(w))
    msgs = [i.message for i in proj.check().issues]
    assert any("TRAINER_JOSH: Pokémon 1: unknown species SPECIES_PIKACHU" in m for m in msgs)
    assert any("land_mons: 1 slots, needs 2" in m for m in msgs)


def test_cli_and_mcp(root, capsys):
    assert main(["trainer", "show", "ROXANNE_1", "-p", str(root)]) == 0
    assert "Nosepass lv15 @ Oran Berry: Block" in capsys.readouterr().out
    assert main(["trainer", "set", "TRAINER_JOSH", "--mon", "Geodude lv11 @Potion : Tackle, Rock Tomb",
                 "--mon", "Zigzagoon lv9", "--field", "Name=JOSHUA", "-p", str(root)]) == 0
    out = capsys.readouterr().out
    assert "Name: JOSHUA" in out and "Geodude lv11 @ Potion: Tackle, Rock Tomb" in out
    assert main(["wild", "set", "Town", "land", "Mudkip:3-5", "Geodude:4", "--rate", "25", "-p", str(root)]) == 0
    assert "Grass & caves (rate 25): Mudkip 3-5 (60%), Geodude 4-4 (40%)" in capsys.readouterr().out
    assert main(["species", "set", "mudkip", "--spe", "55", "--learn", "1:Tackle", "-p", str(root)]) == 0
    assert "Speed 55" in capsys.readouterr().out
    srv = Server(DecompProject(root))
    r = json.loads(srv.call("get_trainer", {"id": "TRAINER_ROXANNE_1"})[0]["text"])
    assert len(r["mons"]) == 2
    r = json.loads(srv.call("set_wild", {"map": "Town", "kind": "rock_smash_mons", "encounter_rate": 10,
                                         "mons": [{"species": "SPECIES_GEODUDE", "min_level": 2, "max_level": 4}]})[0]["text"])
    assert r["entries"][0]["kinds"]["rock_smash_mons"]["encounter_rate"] == 10
    r = json.loads(srv.call("set_species", {"id": "SPECIES_MUDKIP", "changes": {"catchRate": 90}})[0]["text"])
    assert r["catchRate"] == 90


# Needs a built expansion ROM: PAE_TEST_EXPANSION=/path/to/built/pokeemerald-expansion
EXPANSION = os.environ.get("PAE_TEST_EXPANSION")


@pytest.mark.skipif(not EXPANSION, reason="set PAE_TEST_EXPANSION to a built pokeemerald-expansion checkout")
def test_battles_real_rom(tmp_path):
    from pkmn_asset_editor import playtest as pt
    root = Path(EXPANSION)
    s = pt.PlaySession(root, cache_dir=tmp_path)
    try:
        s.start("Route101", 10, 10, state=["CLEAR_TEAM", "SPECIES_MUDKIP lv10"])
        s.start_battle(species="SPECIES_POOCHYENA", level=2, frames=500)
        assert s.in_battle()
        for i in range(30):  # run away
            if s.game.in_overworld():
                break
            s.press("B, wait 30")
            if i > 3:
                s.press("RIGHT, wait 4, DOWN, wait 4, A, wait 60")
        s.press("wait 120")
        assert s.game.in_overworld()
        before = s.location()["y"]
        s.press("UP*32, wait 30")
        assert s.location()["y"] < before  # the player can walk again
        s.start("RustboroCity", 27, 20, state=["CLEAR_TEAM", "SPECIES_MARSHTOMP lv20"])
        s.start_battle(trainer="TRAINER_ROXANNE_1", frames=600)
        assert s.in_battle()
        with pytest.raises(pt.PlaytestError, match="overworld"):
            s.start_battle(trainer="TRAINER_ROXANNE_1")
    finally:
        s.close()

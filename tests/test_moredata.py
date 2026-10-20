import json
import subprocess

import pytest

from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.mcp import Server
from test_gamedata import root  # noqa: F401 - the trainer/species fixture this builds on

ITEMS = """\
const struct ItemInfo gItemsInfo[] =
{
    [ITEM_NONE] = { .name = ITEM_NAME("????????"), },
    [ITEM_POTION] =
    {
        .name = ITEM_NAME("Potion"),
        .price = (I_PRICE >= GEN_7) ? 200 : 300,
        .holdEffectParam = 20,
        .description = COMPOUND_STRING(
            "Restores the HP of\\n"
            "a Pokémon by\\n"
        #if I_HEALTH_RECOVERY >= GEN_7
            "20 points."),
        #else
            "10 points."),
        #endif
        .pocket = POCKET_ITEMS,
        .iconPic = gItemIcon_Potion,
    },
    [ITEM_ORAN_BERRY] =
    {
        .name = ITEM_NAME("Oran Berry"),
        .price = 80,
        .description = COMPOUND_STRING(
            "A hold item that\\n"
            "restores 10 HP in\\n"
            "battle."),
        .pocket = POCKET_BERRIES,
    },
    [ITEM_TM_TACKLE] =
    {
        .name = ITEM_NAME("TM01"),
        .price = 3000,
        .description = COMPOUND_STRING(
            "An old move."),
        .pocket = POCKET_TM_HM,
    },
    [ITEM_TM02] =
    {
        .name = ITEM_NAME("TM02"),
        .price = 3000,
        .description = COMPOUND_STRING("?"),
        .pocket = POCKET_TM_HM,
    },
};
"""

MOVES = """\
const struct MoveInfo gMovesInfo[MOVES_COUNT_ALL] =
{
    [MOVE_TACKLE] =
    {
        .name = COMPOUND_STRING("Tackle"),
        .description = COMPOUND_STRING(
            "Charges the foe with a full-\\n"
            "body tackle."),
    #if B_UPDATED_MOVE_DATA >= GEN_5
        .power = 40,
    #else
        .power = 35,
    #endif
        .effect = EFFECT_HIT,
        .type = TYPE_WATER,
        .accuracy = B_UPDATED_MOVE_DATA >= GEN_5 ? 100 : 95,
        .pp = 35,
        .priority = 0,
        .category = DAMAGE_CATEGORY_PHYSICAL,
        .makesContact = TRUE,
    },
    [MOVE_WATER_GUN] =
    {
        .name = COMPOUND_STRING("Water Gun"),
        .description = COMPOUND_STRING(
            "Squirts water to attack."),
        .power = 40,
        .type = TYPE_WATER,
        .accuracy = 100,
        .pp = 25,
        .priority = 0,
        .category = DAMAGE_CATEGORY_SPECIAL,
    },
    [MOVE_MUD_SHOT] = { .name = COMPOUND_STRING("Mud Shot"), .description = COMPOUND_STRING("Hurls mud."), },
    [MOVE_ROCK_TOMB] = { .name = COMPOUND_STRING("Rock Tomb"), .description = COMPOUND_STRING("Drops rocks."), },
};
"""

TMS = """\
#ifndef GUARD_CONSTANTS_TMS_HMS_H
#define GUARD_CONSTANTS_TMS_HMS_H

#define FOREACH_TM(F) \\
    F(TACKLE)

#define FOREACH_HM(F) \\
    F(WATER_GUN)

#endif
"""

MART = """
Town_EventScript_Clerk::
\tlock
\tpokemart Town_Pokemart
\trelease
\tend

\t.align 2
Town_Pokemart:
\t.2byte ITEM_POTION
\t.2byte ITEM_ORAN_BERRY
\tpokemartlistend

Town_EventScript_TmBall::
\tfinditem ITEM_TM_TACKLE
\tend
"""


@pytest.fixture()
def gd(root):  # noqa: F811
    (root / "src/data/items.h").write_text(ITEMS)
    (root / "src/data/moves_info.h").write_text(MOVES)
    (root / "include/constants/tms_hms.h").write_text(TMS)
    (root / "include/constants/items.h").write_text(
        "enum Item {\n    ITEM_NONE,\n    ITEM_POTION,\n    ITEM_ORAN_BERRY,\n    ITEM_TM01,\n    ITEM_TM02,\n"
        "    ITEM_HM01,\n};\nenum Pocket {\n    POCKET_ITEMS,\n    POCKET_BERRIES,\n    POCKET_TM_HM,\n};\n")
    (root / "include/config/item.h").write_text("#define I_PRICE GEN_LATEST\n#define I_HEALTH_RECOVERY GEN_LATEST\n")
    (root / "include/config/battle.h").write_text("#define B_UPDATED_MOVE_DATA GEN_LATEST\n")
    cfg = root / "include/config/pokemon.h"
    cfg.write_text(cfg.read_text() + "#define P_LEARNSET_HELPER_TEACHABLE TRUE\n")
    (root / "include/config/general.h").write_text("#define GEN_3 2\n#define GEN_5 4\n#define GEN_7 6\n#define GEN_9 8\n"
                                                    "#define GEN_LATEST GEN_9\n")
    (root / "include/constants/global.h").write_text("#define POKEMON_NAME_LENGTH 12\n#define ITEM_NAME_LENGTH 20\n"
                                                     "#define MOVE_NAME_LENGTH 16\n")
    sp = root / "src/data/pokemon/species_info/gen_3_families.h"
    sp.write_text(sp.read_text().replace(
        '        .speciesName = _("Mudkip"),\n',
        '        .speciesName = _("Mudkip"),\n        .categoryName = _("Mud Fish"),\n        .height = 4,\n'
        '        .weight = 76,\n        .description = COMPOUND_STRING(\n            "The fin on its head\\n"\n'
        '            "senses the water."),\n        .teachableLearnset = sMudkipTeachableLearnset,\n'
        '        .eggMoveLearnset = sMudkipEggMoveLearnset,\n'))
    (root / "src/data/pokemon/all_learnables.json").write_text(json.dumps({"MUDKIP": ["MOVE_TACKLE"]}, indent=2))
    (root / "src/data/pokemon/special_movesets.json").write_text(json.dumps(
        {"universalMoves": [], "signatureTeachables": [], "extraTutors": []}))
    (root / "src/data/pokemon/egg_moves.h").write_text(
        "static const u16 sMudkipEggMoveLearnset[] = {\n    MOVE_MUD_SHOT,\n    MOVE_UNAVAILABLE,\n};\n")
    s = root / "data/maps/Town/scripts.inc"
    s.write_text(s.read_text() + MART)
    (root / "data/maps/Town_House1/scripts.inc").write_text(
        (root / "data/maps/Town_House1/scripts.inc").read_text()
        + "\nTown_House1_EventScript_Tutor::\n\tsetvar VAR_0x8005, MOVE_ROCK_TOMB\n\tspecial ChooseMonForMoveTutor\n\tend\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "more"], cwd=root, check=True)
    return DecompProject(root).gamedata


def test_items(gd):
    assert {i["id"] for i in gd.items()} == {"ITEM_POTION", "ITEM_ORAN_BERRY", "ITEM_TM_TACKLE", "ITEM_TM02"}
    p = gd.get_item("Potion")
    assert p["price"] == 200 and p["description"] == "Restores the HP of\na Pokémon by\n20 points."
    assert p["pocket"] == "POCKET_ITEMS" and p["sold_in"][0]["label"] == "Town_Pokemart"
    p = gd.set_item("ITEM_POTION", {"name": "Big Potion", "price": 250,
                                    "description": "Restores the HP of\na Pokémon by\n25 points."})
    assert p["name"] == "Big Potion" and p["price"] == 250 and p["description"].endswith("25 points.")
    raw = (gd.root / "src/data/items.h").read_text()
    assert "#if I_HEALTH_RECOVERY" not in raw and '"25 points."),\n        .pocket' in raw  # the #if block is replaced whole
    assert gd.get_item("ITEM_ORAN_BERRY")["price"] == 80  # the next item is untouched
    with pytest.raises(DecompError, match="at most 20"):
        gd.set_item("ITEM_POTION", {"name": "A really long potion name"})
    with pytest.raises(DecompError, match="unknown pocket"):
        gd.set_item("ITEM_POTION", {"pocket": "POCKET_NOPE"})


def test_moves(gd):
    m = gd.get_move("Tackle")
    assert (m["power"], m["accuracy"], m["pp"], m["tm"]) == (40, 100, 35, "TM01")
    assert m["learned_by"] == [{"species": "SPECIES_MUDKIP", "name": "Mudkip", "level": 1}]
    m = gd.set_move("MOVE_TACKLE", {"power": 50, "accuracy": 90, "type": "TYPE_ROCK", "description": "A tackle."})
    assert (m["power"], m["accuracy"], m["type"], m["description"]) == (50, 90, "TYPE_ROCK", "A tackle.")
    raw = (gd.root / "src/data/moves_info.h").read_text()
    assert ".power = 50,\n    #else\n        .power = 35," in raw  # only the compiled branch changes
    with pytest.raises(DecompError, match="power must be"):
        gd.set_move("MOVE_TACKLE", {"power": 300})
    with pytest.raises(DecompError, match="unknown type"):
        gd.set_move("MOVE_TACKLE", {"type": "TYPE_LASER"})


def test_tms_tutors_and_teachables(gd):
    t = gd.tms()
    assert [x["move"] for x in t["tms"]] == ["MOVE_TACKLE"] and t["max_tms"] == 2 and t["max_hms"] == 1
    with pytest.raises(DecompError, match="TM and an HM"):
        gd.set_tms(tms=["MOVE_WATER_GUN"])
    with pytest.raises(DecompError, match="2 TM items"):
        gd.set_tms(tms=["MOVE_TACKLE", "MOVE_MUD_SHOT", "MOVE_ROCK_TOMB"])
    gd.set_tms(tms=["MOVE_MUD_SHOT", "MOVE_ROCK_TOMB"])
    assert "F(MUD_SHOT) \\\n    F(ROCK_TOMB)\n" in (gd.root / "include/constants/tms_hms.h").read_text()
    items = (gd.root / "src/data/items.h").read_text()
    assert "[ITEM_TM_MUD_SHOT] =" in items and "[ITEM_TM_ROCK_TOMB] =" in items and "ITEM_TM_TACKLE" not in items
    assert gd.get_item("ITEM_TM_MUD_SHOT")["description"] == "Hurls mud."  # the move's own description
    assert "finditem ITEM_TM_MUD_SHOT" in (gd.root / "data/maps/Town/scripts.inc").read_text()
    assert [x["move"] for x in gd.tutors()] == ["MOVE_ROCK_TOMB"]
    te = gd.teachable("SPECIES_MUDKIP")
    assert te["key"] == "MUDKIP" and [(r["move"], r["how"], r["can"]) for r in te["moves"]] == [
        ("MOVE_MUD_SHOT", "TM01", False), ("MOVE_ROCK_TOMB", "TM02", False), ("MOVE_WATER_GUN", "HM01", False)]
    gd.set_teachable("SPECIES_MUDKIP", {"MOVE_MUD_SHOT": True, "MOVE_TACKLE": False})
    assert json.loads((gd.root / "src/data/pokemon/all_learnables.json").read_text()) == {"MUDKIP": ["MOVE_MUD_SHOT"]}
    assert gd.teachable("SPECIES_MUDKIP")["moves"][0]["can"]


def test_egg_moves_marts_and_dex(gd):
    assert [m["move"] for m in gd.egg_moves("SPECIES_MUDKIP")["moves"]] == ["MOVE_MUD_SHOT"]
    gd.set_egg_moves("SPECIES_MUDKIP", ["Rock Tomb", "MOVE_TACKLE"])
    assert [m["move"] for m in gd.egg_moves("SPECIES_MUDKIP")["moves"]] == ["MOVE_ROCK_TOMB", "MOVE_TACKLE"]
    mart = gd.marts()[0]
    assert mart["items"] == ["ITEM_POTION", "ITEM_ORAN_BERRY"] and mart["clerk"] == "Town_EventScript_Clerk"
    gd.set_mart("Town_Pokemart", ["Oran Berry"])
    text = (gd.root / "data/maps/Town/scripts.inc").read_text()
    assert "Town_Pokemart:\n\t.2byte ITEM_ORAN_BERRY\n\tpokemartlistend" in text
    with pytest.raises(DecompError, match="at least one"):
        gd.set_mart("Town_Pokemart", [])
    sp = gd.get_species("SPECIES_MUDKIP")
    assert sp["species_name"] == "Mudkip" and sp["dex"]["category"] == "Mud Fish" and sp["dex"]["height"] == 4
    sp = gd.set_species("SPECIES_MUDKIP", {"name": "Mudkipz", "category": "Mudfish", "weight": 80,
                                           "description": "Line one.\nLine two."})
    assert sp["species_name"] == "Mudkipz" and sp["dex"] == dict(sp["dex"], weight=80, description="Line one.\nLine two.")
    assert sp["stats"]["baseHP"] == 50  # other fields untouched
    with pytest.raises(DecompError, match="at most 12"):
        gd.set_species("SPECIES_MUDKIP", {"name": "Mudkipmudkipmudkip"})


def test_cli_mcp_and_checks(gd, capsys):
    root = str(gd.root)
    assert main(["item", "show", "potion", "-p", root]) == 0
    assert "200" in capsys.readouterr().out
    assert main(["item", "set", "potion", "--price", "300", "-p", root]) == 0
    assert main(["move", "set", "tackle", "--power", "55", "-p", root]) == 0
    assert main(["tm", "list", "-p", root]) == 0
    assert "TM01" in capsys.readouterr().out
    assert main(["mart", "set", "Town_Pokemart", "Potion", "Oran Berry", "-p", root]) == 0
    srv = Server(DecompProject(gd.root))
    assert json.loads(srv.call("get_item", {"item": "ITEM_POTION"})[0]["text"])["price"] == 300
    assert json.loads(srv.call("get_move", {"move": "MOVE_TACKLE"})[0]["text"])["power"] == 55
    srv.call("set_mart", {"label": "Town_Pokemart", "items": ["ITEM_POTION"]})
    assert json.loads(srv.call("list_marts", {})[0]["text"])["marts"][0]["items"] == ["ITEM_POTION"]
    # a mart item that doesn't exist is reported by Check for problems
    s = gd.root / "data/maps/Town/scripts.inc"
    s.write_text(s.read_text().replace("Town_Pokemart:\n\t.2byte ITEM_POTION", "Town_Pokemart:\n\t.2byte ITEM_NOPE"))
    msgs = [i.message for i in DecompProject(gd.root).check().issues]
    assert any("ITEM_NOPE" in m for m in msgs)


def test_difficulty(gd, capsys):
    root = gd.root
    st = DecompProject(root).maps.story
    st.add_chapter("Home")
    st.set_chapter("Town", 0)
    s = root / "data/maps/Town/scripts.inc"
    s.write_text(s.read_text() + "\nTown_EventScript_Josh::\n\ttrainerbattle_single TRAINER_JOSH, Town_Text_Hero, Town_Text_Hero\n"
                 "\tend\nTown_EventScript_Roxanne::\n\ttrainerbattle_single TRAINER_ROXANNE_1, Town_Text_Hero, Town_Text_Hero\n\tend\n")
    d = DecompProject(root).gamedata.difficulty()
    c = d["chapters"][0]
    assert {t["id"] for t in c["trainers"]} == {"TRAINER_JOSH", "TRAINER_ROXANNE_1"}
    assert c["boss"]["id"] == "TRAINER_ROXANNE_1" and c["boss"]["ace"] == 15
    assert c["wild"] == {"min": 2, "max": 4, "maps": ["Town"]}  # grass only
    assert d["flags"] == []
    p = root / "src/data/trainers.party"
    p.write_text(p.read_text().replace("SPECIES_GEODUDE\nLevel: 10", "SPECIES_GEODUDE\nLevel: 30"))
    d = DecompProject(root).gamedata.difficulty()
    assert [f["kind"] for f in d["flags"]] == ["stronger than the boss"] and d["flags"][0]["trainer"] == "TRAINER_JOSH"
    assert main(["difficulty", "-p", str(root)]) == 0
    assert "stronger than this chapter's boss ROXANNE" in capsys.readouterr().out

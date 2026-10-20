import json
import os
from pathlib import Path

import pytest

import decomp_fixture
from pkmn_asset_editor import playtest as pt
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.mcp import Server

ITEMS_H = """\
const struct Item gItemsInfo[] =
{
    [ITEM_POTION] =
    {
        .name = ITEM_NAME("Potion"),
        .pocket = POCKET_ITEMS,
    },
    [ITEM_HM_CUT] =
    {
        .name = ITEM_NAME("HM01"),
        .importance = 1,
        .pocket = POCKET_TM_HM,
    },
    [ITEM_PARCEL] =
    {
        .name = ITEM_NAME("Parcel"),
        .pocket = POCKET_KEY_ITEMS,
    },
    [ITEM_BIKE] =
    {
        .name = COMPOUND_STRING("Bike"),
        .importance = 1,
        .pocket = POCKET_KEY_ITEMS,
    },
};
"""


@pytest.fixture()
def root(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    (root / "src/data/items.h").write_text(ITEMS_H)
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["object_events"].append({"graphics_id": "OBJ_EVENT_GFX_ITEM_BALL", "x": 6, "y": 6, "elevation": 3,
                                  "movement_type": "MOVEMENT_TYPE_FACE_DOWN", "movement_range_x": 0,
                                  "movement_range_y": 0, "trainer_type": "TRAINER_TYPE_NONE",
                                  "trainer_sight_or_berry_tree_id": "ITEM_POTION",
                                  "script": "Common_EventScript_FindItem", "flag": "FLAG_ITEM_TOWN_POTION"})
    town["bg_events"].append({"type": "hidden_item", "x": 1, "y": 1, "elevation": 3, "item": "ITEM_POTION",
                              "quantity": 2, "flag": "FLAG_HIDDEN_ITEM_TOWN_POTION"})
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    with open(root / "data/maps/Town_House1/scripts.inc", "a") as f:
        f.write("\nTown_House1_EventScript_Gift::\n\tgoto_if_set FLAG_RECEIVED_CUT, Town_House1_EventScript_Done\n"
                "\tgiveitem ITEM_HM_CUT\n\tsetflag FLAG_RECEIVED_CUT\n\tgiveitem ITEM_PARCEL\n\tsetflag FLAG_GOT_PARCEL\n"
                "\trelease\n\tend\n")
    # a second town, where the parcel is handed over and a bike is given
    groups = json.loads((root / "data/maps/map_groups.json").read_text())
    groups["gMapGroup_Towns"].append("City")
    (root / "data/maps/map_groups.json").write_text(json.dumps(groups))
    (root / "data/maps/City").mkdir()
    city = dict(town, id="MAP_CITY", name="City", object_events=[], bg_events=[])
    (root / "data/maps/City/map.json").write_text(json.dumps(city))
    (root / "data/maps/City/scripts.inc").write_text(
        "City_EventScript_Deliver::\n\tsetvar VAR_0x8004, ITEM_PARCEL\n"
        "\tcall Common_EventScript_PlayerHandedOverTheItem\n\tgiveitem ITEM_BIKE\n\tsetflag FLAG_RECEIVED_BIKE\n\tend\n")
    return root


@pytest.fixture()
def story(root):
    return DecompProject(root).maps.story


def test_parse_team_lines():
    assert pt.parse_state("SPECIES_MUDKIP lv20 ITEM_ORAN_BERRY MOVE_TACKLE move_water_gun") == [
        ("mon", "SPECIES_MUDKIP", {"level": 20, "item": "ITEM_ORAN_BERRY", "moves": ["MOVE_TACKLE", "MOVE_WATER_GUN"]})]
    assert pt.parse_state(["CLEAR_TEAM", "SPECIES_PIKACHU", "SPECIES_EEVEE level 7"]) == [
        ("clear_team", "", 0), ("mon", "SPECIES_PIKACHU", {"level": 5, "item": None, "moves": []}),
        ("mon", "SPECIES_EEVEE", {"level": 7, "item": None, "moves": []})]
    with pytest.raises(pt.PlaytestError, match="at most 4 moves"):
        pt.parse_state("SPECIES_MEW MOVE_A MOVE_B MOVE_C MOVE_D MOVE_E")
    with pytest.raises(pt.PlaytestError, match="level must be 1-100"):
        pt.parse_state("SPECIES_MEW lv101")
    with pytest.raises(pt.PlaytestError, match="Example"):
        pt.parse_state("SPECIES_MEW banana")


def test_items_info(story):
    info = story.items_info()
    assert info["ITEM_POTION"] == {"name": "Potion", "pocket": "POCKET_ITEMS", "needed": False}
    assert info["ITEM_HM_CUT"]["needed"] and info["ITEM_HM_CUT"]["name"] == "HM01 Cut"
    assert info["ITEM_PARCEL"]["needed"]  # key items pocket
    assert info["ITEM_BIKE"]["name"] == "Bike"


def test_chapters_and_catch_up(story, root):
    # a game whose maps aren't Emerald's starts with one empty chapter
    assert story.chapters() == [{"name": "Chapter 1", "maps": [], "flags": []}]
    assert story.add_chapter("Home town", flags=["FLAG_BADGE01_GET"]) == 0
    assert story.add_chapter("The city") == 1
    story.set_chapter("Town", 0)
    story.set_chapter("City", 1)
    assert story.chapter_of("Town_House1") == 0  # inside maps follow their town
    assert story.unplaced_maps() == []
    sources = story.item_sources()
    kinds = sorted((s.kind, s.item, s.qty, s.flag) for s in sources)
    assert kinds == [("gift", "ITEM_BIKE", 1, "FLAG_RECEIVED_BIKE"), ("gift", "ITEM_HM_CUT", 1, "FLAG_RECEIVED_CUT"),
                     ("gift", "ITEM_PARCEL", 1, "FLAG_GOT_PARCEL"),
                     ("hidden item", "ITEM_POTION", 2, "FLAG_HIDDEN_ITEM_TOWN_POTION"),
                     ("item ball", "ITEM_POTION", 1, "FLAG_ITEM_TOWN_POTION")]

    r = story.catch_up(0, extras=False)
    assert sorted(i["item"] for i in r["items"]) == ["ITEM_HM_CUT", "ITEM_PARCEL"]
    assert r["flags"] == ["FLAG_BADGE01_GET"]
    assert r["state"] == ["ITEM_HM_CUT*1", "ITEM_PARCEL*1", "FLAG_BADGE01_GET", "FLAG_RECEIVED_CUT", "FLAG_GOT_PARCEL"]
    r = story.catch_up(0, extras=True)
    assert "ITEM_POTION*3" in r["state"] and "FLAG_ITEM_TOWN_POTION" in r["state"]
    # by the end of the city chapter the parcel has been handed over, and the bike was given
    r = story.catch_up(1)
    assert sorted({i["item"] for i in r["items"]}) == ["ITEM_BIKE", "ITEM_HM_CUT", "ITEM_POTION"]
    # unticked items are left out
    skip = {i["key"] for i in r["items"] if i["item"] == "ITEM_BIKE"}
    assert "ITEM_BIKE*1" not in story.catch_up(1, skip=skip)["state"]
    lines = story.play_state(0, team=["SPECIES_MUDKIP lv5"], clear_team=True, state="VAR_X=2")
    assert lines[-3:] == ["CLEAR_TEAM", "SPECIES_MUDKIP lv5", "VAR_X=2"]
    with pytest.raises(DecompError, match="no chapter 9"):
        story.catch_up(8)
    with pytest.raises(DecompError, match="through a parent map"):
        story.set_chapter("Town_House1", None)
    assert json.loads((root / ".pae/story.json").read_text())["chapters"][1]["maps"] == ["City"]


def test_emerald_default_chapters(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    groups = json.loads((root / "data/maps/map_groups.json").read_text())
    groups["gMapGroup_Towns"] += ["RustboroCity", "RustboroCity_Gym"]
    (root / "data/maps/map_groups.json").write_text(json.dumps(groups))
    for name in ("RustboroCity", "RustboroCity_Gym"):
        (root / "data/maps" / name).mkdir()
        (root / "data/maps" / name / "map.json").write_text(json.dumps({"id": "MAP_X", "layout": "LAYOUT_TOWN"}))
    st = DecompProject(root).maps.story
    ch = st.chapters()
    rustboro = next(i for i, c in enumerate(ch) if c["maps"] == ["RustboroCity"])
    assert ch[rustboro]["flags"] == ["FLAG_BADGE01_GET"] and st.chapter_of("RustboroCity_Gym") == rustboro
    assert "Town" in st.unplaced_maps()


def test_cli_and_mcp(story, root, capsys):
    story.add_chapter("Home town")
    story.set_chapter("Town", 0)
    assert main(["story", "list", "-p", str(root)]) == 0
    out = capsys.readouterr().out
    assert "1. Home town" in out and "Town" in out
    assert main(["story", "items", "1", "--extras", "-p", str(root)]) == 0
    out = capsys.readouterr().out
    assert "needed  HM01 Cut" in out and "extra   Potion" in out
    assert main(["story", "add", "Postgame", "-p", str(root)]) == 0
    assert main(["story", "move", "City", "2", "-p", str(root)]) == 0
    assert "City -> Postgame" in capsys.readouterr().out
    srv = Server(DecompProject(root))
    r = json.loads(srv.call("list_story_chapters", {})[0]["text"])
    assert [c["number"] for c in r["chapters"]] == [1, 2]
    items = json.loads(srv.call("story_items", {"chapter": 1})[0]["text"])["items"]
    assert {i["item"] for i in items} == {"ITEM_HM_CUT", "ITEM_PARCEL"}
    assert json.loads(srv.call("set_map_chapter", {"map": "City", "chapter": 0})[0]["text"]) == {"ok": True}


# Needs a built expansion ROM: PAE_TEST_EXPANSION=/path/to/built/pokeemerald-expansion
EXPANSION = os.environ.get("PAE_TEST_EXPANSION")


@pytest.mark.skipif(not EXPANSION, reason="set PAE_TEST_EXPANSION to a built pokeemerald-expansion checkout")
def test_catch_up_and_team_real_rom(tmp_path):
    root = Path(EXPANSION)
    st = DecompProject(root).maps.story
    session = pt.PlaySession(root, cache_dir=tmp_path)
    try:
        lines = st.play_state(8, team=["SPECIES_MARSHTOMP lv28 ITEM_MYSTIC_WATER MOVE_MUD_SHOT MOVE_WATER_GUN",
                                       "SPECIES_ZIGZAGOON"], clear_team=True)
        session.start("MauvilleCity", 20, 10, state=lines)
        g = session.game
        v = session.constants.values(["FLAG_BADGE03_GET", "FLAG_RECEIVED_HM_CUT", "ITEM_HM_CUT", "ITEM_LETTER",
                                      "SPECIES_MARSHTOMP", "ITEM_MYSTIC_WATER", "MOVE_MUD_SHOT", "MOVE_WATER_GUN"])
        assert g.call("FlagGet", v["FLAG_BADGE03_GET"]) == 1 and g.call("FlagGet", v["FLAG_RECEIVED_HM_CUT"]) == 1
        assert g.call("CheckBagHasItem", v["ITEM_HM_CUT"], 1) == 1
        assert g.call("CheckBagHasItem", v["ITEM_LETTER"], 1) == 0  # delivered to Steven by then
        team = session.team()
        assert [m["species"] for m in team][:1] == [v["SPECIES_MARSHTOMP"]] and len(team) == 2
        assert team[0]["level"] == 28 and team[0]["item"] == v["ITEM_MYSTIC_WATER"]
        assert team[0]["moves"] == [v["MOVE_MUD_SHOT"], v["MOVE_WATER_GUN"], 0, 0]
    finally:
        session.close()

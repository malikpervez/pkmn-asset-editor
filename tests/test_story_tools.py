"""Tools for writing story scenes: prefixed story state in tests, team checks, new trainers, wrapped pages,
local_id clashes."""
import json

import pytest

from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.playtest import PlaytestError, parse_state
from test_gamedata import gd, root  # noqa: F401 - fixtures with a trainers.party


def test_state_lines_with_a_prefix_and_calls():
    assert parse_state(["JOHTO_FLAG_HIDE_BALL", "!JOHTO_FLAG_X", "JOHTO_VAR_LABSTATE=1", "CALL Region_Init",
                        "call Other()"]) == [
        ("flag", "JOHTO_FLAG_HIDE_BALL", 1), ("flag", "JOHTO_FLAG_X", 0), ("var", "JOHTO_VAR_LABSTATE", 1),
        ("call", "Region_Init", 0), ("call", "Other", 0)]
    with pytest.raises(PlaytestError):
        parse_state(["CALL"])


def test_new_trainer_gets_the_next_number(gd, root):  # noqa: F811
    c = root / "include" / "constants"
    (c / "opponents.h").write_text("#define TRAINER_NONE      0\n#define TRAINER_ROXANNE_1 2\n"
                                   "#define TRAINER_JOSH      1\n\n#define TRAINERS_COUNT 3\n#define MAX_TRAINERS_COUNT 4\n")
    (c / "region_trainers.h").write_text("#define REGION_TRAINER_COUNT 3\n"
                                         "#define TRAINER_R_ROXANNE_1 (BASE + 2)\n")
    t = gd.new_trainer("TRAINER_GOLD", "TRAINER_JOSH", {"mons": [{"species": "Mudkip", "level": 5}]})
    assert t["mons"][0]["species"] == "SPECIES_MUDKIP" and t["mons"][0]["level"] == 5
    assert {f["key"]: f["value"] for f in t["fields"]} == \
        {f["key"]: f["value"] for f in gd.get_trainer("TRAINER_JOSH")["fields"]}
    # the highest number (ROXANNE_1 = 2), not the last line, decides the new one
    assert "#define TRAINER_ROXANNE_1 2\n#define TRAINER_GOLD      3\n" in (c / "opponents.h").read_text()
    assert "#define TRAINERS_COUNT 4\n#define MAX_TRAINERS_COUNT 4" in (c / "opponents.h").read_text()
    alias = (c / "region_trainers.h").read_text()
    assert "#define TRAINER_R_GOLD (BASE + 3)\n" in alias and "REGION_TRAINER_COUNT 4" in alias
    party = (root / "src/data/trainers.party").read_text()
    assert party.count("=== TRAINER_GOLD ===") == 1 and party.rstrip().endswith("Mudkip\nLevel: 5")
    with pytest.raises(DecompError, match="already a trainer"):
        gd.new_trainer("TRAINER_GOLD", "TRAINER_JOSH")
    with pytest.raises(DecompError, match="no room"):
        gd.new_trainer("TRAINER_KRIS", "TRAINER_JOSH")


def test_builder_wraps_each_page(root):  # noqa: F811
    from pkmn_asset_editor import eventbuilder as eb
    wrap = DecompProject(root).textbox.wrap
    text = eb.generate("Town_EventScript_Gold", "trigger", [
        {"type": "say", "text": "GOLD: Hi! You must be {PLAYER}, the champion everyone talks about!\\pThen I'll take "
                                "this one!"}], wrap)
    body = text.split("Town_Text_Gold1:\n", 1)[1]
    assert '\\p"' in body and body.count('.string "') >= 3  # a page break kept, and the first page wrapped
    # a text with its own \n is kept as written
    own = eb.generate("Town_EventScript_Own", "npc", [{"type": "say", "text": "A\\nB"}], wrap)
    assert '.string "A\\nB$"' in own


def test_local_id_clash_with_another_map(root):  # noqa: F811
    (root / "data/maps/removed_maps.json").write_text(json.dumps(
        {"OldTown": {"id": "MAP_OLD_TOWN", "event_ids": {"LOCALID_TOWN_HERO": 5}}}))
    m = DecompProject(root).maps
    msgs = [msg for lvl, msg in m.validate_map_data("Town", m.get_map("Town")) if lvl == "error"]
    assert any("LOCALID_TOWN_HERO is also a different object of OldTown (removed)" in x for x in msgs)
    (root / "data/maps/removed_maps.json").write_text(json.dumps(
        {"OldTown": {"id": "MAP_OLD_TOWN", "event_ids": {"LOCALID_TOWN_HERO": 1}}}))  # same number: fine
    m = DecompProject(root).maps
    assert not [x for lvl, x in m.validate_map_data("Town", m.get_map("Town")) if "local_id" in x]

import json

import pytest

from pkmn_asset_editor import eventbuilder as eb
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.mcp import Server
from test_text_history_flow import root  # noqa: F401 - the fixture with a charmap and font

BLOCKS = [
    {"type": "say", "text": "Welcome! This town is always sunny, they say."},
    {"type": "if_flag", "flag": "FLAG_TEST", "on": True,
     "then": [{"type": "say", "text": "Enjoy the potion!"}],
     "else": [{"type": "ask", "text": "Want a present?",
               "yes": [{"type": "give_item", "item": "ITEM_POTION", "amount": 2},
                       {"type": "flag", "flag": "FLAG_TEST", "on": True}],
               "no": [{"type": "say", "text": "Maybe next time."}]}]},
    {"type": "move", "who": "player", "steps": ["!", "up", "up", "face_down"]},
    {"type": "move", "who": "LOCALID_TOWN_HERO", "steps": ["face_player"]},
    {"type": "wait", "frames": 20},
    {"type": "custom", "line": "special DoSomething"},
]


def test_generate_and_parse(root):  # noqa: F811
    wrap = DecompProject(root).textbox.wrap
    text = eb.generate("Town_EventScript_Greeter", "npc", BLOCKS, wrap, taken={"Town_Text_Greeter1"})
    assert "Town_EventScript_Greeter::\n\tlock\n\tfaceplayer\n\tmsgbox Town_Text_Greeter2, MSGBOX_DEFAULT\n" in text
    assert "\tgoto_if_unset FLAG_TEST, Town_EventScript_Greeter_Branch1\n" in text
    assert "\tgoto_if_eq VAR_RESULT, NO, Town_EventScript_Greeter_Branch2\n\tgiveitem ITEM_POTION, 2\n" in text
    assert "\tapplymovement LOCALID_PLAYER, Town_Movement_Greeter1\n\twaitmovement 0\n" in text
    assert "Town_Movement_Greeter1:\n\temote_exclamation_mark\n\twalk_up\n\twalk_up\n\tface_down\n\tstep_end\n" in text
    assert "\tspecial DoSomething\n\trelease\n\tend\n" in text and "Town_Text_Greeter1:" not in text
    sign = eb.generate("Town_EventScript_Sign", "sign", [{"type": "say", "text": "TOWN"}], wrap)
    assert "msgbox Town_Text_Sign1, MSGBOX_SIGN\n\tend" in sign and "lockall" not in sign
    trainer = eb.generate("Town_EventScript_Kid", "npc", [{"type": "trainer", "trainer": "TRAINER_JOSH", "intro": "Hi!",
                                                         "defeat": "Oh no!"}, {"type": "say", "text": "Good battle."}], wrap)
    assert "Town_EventScript_Kid::\n\ttrainerbattle_single TRAINER_JOSH, Town_Text_Kid1, Town_Text_Kid2\n" in trainer
    with pytest.raises(DecompError, match="unknown block"):
        eb.generate("X", "npc", [{"type": "dance"}], wrap)


def test_save_and_read_back(root):  # noqa: F811
    proj = DecompProject(root)
    m = proj.maps
    r = eb.save(m, "Town", "Town_EventScript_Greeter", "npc", BLOCKS)
    assert r["problems"] == [] and len(r["texts"]) == 4
    assert eb.parse(m.read_script("Town"), "Town_EventScript_Greeter") == {"blocks": BLOCKS, "kind": "npc", "built": True}
    # saving again replaces it (no second copy)
    eb.save(m, "Town", "Town_EventScript_Greeter", "npc", BLOCKS[:1])
    script = m.read_script("Town")
    assert script.count("Town_EventScript_Greeter::") == 1 and "Town_Movement_Greeter1" not in script
    # a hand-written script is read back as blocks, and saving it replaces only its code
    hero = eb.parse(script, "Town_EventScript_Hero")
    assert hero == {"blocks": [{"type": "say", "text": "Hi!"}], "kind": "npc", "built": False}
    eb.save(m, "Town", "Town_EventScript_Hero", "npc", hero["blocks"] + [{"type": "heal"}])
    script = m.read_script("Town")
    assert "Town_Text_Hero:" in script and "special HealPlayerParty" in script
    assert script.count("Town_EventScript_Hero::") == 1
    msgs = [i.message for i in DecompProject(root).check().issues if i.level == "error"]
    assert msgs == []


def test_server_and_mcp(root):  # noqa: F811
    srv = Server(DecompProject(root))
    r = json.loads(srv.call("build_script", {"map": "Town", "label": "Town_EventScript_New", "kind": "trigger",
                                             "blocks": [{"type": "say", "text": "Stop right there!"}]})[0]["text"])
    assert "lockall" in r["script"] and r["problems"] == []
    back = json.loads(srv.call("get_blocks_script", {"map": "Town", "label": "Town_EventScript_New"})[0]["text"])
    assert back["kind"] == "trigger" and back["built"]

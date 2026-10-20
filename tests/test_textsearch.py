import json

from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompProject
from pkmn_asset_editor.mcp import Server
from test_text_history_flow import root  # noqa: F401 - fixture with a charmap and font

SCRIPT = """
Town_Text_Sign:
\t.string "PETALBURG CITY\\n"
\t.string "Where people mingle with nature.$"

Town_Text_Long:
\t.string "Welcome to Petalburg!\\p"
\t.string "This box stays as it is.$"

Town_Text_Split:
\t.string "Go to PETALBURG\\n"
\t.string "CITY now.$"
"""


def setup(root):
    p = root / "data/maps/Town/scripts.inc"
    p.write_text(p.read_text() + SCRIPT)
    (root / "src/data/region_map").mkdir(parents=True, exist_ok=True)
    (root / "src/data/region_map/region_map_sections.json").write_text(json.dumps(
        {"map_sections": [{"id": "MAPSEC_PETALBURG_CITY", "name": "PETALBURG CITY", "x": 1}]}, indent=2) + "\n")
    (root / "src/data/trainers.party").write_text("=== TRAINER_PETAL ===\nName: PETALBURG\nClass: Hiker\n\nGeodude\nLevel: 5\n")
    return DecompProject(root).textsearch


def test_find(root):  # noqa: F811
    ts = setup(root)
    r = ts.search("Petalburg City")
    assert {x["label"] for x in r["results"]} == {"Town_Text_Sign", "Town_Text_Split", "MAPSEC_PETALBURG_CITY"}
    split = next(x for x in r["results"] if x["label"] == "Town_Text_Split")
    assert split["text"] == "Go to PETALBURG\nCITY now."  # found across the line break
    assert len(ts.search("Petalburg")["results"]) == 5  # + Town_Text_Long and the trainer name
    assert ts.search("petalburg", match_case=True)["results"] == []
    assert ts.search("n", whole_word=True)["results"] == []  # never the n of \n


def test_replace(root, capsys):  # noqa: F811
    ts = setup(root)
    d = ts.replace("Petalburg City", "Sunny Town", dry_run=True)
    assert len(d["changes"]) == 3 and d["files"] and "PETALBURG" in (root / "data/maps/Town/scripts.inc").read_text()
    ts.replace("Petalburg City", "Sunny Town")
    text = (root / "data/maps/Town/scripts.inc").read_text()
    assert 'Town_Text_Sign:\n\t.string "SUNNY TOWN\\n"\n\t.string "Where people mingle with nature.$"' in text
    assert 'Town_Text_Split:\n\t.string "Go to SUNNY\\n"\n\t.string "TOWN now.$"' in text  # the break stays put
    places = json.loads((root / "src/data/region_map/region_map_sections.json").read_text())
    assert places["map_sections"][0]["name"] == "SUNNY TOWN"
    # a longer name that no longer fits: only that box is re-wrapped
    d = ts.replace("Petalburg", "Supercalifragilisticexpialidocious Village Of Many Long Names", only=[
        "data/maps/Town/scripts.inc:Town_Text_Long"])
    c = d["changes"][0]
    assert c["rewrapped"] and c["problems"] == [] and c["after"].endswith("\n\nThis box stays as it is.")
    t = DecompProject(root).textbox
    assert all(not x.problems for x in t.texts((root / "data/maps/Town/scripts.inc").read_text())
               if x.label == "Town_Text_Long")
    assert "PETALBURG" in (root / "src/data/trainers.party").read_text()  # not in `only`
    assert main(["text", "replace", "Petalburg", "Rocky", "--word", "-p", str(root)]) == 0
    assert "Name: ROCKY" in (root / "src/data/trainers.party").read_text()
    assert "changed 1 text(s)" in capsys.readouterr().out
    srv = Server(DecompProject(root))
    assert json.loads(srv.call("find_text", {"query": "Rocky"})[0]["text"])["total"] == 1

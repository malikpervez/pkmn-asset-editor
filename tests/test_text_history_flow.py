import json
import subprocess

import numpy as np
import pytest
from PIL import Image

import decomp_fixture
from pkmn_asset_editor.cli import main
from pkmn_asset_editor.decomp import DecompError, DecompProject
from pkmn_asset_editor.history import History
from pkmn_asset_editor.mcp import Server
from pkmn_asset_editor.server import make_handler, _State
from pkmn_asset_editor.textbox import BOX_WIDTH, TextBox

CHARMAP = "' ' = 00\n'!' = AB\n'?' = AC\n'.' = AD\n',' = B8\n" + "".join(
    f"'{c}' = {0xBB + i:02X}\n" for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")) + "".join(
    f"'{c}' = {0xD5 + i:02X}\n" for i, c in enumerate("abcdefghijklmnopqrstuvwxyz")) + \
    "'$' = FF\nPLAYER = FD 01\nCOLOR = FC 01\nBLUE = 08\n"


@pytest.fixture()
def root(tmp_path):
    root = decomp_fixture.build(tmp_path / "game")
    (root / "charmap.txt").write_text(CHARMAP)
    (root / "src/fonts.c").write_text("const u8 gFontNormalLatinGlyphWidths[] = {\n" + ", ".join(["6"] * 256) + "\n};\n")
    font = np.zeros((512, 256), np.uint8)
    for code in range(256):
        y, x = (code // 16) * 16, (code % 16) * 16
        font[y + 2:y + 12, x:x + 5] = 1
        font[y + 12, x:x + 5] = 2
    (root / "graphics/fonts").mkdir(parents=True)
    im = Image.fromarray(font, "P")
    im.putpalette([144, 200, 255, 56, 56, 56, 216, 216, 216, 255, 255, 255] + [0] * 756)
    im.save(root / "graphics/fonts/latin_normal.png")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "font"], cwd=root, check=True)
    return root


def test_text_layout(root):
    tb = TextBox(root)
    p = tb.layout("T", r"Hello {PLAYER}!\nHow are you?\pBye.$")
    assert p.problems == []
    assert [b.lines for b in p.boxes] == [["Hello BRENDAN!", "How are you?"], ["Bye."]]
    assert p.boxes[0].widths[0] == 14 * 6
    p = tb.layout("T", r"{COLOR BLUE}One\lTwo\nThree$")
    assert [b.lines for b in p.boxes] == [["One"], ["One", "Two", "Three"]]
    assert any("3rd line starts after \"Two\"" in x for x in p.problems)
    long = "word " * 10
    p = tb.layout("T", long + "$")
    assert any(f"{len(long) * 6} pixels wide" in x for x in p.problems)
    p = tb.layout("T", "Café ✓")
    msgs = " ".join(p.problems)
    assert "'é' can't be shown" in msgs and "doesn't end with $" in msgs
    assert any("{NOPE}" in x for x in tb.layout("T", "{NOPE}$").problems)
    img = tb.render(tb.layout("T", r"Hi!\nThere$"), scale=1)
    assert img.width == BOX_WIDTH + 16 and img.height > 40


def test_wrap_and_new_script(root):
    tb = TextBox(root)
    lines = tb.wrap("one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen\n\nBye")
    assert lines[0].endswith("\\n") and lines[1].endswith("\\l") and lines[-2].endswith("\\p") and lines[-1] == "Bye$"
    assert tb.layout("x", "".join(lines)).problems == []
    proj = DecompProject(root)
    label = proj.maps.new_script("Town", "npc", "Hello there! I am a very chatty person who talks for a long time.")
    script = proj.maps.read_script("Town")
    text = next(t for t in tb.texts(script) if t.label == label.replace("_EventScript_", "_Text_"))
    assert text.problems == [] and len(text.boxes) >= 1
    assert script.count('.string "') >= 3  # the original text plus a wrapped multi-line one


def test_text_checks(root, capsys):
    p = root / "data/maps/Town/scripts.inc"
    p.write_text(p.read_text() + "\nTown_Text_Bad:\n\t.string \"" + "word " * 12 + "$\"\n")
    proj = DecompProject(root)
    msgs = [i.message for i in proj.check().issues]
    assert any("Town_Text_Bad" in m and "pixels wide" in m for m in msgs)
    assert not any("Town_Text_Hero" in m for m in msgs)  # unchanged texts are not reported
    assert main(["text", "check", "Town", "-p", str(root)]) == 1
    assert "Town_Text_Bad" in capsys.readouterr().out
    out = root / "t.png"
    assert main(["text", "preview", "Town", "Town_Text_Hero", "-o", str(out), "-p", str(root)]) == 0
    assert Image.open(out).width > 200
    srv = Server(DecompProject(root))
    res = srv.call("preview_text", {"name": "Town"})
    assert res[0]["type"] == "image" and "Town_Text_Bad" in res[1]["text"]


def test_save_points(root, capsys):
    proj = DecompProject(root)
    h = History(proj)
    assert h.available() and h.unsaved() == []
    with pytest.raises(DecompError, match="nothing changed"):
        h.save("x")
    s = root / "data/maps/Town/scripts.inc"
    original = s.read_text()
    s.write_text(original + "\n@ my change\n")
    from pkmn_asset_editor.decomp import _exclude_state_dir
    _exclude_state_dir(root)  # the usual setup: .pae/ is git-ignored
    (proj.state_dir).mkdir(parents=True, exist_ok=True)
    (proj.state_dir / "junk.txt").write_text("not saved")
    assert [c["file"] for c in h.unsaved()] == ["data/maps/Town/scripts.inc"]
    p1 = h.save("")
    assert p1["message"] == "Changed maps (Town)" and p1["files"] == 1
    assert h.show(p1["sha"])["kinds"] == ["maps"]
    s.write_text("oops")
    r = h.go_back(h.points()[1]["sha"])  # the save point before p1
    assert s.read_text() == original
    assert r["kept_unsaved"]["message"].startswith("Unsaved changes")
    assert [p["message"] for p in h.points()][:3] == [r["now"]["message"], r["kept_unsaved"]["message"], p1["message"]]
    assert main(["history", "-p", str(root)]) == 0
    assert "Went back to save point" in capsys.readouterr().out
    s.write_text(original + "\n@ again\n")
    assert main(["savepoint", "my", "message", "-p", str(root)]) == 0
    assert "my message" in capsys.readouterr().out
    srv = Server(DecompProject(root))
    assert json.loads(srv.call("list_save_points", {})[0]["text"])["save_points"][0]["message"] == "my message"


def test_story_flow_and_suggestions(root):
    st = DecompProject(root).maps.story
    p = root / "data/maps/Town_House1/scripts.inc"
    p.write_text(p.read_text() + "\nTown_House1_EventScript_Gift::\n\tgoto_if_set FLAG_GOT_GIFT, Town_House1_EventScript_Mom\n"
                 "\tsetflag FLAG_GOT_GIFT\n\tsetvar VAR_STORY, 2\n\tend\n")
    town = json.loads((root / "data/maps/Town/map.json").read_text())
    town["coord_events"] = [{"type": "trigger", "x": 1, "y": 1, "elevation": 3, "var": "VAR_STORY", "var_value": "2",
                             "script": "Town_EventScript_Hero"}]
    (root / "data/maps/Town/map.json").write_text(json.dumps(town))
    st.add_chapter("Home")
    st.set_chapter("Town", 0)
    f = st.flow()
    house = next(m for m in f["chapters"][0]["maps"] if m["map"] == "Town_House1")
    ev = house["events"][0]
    assert ev["sets"] == ["FLAG_GOT_GIFT"] and ev["needs"] == ["FLAG_GOT_GIFT on"] and ev["vars_set"] == ["VAR_STORY = 2"]
    u = f["usage"]["VAR_STORY"]
    assert u["set_by"][0]["how"] == "sets it to 2" and "step trigger works while it is 2" in u["checked_by"][0]["how"]
    assert f["usage"]["FLAG_HIDE_HERO"]["checked_by"][0]["how"].endswith("disappears while it is on")
    assert st.chapter_of("Town_House1") == 0  # inside maps follow their town
    assert st.suggest_chapters() == []  # everything is placed
    # a new map behind a door of Town, not named after it, is suggested into Town's chapter
    proj = st.maps.dp
    proj.maps.duplicate_map("Town_House1", "Cave", copy_layout=True)
    cave = json.loads((root / "data/maps/Cave/map.json").read_text())
    cave["warp_events"][0]["dest_map"] = "MAP_TOWN"
    (root / "data/maps/Cave/map.json").write_text(json.dumps(cave))
    st.maps.reload()
    assert st.chapter_of("Cave") is None
    assert [(s["map"], s["chapter"], s["via"]) for s in st.suggest_chapters()] == [("Cave", 0, "Town")]
    assert st.apply_suggestions() == 1 and st.chapter_of("Cave") == 0


def test_server_pages(root):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    proj = DecompProject(root)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(_State(proj)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        def get(path):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=30)
            c.request("GET", path)
            r = c.getresponse()
            return r.status, r.read()
        for page in ("/data", "/story", "/help", "/history.js"):
            assert get(page)[0] == 200, page
        st, body = get("/api/history")
        assert st == 200 and json.loads(body)["available"]
        st, body = get("/api/story/flow")
        assert st == 200 and "usage" in json.loads(body)
        c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=30)
        c.request("POST", "/api/map/text_preview", body=json.dumps({"text": "X_Text:\n\t.string \"Hi!$\"\n"}))
        r = json.loads(c.getresponse().read())
        assert r[0]["label"] == "X_Text" and r[0]["image"].startswith("data:image/png")
    finally:
        srv.shutdown()

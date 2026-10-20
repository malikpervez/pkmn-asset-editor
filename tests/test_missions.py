import json
import shutil
import subprocess
import time

import pytest

from pkmn_asset_editor.missions import Missions, note_activity

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


class Proj:
    def __init__(self, root, kind="decomp", status=()):
        self.root, self.kind, self._status = root, kind, list(status)

    def status(self):
        return self._status


def git(root, *args):
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


@pytest.fixture
def game(tmp_path):
    (tmp_path / "data" / "maps" / "Town").mkdir(parents=True)
    (tmp_path / "data" / "maps" / "Town" / "scripts.inc").write_text('Town_Text_Hi:\n\t.string "HELLO$"\n')
    (tmp_path / "data" / "maps" / "Town" / "map.json").write_text(json.dumps({"object_events": [{"x": 1}]}))
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / "trainers.party").write_text("=== TRAINER_A ===\n")
    (tmp_path / ".gitignore").write_text(".pae/\n")
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "start")
    return tmp_path


def done(m):
    return {x["id"] for x in m.status()["missions"] if x["done"]}


def test_missions_tick_themselves_off(game):
    m = Missions(Proj(game))
    st = m.status()
    assert st["done"] == 0 and st["current"] == "say" and st["total"] == 11 and st["git"]
    assert st["missions"][0]["link"]["href"].startswith("/maps")

    # moving words around without new text doesn't count; new words do
    p = game / "data" / "maps" / "Town" / "scripts.inc"
    p.write_text('Town_Text_Hi:\n\n\t.string "HELLO$"\n')
    assert "say" not in done(m)
    p.write_text('Town_Text_Hi:\n\t.string "HI THERE$"\n')
    st = m.status()
    assert st["just_done"] == ["say"] and st["current"] == "play"
    assert m.status()["just_done"] == []  # only once

    time.sleep(0.01)
    note_activity(game, "played")
    (game / "src" / "data" / "trainers.party").write_text("=== TRAINER_A ===\nGeodude\n")
    mj = game / "data" / "maps" / "Town" / "map.json"
    mj.write_text(json.dumps({"object_events": [{"x": 1}, {"x": 2}]}))
    assert {"say", "play", "trainer", "person"} <= done(m)

    git(game, "commit", "-qam", "my changes")
    (game / "pae_tests").mkdir()
    (game / "pae_tests" / "walk.json").write_text("{}")
    d = done(m)
    assert "savepoint" in d and "test" in d
    # done stays done, even after the change is undone
    p.write_text('Town_Text_Hi:\n\t.string "HELLO$"\n')
    assert "say" in done(m)


def test_changes_from_before_the_start_dont_count(game):
    (game / "src" / "data" / "trainers.party").write_text("changed before\n")
    p = game / "data" / "maps" / "Town" / "scripts.inc"
    p.write_text('Town_Text_Hi:\n\t.string "OLD EDIT$"\n')
    m = Missions(Proj(game))
    assert done(m) == set()
    p.write_text('Town_Text_Hi:\n\t.string "NEW EDIT$"\n')
    assert done(m) == {"say"}
    git(game, "commit", "-qam", "save")
    assert "savepoint" in done(m)


def test_skip_and_reset(game):
    m = Missions(Proj(game))
    st = m.skip("say")
    assert st["current"] == "play" and st["missions"][0]["skipped"]
    st = m.skip("say", False)
    assert st["current"] == "say"
    (game / "src" / "data" / "trainers.party").write_text("changed\n")
    assert "trainer" in done(m)
    git(game, "commit", "-qam", "x")
    st = m.reset()  # starts from now: the old change no longer counts
    assert st["done"] == 0


def test_without_git_and_rom_mode(tmp_path):
    (tmp_path / "src" / "data").mkdir(parents=True)
    m = Missions(Proj(tmp_path))
    assert m.status()["git"] is False
    time.sleep(0.01)
    (tmp_path / "src" / "data" / "wild_encounters.json").write_text("{}")
    assert "wild" in done(m)

    rom = Missions(Proj(tmp_path / "r", kind="rom"))
    (tmp_path / "r").mkdir()
    assert [x["id"] for x in rom.status()["missions"]] == ["picture", "build"]
    rom.proj._status = [{"id": "pokemon/mudkip/front"}]
    assert done(rom) == {"picture"}

import json

import pytest
from PIL import Image

from pkmn_asset_editor import replay
from pkmn_asset_editor.playtest import PlaySession, PlaytestError, parse_inputs


class FakeSession:
    """Stands in for PlaySession: a 'game' whose screen colour and position follow the frames pressed."""

    def __init__(self, root, speed=1):
        self.root = root
        self.speed = speed  # how far one frame of RIGHT walks (change it to simulate a changed game)
        self.game = None
        self.started = None
        self.steps = []
        self.closed = False

    def _checkpoint_path(self, name):
        return self.root / ".pae" / "checkpoints" / f"{name}.sav"

    def start(self, map_name, x=None, y=None, checkpoint=None, state=None, script=None, save_file=None):
        self.game = object()
        self.x, self.y, self.map = x or 0, y or 0, map_name
        self.beaten = False
        self.flag = "FLAG_X" in (state or [])
        self.started = {"map": map_name, "x": x, "y": y, "checkpoint": checkpoint,
                        "save_file": str(save_file) if save_file else None, "state": list(state or []), "script": script}
        self.steps = []
        self.save_file = save_file

    def press(self, inputs, record_every=0):
        steps = parse_inputs(inputs)
        self.steps.append({"op": "press", "inputs": [[k, n] for k, n in steps]})
        for keys, n in steps:
            if keys & 16:  # RIGHT
                self.x += n * self.speed
        return self.screenshot(), []

    def start_battle(self, trainer=None, species=None, level=5, item=None, double=False, frames=240):
        self.steps.append({"op": "battle", "trainer": trainer, "species": species, "level": level, "item": item,
                           "double": double, "frames": frames})
        self.beaten = self.x >= 10

    def run_script(self, label, frames=30):
        self.steps.append({"op": "script", "label": label, "frames": frames})

    def apply_state(self, ops):
        self.steps.append({"op": "state", "state": list(ops)})

    def save_game(self):
        pass

    def location(self):
        return {"map": self.map, "x": self.x, "y": self.y}

    def in_battle(self):
        return False

    def flags(self, names):
        return {n: self.flag for n in names}

    def vars(self, names):
        return {n: 3 for n in names}

    def trainers_beaten(self, names):
        return {n: self.beaten for n in names}

    def screenshot(self):
        return Image.new("RGB", (240, 160), (min(255, self.x * 10), 0, 0))

    def close(self):
        self.closed = True


def test_record_run_and_accept(tmp_path):
    tests = replay.ReplayTests(tmp_path)
    s = FakeSession(tmp_path)
    with pytest.raises(PlaytestError):
        tests.record(s, "walk")  # nothing started
    s.start("Town", 1, 2, state=["FLAG_X"])
    s.press("RIGHT*12")
    s.start_battle(trainer="TRAINER_A")
    t = tests.record(s, "walk east", note="walk", flags=["FLAG_X"], trainers=["TRAINER_A"])
    assert t["expect"] == {"map": "Town", "x": 13, "y": 2, "in_battle": False, "flags": {"FLAG_X": True},
                           "trainers": {"TRAINER_A": True}}
    assert (tmp_path / "pae_tests" / "walk east.png").exists()
    with pytest.raises(PlaytestError):
        tests.record(s, "walk east")  # already there
    with pytest.raises(PlaytestError):
        tests.record(s, "../evil")

    # the same game: passes
    r = tests.run("walk east", FakeSession(tmp_path))
    assert r["ok"], r
    assert r["summary"] == "passed"
    listed = tests.list()
    assert listed[0]["name"] == "walk east" and listed[0]["frames"] == 12 and listed[0]["last"]["ok"]

    # a changed game (walks slower): position, trainer and screen all change
    r = tests.run("walk east", FakeSession(tmp_path, speed=0))
    assert not r["ok"]
    bad = {c["what"] for c in r["checks"] if not c["ok"]}
    assert bad == {"position", "beaten TRAINER_A", "screen"}
    assert "position (13,2 → 1,2)" in r["summary"]
    assert (tests.results_dir / "walk east.diff.png").exists()
    assert Image.open(tests.results_dir / "walk east.diff.png").size == (240 * 2 * 3 + 16, 320)

    # it was on purpose: accept
    tests.accept("walk east", FakeSession(tmp_path, speed=0))
    assert tests.load("walk east")["expect"]["x"] == 1
    assert tests.run("walk east", FakeSession(tmp_path, speed=0))["ok"]

    tests.delete("walk east")
    assert tests.list() == [] and not list(tests.results_dir.glob("walk east*"))


def test_checkpoint_save_is_copied(tmp_path):
    tests = replay.ReplayTests(tmp_path)
    s = FakeSession(tmp_path)
    cp = s._checkpoint_path("gym")
    cp.parent.mkdir(parents=True)
    cp.write_bytes(b"S" * 0x20000)
    s.start(None, checkpoint="gym")
    s.press("RIGHT*2")
    tests.record(s, "from gym")
    t = tests.load("from gym")
    assert t["start"]["save"] == "from gym.sav" and t["start"]["from_checkpoint"] == "gym"
    assert (tmp_path / "pae_tests" / "from gym.sav").read_bytes() == b"S" * 0x20000
    cp.unlink()  # the test keeps working without the checkpoint
    s2 = FakeSession(tmp_path)
    assert tests.run("from gym", s2)["ok"]
    assert str(s2.save_file).endswith("from gym.sav")


def test_replay_errors_are_reported(tmp_path):
    tests = replay.ReplayTests(tmp_path)
    s = FakeSession(tmp_path)
    s.start("Town")
    tests.record(s, "t")
    t = tests.load("t")
    t["steps"] = [{"op": "dance"}]
    (tmp_path / "pae_tests" / "t.json").write_text(json.dumps(t))
    r = tests.run("t", FakeSession(tmp_path))
    assert not r["ok"] and "unknown step" in r["summary"]
    assert "❌" in tests.report([r]) and "| t |" in tests.report([r])


def test_session_records_live_frames_merged(tmp_path):
    s = PlaySession(tmp_path)
    s.note_frames(1, 1)  # not started: ignored
    assert s.steps == []
    s.started = {"map": "Town"}
    for keys in (0, 0, 0, 16, 16, 0):
        s.note_frames(keys, 1)
    s.note_frames(0, 4)  # fast-forward runs 4 frames per tick
    assert s.steps == [{"op": "press", "inputs": [[0, 3], [16, 2], [0, 5]]}]
    assert parse_inputs([[16, 2], (0, 5)]) == [(16, 2), (0, 5)]


def test_screen_difference():
    a = Image.new("RGB", (240, 160), (10, 10, 10))
    b = a.copy()
    assert replay.screen_difference(a, b)[0] == 0
    b.paste((10, 10, 20), (0, 0, 240, 80))  # a tiny colour change doesn't count
    assert replay.screen_difference(a, b)[0] == 0
    b.paste((200, 10, 10), (0, 0, 24, 16))  # a strong change in one channel does
    share, pic = replay.screen_difference(a, b)
    assert share == pytest.approx(24 * 16 / (240 * 160))
    assert pic.getpixel((0, 0)) == (230, 30, 40)


def test_workflow(tmp_path):
    p = replay.write_workflow(tmp_path)
    text = p.read_text()
    assert p == tmp_path / ".github" / "workflows" / "pae.yml"
    for need in ("libmgba-dev", "gcc-arm-none-eabi", "pae check", "pae test run --no-build", "upload-artifact"):
        assert need in text
    with pytest.raises(PlaytestError):
        replay.write_workflow(tmp_path)
    replay.write_workflow(tmp_path, force=True)

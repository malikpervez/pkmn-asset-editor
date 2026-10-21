"""Replay tests: play something once, then replay it after every change and be told what changed.

A test is a play-test recorded from the test window (or ``pae test record``): where it started (a map, a
checkpoint save, story state), every button press and frame, every battle and script started, and what the game
showed at the end. Running it plays exactly the same inputs on the newly built ROM (the emulator is deterministic)
and compares:

* the screen at the end (a small share of changed pixels is allowed; see ``threshold``),
* where the player is (map and position) and whether a battle is going on,
* the flags, variables and beaten trainers the test was told to watch.

Tests live in ``pae_tests/`` in the project (commit them): ``NAME.json`` (start, steps, expectations), ``NAME.png``
(the expected screen) and ``NAME.sav`` when the test starts from a checkpoint. Results of the last run go to
``.pae/test-results/`` (the screen it got and a picture of the differences).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from PIL import Image, ImageChops

from .playtest import PlaySession, PlaytestError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")
DEFAULT_THRESHOLD = 0.01  # share of pixels allowed to differ
PIXEL_TOLERANCE = 24      # per colour channel, so a tiny palette tweak doesn't count as a changed pixel


def screen_difference(expected: Image.Image, got: Image.Image) -> tuple[float, Image.Image]:
    """(share of changed pixels, a picture: the expected screen faded, with changed pixels in red)."""
    a, b = expected.convert("RGB"), got.convert("RGB")
    if a.size != b.size:
        b = b.resize(a.size)
    diff = ImageChops.difference(a, b)
    mask = diff.convert("L").point(lambda v: 255 if v > PIXEL_TOLERANCE else 0)
    # convert("L") averages the channels, so also catch a big change in a single channel
    for band in diff.split():
        mask = ImageChops.lighter(mask, band.point(lambda v: 255 if v > PIXEL_TOLERANCE else 0))
    changed = mask.histogram()[255]
    faded = Image.blend(a.convert("L").convert("RGB"), Image.new("RGB", a.size, (255, 255, 255)), 0.55)
    faded.paste(Image.new("RGB", a.size, (230, 30, 40)), mask=mask)
    return changed / (a.width * a.height), faded


def side_by_side(*images: Image.Image, scale: int = 2) -> Image.Image:
    w, h = images[0].width * scale, images[0].height * scale
    out = Image.new("RGB", (w * len(images) + 8 * (len(images) - 1), h), (20, 20, 24))
    for i, im in enumerate(images):
        out.paste(im.convert("RGB").resize((w, h), Image.NEAREST), (i * (w + 8), 0))
    return out


class ReplayTests:
    def __init__(self, root: Path, cache_dir: Path | None = None, log=None):
        self.root = Path(root)
        self.dir = self.root / "pae_tests"
        self.results_dir = self.root / ".pae" / "test-results"
        self.cache_dir = cache_dir or self.root / ".pae" / "cache"
        self.log = log or (lambda *_: None)

    # ------------------------------------------------------------------ files
    def _path(self, name: str, ext: str = ".json") -> Path:
        if not NAME_RE.match(name or ""):
            raise PlaytestError("test names may use letters, digits, spaces, - and _ (up to 64)")
        return self.dir / f"{name}{ext}"

    def load(self, name: str) -> dict:
        p = self._path(name)
        if not p.exists():
            raise PlaytestError(f"no test named {name}")
        return json.loads(p.read_text())

    def _save(self, name: str, test: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(name).write_text(json.dumps(test, indent=1) + "\n")

    def list(self) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                t = json.loads(p.read_text())
            except ValueError:
                continue
            last = self.results_dir / f"{p.stem}.json"
            res = None
            if last.exists():
                try:
                    res = json.loads(last.read_text())
                except ValueError:
                    pass
            out.append({"name": p.stem, "note": t.get("note", ""), "created": t.get("created"),
                        "start": t.get("start", {}), "frames": frames_in(t.get("steps", [])),
                        "watch": t.get("watch", {}), "last": res and {k: res.get(k) for k in ("ok", "ran", "summary")}})
        return out

    def delete(self, name: str) -> None:
        if not self._path(name).exists():
            raise PlaytestError(f"no test named {name}")
        for ext in (".json", ".png", ".sav"):
            self._path(name, ext).unlink(missing_ok=True)
        for p in self.results_dir.glob(f"{name}*"):
            if p.stem == name or p.stem.startswith(name + "."):
                p.unlink(missing_ok=True)

    # ------------------------------------------------------------------ record
    def record(self, session: PlaySession, name: str, note: str = "", flags=(), vars=(), trainers=(),
               threshold: float = DEFAULT_THRESHOLD, overwrite: bool = False, team: bool = False) -> dict:
        """Save what was played in ``session`` since it started as test ``name``; what the game shows now is
        what every replay must show."""
        if session.game is None or session.started is None:
            raise PlaytestError("start a play-test first, then play what the test should check")
        if self._path(name).exists() and not overwrite:
            raise PlaytestError(f"there is already a test named {name}")
        start = dict(session.started)
        self.dir.mkdir(parents=True, exist_ok=True)
        save = start.pop("save_file", None)
        cp = start.pop("checkpoint", None)
        src = Path(save) if save else (session._checkpoint_path(cp) if cp else None)
        if src is not None:
            if src.resolve() != self._path(name, ".sav").resolve():
                shutil.copyfile(src, self._path(name, ".sav"))
            start["save"] = f"{name}.sav"
            start["from_checkpoint"] = cp
        else:
            self._path(name, ".sav").unlink(missing_ok=True)
        test = {"note": note, "created": time.strftime("%Y-%m-%d %H:%M"), "start": start,
                "steps": [dict(s) for s in session.steps],
                "watch": {"flags": list(flags), "vars": list(vars), "trainers": list(trainers)},
                "threshold": threshold}
        if team:
            test["watch"]["team"] = True
        test["expect"] = self._observe(session, test["watch"])
        session.screenshot().save(self._path(name, ".png"))
        self._save(name, test)
        return {"name": name, **test}

    def _observe(self, session: PlaySession, watch: dict) -> dict:
        where = session.location()
        out = {"map": where["map"], "x": where["x"], "y": where["y"], "in_battle": session.in_battle()}
        if watch.get("flags"):
            out["flags"] = session.flags(watch["flags"])
        if watch.get("vars"):
            out["vars"] = session.vars(watch["vars"])
        if watch.get("trainers"):
            out["trainers"] = session.trainers_beaten(watch["trainers"])
        if watch.get("team"):
            out["team"] = session.team_summary()
        return out

    # ------------------------------------------------------------------ replay
    def _replay(self, session: PlaySession, name: str, test: dict) -> None:
        st = test.get("start", {})
        save = self._path(name, ".sav") if st.get("save") else None
        if save is not None and not save.exists():
            raise PlaytestError(f"{save.name} is missing (the save this test starts from)")
        session.start(st.get("map"), st.get("x"), st.get("y"), state=st.get("state") or None,
                      script=st.get("script"), save_file=save)
        for step in test.get("steps", []):
            op = step.get("op")
            if op == "press":
                session.press([tuple(x) for x in step["inputs"]])
            elif op == "battle":
                session.start_battle(trainer=step.get("trainer"), species=step.get("species"),
                                     level=step.get("level", 5), item=step.get("item"),
                                     double=step.get("double", False), frames=step.get("frames", 240))
            elif op == "script":
                session.run_script(step["label"], step.get("frames", 30))
            elif op == "state":
                session.apply_state(step["state"])
            elif op == "save":
                session.save_game()
            else:
                raise PlaytestError(f"unknown step {op!r} in test {name}")

    def run(self, name: str, session: PlaySession | None = None) -> dict:
        """Replay test ``name`` on the built ROM and compare. The result says what differs."""
        test = self.load(name)
        own = session is None
        if own:
            session = PlaySession(self.root, self.cache_dir, self.log, scratch=self.cache_dir / "replay.sav")
        t0 = time.time()
        res = {"name": name, "ran": time.strftime("%Y-%m-%d %H:%M"), "checks": []}
        try:
            try:
                self._replay(session, name, test)
            except PlaytestError as e:
                res.update(ok=False, error=str(e), summary=f"could not replay: {e}")
                return self._finish(name, res)
            got = session.screenshot()
            now = self._observe(session, test.get("watch", {}))
        finally:
            if own:
                session.close()
        exp = test.get("expect", {})
        checks = res["checks"]

        def check(what, expected, actual):
            checks.append({"what": what, "expected": expected, "got": actual, "ok": expected == actual})

        check("map", exp.get("map"), now["map"])
        check("position", [exp.get("x"), exp.get("y")], [now["x"], now["y"]])
        check("in a battle", exp.get("in_battle"), now["in_battle"])
        for kind, label in (("flags", "flag"), ("vars", "variable"), ("trainers", "beaten")):
            for k, v in (exp.get(kind) or {}).items():
                check(f"{label} {k}", v, (now.get(kind) or {}).get(k))
        if "team" in exp:
            check("team", exp["team"], now.get("team"))
        self.results_dir.mkdir(parents=True, exist_ok=True)
        got.save(self.results_dir / f"{name}.got.png")
        ref = self._path(name, ".png")
        if ref.exists():
            expected = Image.open(ref)
            share, diff = screen_difference(expected, got)
            threshold = float(test.get("threshold", DEFAULT_THRESHOLD))
            checks.append({"what": "screen", "expected": f"at most {threshold:.1%} of pixels changed",
                           "got": f"{share:.1%} changed", "ok": share <= threshold, "share": round(share, 4)})
            side_by_side(expected, got, diff).save(self.results_dir / f"{name}.diff.png")
        failed = [c for c in checks if not c["ok"]]
        res["ok"] = not failed
        res["seconds"] = round(time.time() - t0, 1)
        res["summary"] = "passed" if not failed else "changed: " + ", ".join(
            f"{c['what']} ({_short(c['expected'])} → {_short(c['got'])})" for c in failed)
        return self._finish(name, res)

    def _finish(self, name: str, res: dict) -> dict:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.results_dir / f"{name}.json").write_text(json.dumps(res, indent=1))
        return res

    def run_all(self, names: list[str] | None = None) -> list[dict]:
        names = names or [t["name"] for t in self.list()]
        if not names:
            return []
        session = PlaySession(self.root, self.cache_dir, self.log, scratch=self.cache_dir / "replay.sav")
        try:
            return [self.run(n, session) for n in names]
        finally:
            session.close()

    def accept(self, name: str, session: PlaySession | None = None) -> dict:
        """The game now looks/ends like this on purpose: replay and keep what it shows as the new expectation."""
        test = self.load(name)
        own = session is None
        if own:
            session = PlaySession(self.root, self.cache_dir, self.log, scratch=self.cache_dir / "replay.sav")
        try:
            self._replay(session, name, test)
            test["expect"] = self._observe(session, test.get("watch", {}))
            session.screenshot().save(self._path(name, ".png"))
        finally:
            if own:
                session.close()
        test["accepted"] = time.strftime("%Y-%m-%d %H:%M")
        self._save(name, test)
        (self.results_dir / f"{name}.json").unlink(missing_ok=True)
        return {"name": name, **test}

    def set_watch(self, name: str, flags=None, vars=None, trainers=None, threshold: float | None = None,
                  team: bool | None = None) -> dict:
        """Change what a test watches; the expected values are read by replaying it."""
        test = self.load(name)
        w = test.setdefault("watch", {})
        for k, v in (("flags", flags), ("vars", vars), ("trainers", trainers)):
            if v is not None:
                w[k] = [x.strip() for x in v if x.strip()]
        if team is not None:
            if team:
                w["team"] = True
            else:
                w.pop("team", None)
                test.get("expect", {}).pop("team", None)
        if threshold is not None:
            test["threshold"] = float(threshold)
        self._save(name, test)
        return self.accept(name)

    # ------------------------------------------------------------------ CI
    def report(self, results: list[dict]) -> str:
        """A Markdown summary (for a GitHub Actions job summary)."""
        lines = ["## Replay tests", "", "| Test | Result |", "|---|---|"]
        for r in results:
            lines.append(f"| {r['name']} | {'✅' if r.get('ok') else '❌'} {r.get('summary', '')} |")
        if not results:
            lines.append("| (none) | record one in the test window: 🔴 Save as a test |")
        return "\n".join(lines) + "\n"


def frames_in(steps: list[dict]) -> int:
    return sum(n for s in steps if s.get("op") == "press" for _, n in s.get("inputs", []))


def _short(v) -> str:
    if isinstance(v, list):
        return ",".join(str(x) for x in v)
    return str(v)


WORKFLOW = """\
# Built by `pae ci init` (pkmn-asset-editor). On every push and pull request: build the game, run `pae check`
# and replay the tests in pae_tests/. Download the "pae-test-results" artifact to see the screens that changed.
name: pae

on:
  push:
  pull_request:
  workflow_dispatch:

jobs:
  check:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v4
      - name: Install the build tools and the emulator library
        run: |
          sudo apt-get update
          sudo apt-get install -y build-essential binutils-arm-none-eabi gcc-arm-none-eabi libnewlib-arm-none-eabi \\
            libpng-dev libmgba-dev python3-pip
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install pkmn-asset-editor
        run: pip install "git+https://github.com/malikpervez/pkmn-asset-editor"
      - name: Build the game
        run: make -j"$(nproc)"
      - name: Check graphics, maps, data and text
        run: pae check --all
      - name: Replay tests
        run: pae test run --no-build --summary "$GITHUB_STEP_SUMMARY"
      - name: Keep the screens
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: pae-test-results
          path: .pae/test-results/
          if-no-files-found: ignore
"""


def write_workflow(root: Path, force: bool = False) -> Path:
    p = Path(root) / ".github" / "workflows" / "pae.yml"
    if p.exists() and not force:
        raise PlaytestError(f"{p.relative_to(root)} already exists (use --force to replace it)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(WORKFLOW)
    return p

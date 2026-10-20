"""Guided missions: small real tasks in your own game, one after another, each ticked off by itself.

Each mission says what to do in a few steps and links to the page where you do it. The tool notices when it is
done by looking at the game folder: what changed since the missions started (git, or file times without git),
and what the tool did (a play-test started, a checkpoint, a test, a release). State is kept in
``.pae/missions.json``; done missions stay done.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ACTIVITY = "activity.json"


def note_activity(root: Path, what: str) -> None:
    """Remember that something happened (e.g. "played"), for missions."""
    p = Path(root) / ".pae" / ACTIVITY
    try:
        data = json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        data = {}
    data[what] = time.time()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except OSError:
        pass


# id, title, why, steps, link, help anchor. In steps, **x** is shown bold.
DECOMP = [
    ("say", "Change what someone says",
     "The quickest way to make the game yours: new words in a real conversation.",
     ["Open **🗺️ Maps** and pick **LittlerootTown** on the left (or any town).",
      "Open the **People & doors** tab and click a person on the map.",
      "Press **Open script**. Their words are on the lines that start with **.string**.",
      "Change the words between the quotes, then press **Save script**. The preview below shows how it looks in the game."],
     ("/maps?map=LittlerootTown", "Open Littleroot Town"), "dialogue"),
    ("play", "Play your change",
     "See your change in the real game, running right in the browser.",
     ["On **🗺️ Maps**, press **▶ Test in game**.",
      "Press **▶ Build & play**. The first build takes a few minutes; later ones are quick.",
      "Walk with the arrow keys and press **Z** (the A button) next to the person you changed."],
     ("/maps?map=LittlerootTown", "Open Maps"), "test"),
    ("town", "Rename a town",
     "Every place has a name on the town map and on the sign when you arrive.",
     ["Open **🌍 World** and pick **Town map & flying**.",
      "Click a town's square on the town map.",
      "Type a new name (up to 18 letters) and press **Rename**."],
     ("/world?tab=region", "Open the town map"), "world"),
    ("trainer", "Give a trainer a new team",
     "Make a battle easier or harder, or give a gym leader your favourite Pokémon.",
     ["Open **🔴 Pokémon & Trainers** and search for **roxanne**.",
      "Change a Pokémon, its level or its moves.",
      "Press **Save**. Then **⚔️ Test this battle** plays it straight away."],
     ("/data?tab=trainers&id=TRAINER_ROXANNE_1", "Open Roxanne"), "trainer"),
    ("wild", "Change the wild Pokémon",
     "Choose which Pokémon live in the grass of a route.",
     ["Open **🔴 Pokémon & Trainers** → **Wild Pokémon** and pick a route, like **Route101**.",
      "Change the Pokémon or the levels in some slots (slots with a small chance are rare).",
      "Press **Save**."],
     ("/data?tab=wild", "Open Wild Pokémon"), "wild"),
    ("person", "Add a person who talks",
     "Put someone new in a town and decide what they say.",
     ["On **🗺️ Maps**, press **+ Person** above the map and click where they should stand.",
      "On the right, pick what they **Look like**.",
      "Press **🧩 Build what happens**, add a **💬 Say** block and type their words.",
      "Press **Save** (top right)."],
     ("/maps?map=LittlerootTown", "Open Maps"), "person"),
    ("picture", "Redraw a Pokémon",
     "Change how a Pokémon looks in battle.",
     ["Open **🎨 Graphics** and search for a Pokémon, like **mudkip**.",
      "Click its front picture.",
      "Draw on it with the **Pencil** and press **Save drawing**, or press **Replace with image…** and pick a picture."],
     ("/?q=mudkip", "Find Mudkip"), "sprite"),
    ("checkpoint", "Save a checkpoint",
     "A checkpoint is a saved game, so a play-test can start later in the story instead of from the beginning.",
     ["Start a play-test (**▶ Test in game**) and walk somewhere.",
      "Press **Save checkpoint…** and give it a name.",
      "Next time, pick it in **Start from** before pressing play."],
     ("/maps?map=LittlerootTown", "Open Maps"), "later"),
    ("savepoint", "Make a save point",
     "A save point remembers your whole game as it is now. You can always go back to it.",
     ["Press **💾 Save point** at the top of any page.",
      "Write a few words about what you changed and press OK.",
      "**🕘 History** lists your save points."],
     None, "savepoints"),
    ("test", "Save a replay test",
     "A replay test plays the same thing again after every change and tells you if something broke.",
     ["Start a play-test and play something short: walk through a town, or start a battle and win it.",
      "Press **🔴 Save as a test…** and name it.",
      "Open **🧪 Tests** and press **Run without building**: it should pass."],
     ("/tests", "Open Tests"), "tests"),
    ("release", "Share your game",
     "Make a patch that other people can put on their own copy of the game.",
     ["Open **📦 Release**.",
      "Fill in the name of your game and pick your original game file.",
      "Press **📦 Build release** and share the zip it makes."],
     ("/release", "Open Release"), "release"),
]

ROM = [
    ("picture", "Change a picture",
     "Change how a Pokémon, an item or a trainer looks.",
     ["Search for a picture, like **mudkip**.",
      "Click it, then draw with the **Pencil** and press **Save drawing**, or press **Replace with image…**."],
     ("/?q=mudkip", "Find Mudkip"), "sprite"),
    ("build", "Build your game",
     "Write a new game file with your changes, and patches to share it.",
     ["Press **Build ROM** at the top.",
      "Open the new game file (build/rom.gba) in an emulator like mGBA."],
     None, "build"),
]


class Missions:
    def __init__(self, proj):
        self.proj = proj
        self.root = Path(proj.root)
        self.decomp = getattr(proj, "kind", "") == "decomp"
        self.path = self.root / ".pae" / "missions.json"

    # ------------------------------------------------------------------ state
    def _git(self, *args) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    def state(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except ValueError:
                pass
        st = {"started": time.time(), "base": None, "head": None, "done": {}, "skipped": []}
        if self.decomp:
            head = (self._git("rev-parse", "HEAD") or "").strip()
            if head:
                # the files as they are now (uncommitted changes too), so only changes from now on count
                snap = (self._git("-c", "user.name=pae", "-c", "user.email=pae@localhost", "stash", "create",
                                  "pae missions start") or "").strip()
                st["head"], st["base"] = head, snap or head
                if snap:  # keep the snapshot from being cleaned away by git gc
                    self._git("update-ref", "refs/pae/missions-start", snap)
        self._write(st)
        return st

    def _write(self, st: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(st, indent=1))

    def reset(self) -> dict:
        self.path.unlink(missing_ok=True)
        return self.status()

    def skip(self, mid: str, skipped: bool = True) -> dict:
        st = self.state()
        s = set(st.get("skipped", []))
        (s.add if skipped else s.discard)(mid)
        st["skipped"] = sorted(s)
        self._write(st)
        return self.status()

    # ------------------------------------------------------------------ checks
    def _changed(self, st: dict) -> set[str]:
        """Files changed since the missions started (relative paths)."""
        out: set[str] = set()
        if st.get("base"):
            diff = self._git("diff", "--name-only", st["base"])
            new = self._git("ls-files", "--others", "--exclude-standard")
            if diff is not None and new is not None:
                out = {x for x in (diff + new).splitlines() if x}
                # untracked files count only when they are new since the start
                for x in new.splitlines():
                    p = self.root / x
                    if x and p.exists() and p.stat().st_mtime < st["started"]:
                        out.discard(x)
                return out
        for sub in ("data/maps", "data/text", "src/data", "graphics"):
            base = self.root / sub
            if base.is_dir():
                out |= {str(p.relative_to(self.root)) for p in base.rglob("*")
                        if p.is_file() and p.stat().st_mtime > st["started"]}
        return out

    def _diff_text(self, st: dict, paths: list[str]) -> str:
        if not st.get("base") or not paths:
            return ""
        return self._git("diff", "-U0", st["base"], "--", *paths) or ""

    def _git_show(self, st: dict, path: str) -> str | None:
        return self._git("show", f"{st['base']}:{path}") if st.get("base") else None

    def _newer(self, folder: Path, pattern: str, since: float) -> bool:
        return folder.is_dir() and any(p.stat().st_mtime > since for p in folder.glob(pattern))

    def _activity(self) -> dict:
        p = self.root / ".pae" / ACTIVITY
        try:
            return json.loads(p.read_text()) if p.exists() else {}
        except ValueError:
            return {}

    def _check(self, mid: str, st: dict, changed: set[str]) -> bool:
        started = st["started"]
        if mid == "say":
            texts = [c for c in changed if c.endswith((".inc", ".pory", ".txt"))
                     and (c.startswith("data/maps/") or c.startswith("data/text/"))]
            if not st.get("base"):
                return bool(texts)
            return any(line.startswith("+") and ".string" in line for line in self._diff_text(st, texts).splitlines())
        if mid == "play":
            return self._activity().get("played", 0) > started
        if mid == "town":
            return any("region_map_sections" in c or c.startswith("src/data/region_map/") for c in changed)
        if mid == "trainer":
            return any(c.startswith("src/data/trainers") for c in changed)
        if mid == "wild":
            return "src/data/wild_encounters.json" in changed
        if mid == "person":
            for c in changed:
                if not (c.startswith("data/maps/") and c.endswith("/map.json")):
                    continue
                try:
                    now = len(json.loads((self.root / c).read_text()).get("object_events") or [])
                except (OSError, ValueError):
                    continue
                old = self._git_show(st, c)
                try:
                    before = len(json.loads(old).get("object_events") or []) if old else 0
                except ValueError:
                    before = 0
                if now > before:
                    return True
            return False
        if mid == "picture":
            if not self.decomp:
                try:
                    return bool(self.proj.status())
                except Exception:  # noqa: BLE001 - a broken project is not a finished mission
                    return False
            return any(c.startswith("graphics/pokemon/") and c.endswith((".png", ".pal")) for c in changed)
        if mid == "checkpoint":
            return self._newer(self.root / ".pae" / "checkpoints", "*.sav", started)
        if mid == "savepoint":
            if not st.get("base"):
                return False
            n = self._git("rev-list", "--count", f"{st.get('head') or st['base']}..HEAD")
            return bool(n and int(n.strip() or 0) > 0)
        if mid == "test":
            return self._newer(self.root / "pae_tests", "*.json", started)
        if mid == "release":
            return self._newer(self.root / ".pae" / "releases", "*.zip", started)
        if mid == "build":
            return self._newer(self.root / "build", "*.gba", started)
        return False

    # ------------------------------------------------------------------ public
    def status(self) -> dict:
        st = self.state()
        changed = self._changed(st)
        done = dict(st.get("done", {}))
        newly = []
        out = []
        for mid, title, why, steps, link, help_id in (DECOMP if self.decomp else ROM):
            if mid not in done and self._check(mid, st, changed):
                done[mid] = time.time()
                newly.append(mid)
            out.append({"id": mid, "title": title, "why": why, "steps": steps,
                        "link": {"href": link[0], "label": link[1]} if link else None,
                        "help": f"/help#{help_id}", "done": mid in done,
                        "skipped": mid in st.get("skipped", [])})
        if newly:
            st["done"] = done
            self._write(st)
        current = next((m["id"] for m in out if not m["done"] and not m["skipped"]), None)
        return {"missions": out, "done": sum(m["done"] for m in out), "total": len(out), "current": current,
                "just_done": newly, "git": bool(st.get("base")) or not self.decomp}

"""Save points: git commits with plain-language names, a history to look through, and going back.

* A save point commits every changed file in the game folder (``git add -A``; build output stays out through the
  project's ``.gitignore``, and ``.pae/`` through ``.git/info/exclude``).
* Going back never deletes history: the files are restored to that save point and the result is saved as a new
  save point, so you can always come forward again. Unsaved changes are kept in a save point of their own first.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .decomp import DecompError, _exclude_state_dir

AUTHOR = ("-c", "user.name=ROM hacker", "-c", "user.email=romhacker@localhost")


class History:
    def __init__(self, dp):
        self.dp = dp
        self.root: Path = Path(dp.root)

    def _git(self, *args: str, check: bool = True, identity: bool = False) -> str:
        cmd = ["git", "-C", str(self.root)]
        if identity and not self._has_identity():
            cmd += list(AUTHOR)
        try:
            r = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DecompError(f"git is not available: {e}")
        if check and r.returncode != 0:
            raise DecompError((r.stderr or r.stdout).strip() or f"git {args[0]} failed")
        return r.stdout

    def my_emails(self) -> set[str]:
        """Who makes this game's save points: the git identity, and the one save points use when there is none."""
        r = subprocess.run(["git", "-C", str(self.root), "config", "user.email"], capture_output=True, text=True)
        return {e for e in (r.stdout.strip(), AUTHOR[3].split("=", 1)[1]) if e}

    def _has_identity(self) -> bool:
        r = subprocess.run(["git", "-C", str(self.root), "config", "user.name"], capture_output=True, text=True)
        return r.returncode == 0 and bool(r.stdout.strip())

    def available(self) -> bool:
        try:
            return self._git("rev-parse", "--is-inside-work-tree", check=False).strip() == "true"
        except DecompError:
            return False

    def _require(self) -> None:
        if not self.available():
            raise DecompError("save points need the game folder to be a git repository (run `git init` in it once)")

    def unsaved(self) -> list[dict]:
        """Files changed since the last save point: [{file, state}]."""
        self._require()
        out = []
        for line in self._git("status", "--porcelain", "--untracked-files=all").splitlines():
            code, path = line[:2], line[3:]
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            path = path.strip('"')
            if path.startswith(".pae/"):
                continue
            state = "new" if "?" in code or "A" in code else "deleted" if "D" in code else "changed"
            out.append({"file": path, "state": state})
        return out

    def points(self, limit: int = 50) -> list[dict]:
        self._require()
        if not self._git("rev-parse", "--verify", "-q", "HEAD", check=False).strip():
            return []
        out = []
        raw = self._git("log", f"-n{int(limit)}", "--date=format:%Y-%m-%d %H:%M", "--shortstat",
                        "--pretty=format:\x1e%H\x1f%h\x1f%an\x1f%ad\x1f%s\x1f%ae")
        for chunk in raw.split("\x1e"):
            if not chunk.strip():
                continue
            head, _, stat = chunk.partition("\n")
            sha, short, author, date, subject, email = (head.split("\x1f") + [""] * 6)[:6]
            m = re.search(r"(\d+) files? changed", stat)
            out.append({"sha": sha, "short": short, "author": author, "email": email, "date": date, "message": subject,
                        "files": int(m.group(1)) if m else 0})
        return out

    def show(self, sha: str) -> dict:
        self._require()
        sha = self._resolve(sha)
        files = []
        for line in self._git("show", "--name-status", "--pretty=format:", sha).splitlines():
            if not line.strip():
                continue
            st, *paths = line.split("\t")
            files.append({"file": paths[-1], "state": {"A": "new", "D": "deleted"}.get(st[:1], "changed")})
        msg = self._git("log", "-1", "--pretty=format:%s", sha)
        return {"sha": sha, "message": msg, "files": files, "kinds": _kinds(files)}

    def _resolve(self, sha: str) -> str:
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}|HEAD(~\d+)?", sha.strip()):
            raise DecompError(f"'{sha}' is not a save point")
        return self._git("rev-parse", "--verify", sha.strip() + "^{commit}").strip()

    def save(self, message: str = "") -> dict:
        """Make a save point of every unsaved change."""
        self._require()
        changes = self.unsaved()
        if not changes:
            raise DecompError("nothing changed since the last save point")
        message = (message or "").strip() or _auto_message(changes)
        _exclude_state_dir(self.root)  # .pae/ (checkpoints, caches) never goes into a save point
        self._git("add", "-A", ".")
        self._git("commit", "-q", "-m", message, identity=True)
        self._refresh()
        return self.points(1)[0]

    def go_back(self, sha: str) -> dict:
        """Put every file back the way it was at a save point (as a new save point; nothing is lost)."""
        self._require()
        sha = self._resolve(sha)
        target = self._git("log", "-1", "--pretty=format:%h %s", sha)
        kept = None
        if self.unsaved():
            kept = self.save("Unsaved changes (kept before going back)")
        self._git("restore", f"--source={sha}", "--staged", "--worktree", "--", ".")
        if self.unsaved():
            _exclude_state_dir(self.root)
            self._git("add", "-A", ".")
            self._git("commit", "-q", "-m", f"Went back to save point {target}", identity=True)
        self._refresh()
        return {"now": self.points(1)[0], "kept_unsaved": kept}

    def _refresh(self) -> None:
        self.dp._head_cache = {}
        self.dp._changed = None
        if hasattr(self.dp, "reindex"):
            try:
                self.dp.reindex()
            except Exception:  # noqa: BLE001 - the index is rebuilt on next use anyway
                pass


def _kinds(files: list[dict]) -> list[str]:
    kinds = []
    for f in files:
        p = f["file"]
        k = ("maps" if p.startswith(("data/maps/", "data/layouts/")) else
             "graphics" if p.endswith((".png", ".pal")) else
             "trainers" if p.endswith(".party") else
             "wild Pokémon" if p.endswith("wild_encounters.json") else
             "Pokémon data" if "/pokemon/" in p and p.endswith(".h") else
             "scripts" if p.endswith((".inc", ".pory")) else "code")
        if k not in kinds:
            kinds.append(k)
    return kinds


def _auto_message(changes: list[dict]) -> str:
    kinds = _kinds(changes)
    maps = sorted({Path(c["file"]).parts[2] for c in changes if c["file"].startswith("data/maps/")
                   and len(Path(c["file"]).parts) > 3})
    what = ", ".join(kinds)
    if maps:
        what += " (" + ", ".join(maps[:3]) + ("…" if len(maps) > 3 else "") + ")"
    return f"Changed {what}"

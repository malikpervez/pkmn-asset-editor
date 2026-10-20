"""Make a release of your game that you can share: a release build, BPS and UPS patches, a README and a zip.

* The game is built with ``make release``: pokeemerald-expansion then turns off the debug menus, Quickstart and
  other test features, and writes ``<name>-release.gba`` (its own build folder, so test builds are untouched).
* BPS patches copy data that only moved from where it is in the original ROM, so they stay small (a few MB for a
  whole expansion game); UPS can only store changes in place, so it is optional.
* Players need the original ROM the patch is made from (you can't share the ROM itself). The patches are made against
  the original ROM you choose; its checksum is checked against the ``*.sha1`` files of the project
  (``rom.sha1`` = Pokémon Emerald) so the README can tell players exactly which file they need.
* Everything goes into ``.pae/releases/<name>-<version>/`` plus a zip of it; nothing in the game folder changes.
* The README's "What's new" lists the save points since the last release.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
import zipfile
import zlib
from pathlib import Path

from .decomp import DecompError
from .patch import apply_bps, apply_ups, make_bps, make_ups

ROM_NAMES = {"pokeemerald.gba": "Pokémon Emerald Version (USA, Europe)",
             "pokefirered.gba": "Pokémon FireRed Version (USA) 1.0",
             "pokefirered_rev1.gba": "Pokémon FireRed Version (USA) 1.1",
             "pokeleafgreen.gba": "Pokémon LeafGreen Version (USA) 1.0",
             "pokeleafgreen_rev1.gba": "Pokémon LeafGreen Version (USA) 1.1",
             "pokeruby.gba": "Pokémon Ruby Version (USA) 1.0",
             "pokesapphire.gba": "Pokémon Sapphire Version (USA) 1.0"}
PATCHER_URL = "https://www.marcrobledo.com/RomPatcher.js/"


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-") or "game"


class Release:
    def __init__(self, dp):
        self.dp = dp
        self.root = Path(dp.root)

    @property
    def path(self) -> Path:
        return self.root / ".pae" / "release.json"

    @property
    def out_dir(self) -> Path:
        return self.root / ".pae" / "releases"

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict:
        d = {"name": self.root.name, "version": "1.0", "author": "", "description": "", "base_rom": "",
             "last_release_sha": None, "releases": []}
        if self.path.exists():
            try:
                d.update(json.loads(self.path.read_text()))
            except ValueError:
                pass
        return d

    def save_settings(self, changes: dict) -> dict:
        d = self.settings()
        for k in ("name", "version", "author", "description", "base_rom"):
            if k in changes and changes[k] is not None:
                d[k] = str(changes[k]).strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d, indent=2))
        return d

    def known_roms(self) -> dict[str, str]:
        """sha1 -> name of the original ROMs this project is a decomp of (its *.sha1 files)."""
        out = {}
        for f in sorted(self.root.glob("*.sha1")):
            for line in f.read_text(errors="replace").splitlines():
                m = re.match(r"([0-9a-fA-F]{40})\s+\*?(\S+)", line)
                if m:
                    out[m.group(1).lower()] = ROM_NAMES.get(m.group(2), m.group(2))
        return out

    def base_info(self, path: str | Path | None = None) -> dict:
        p = Path(path or self.settings().get("base_rom") or "")
        if not str(p) or not p.is_file():
            return {"path": str(p) if str(p) not in ("", ".") else "", "ok": False,
                    "problem": "choose the original ROM your patch is for (the game your hack is made from)"}
        data = p.read_bytes()
        sha1 = hashlib.sha1(data).hexdigest()
        known = self.known_roms()
        info = {"path": str(p), "ok": True, "size": len(data), "sha1": sha1, "crc32": f"{zlib.crc32(data):08X}",
                "name": known.get(sha1), "known": sha1 in known}
        if not info["known"] and known:
            info["warning"] = ("this isn't the original ROM this project is made from (" + ", ".join(known.values())
                               + "); players with the usual file won't be able to use the patch")
        return info

    def upload_base(self, data: bytes) -> dict:
        if len(data) < 1024 * 1024 or data[0xB2:0xB3] != b"\x96":
            raise DecompError("that doesn't look like a GBA ROM")
        p = self.root / ".pae" / "base.gba"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.save_settings({"base_rom": str(p)})
        return self.base_info(p)

    # ------------------------------------------------------------------ what's new
    def changelog(self) -> list[str]:
        """Save point messages since the last release (newest first)."""
        from .history import History
        h = History(self.dp)
        if not h.available():
            return []
        last = self.settings().get("last_release_sha")
        mine = h.my_emails()
        out = []
        for p in h.points(300):
            if last and p["sha"].startswith(last[:12]):
                break
            if p["email"] not in mine:  # the expansion's own history (before your game) isn't news
                continue
            if not p["message"].startswith(("Unsaved changes (kept", "Went back to save point")):
                out.append(p["message"])
        return out[:60]

    def checks(self, full: bool = False) -> list[dict]:
        """Things to know before releasing: [{level, message}]. ``full`` checks every graphic, not just your
        changes (that takes a while in a big game, so it's done when building)."""
        out = []
        base = self.base_info()
        if not base["ok"]:
            out.append({"level": "error", "message": base["problem"]})
        elif base.get("warning"):
            out.append({"level": "warning", "message": base["warning"]})
        try:
            from .history import History
            unsaved = History(self.dp).unsaved()
            if unsaved:
                out.append({"level": "warning", "message": f"{len(unsaved)} file(s) changed since your last save point; "
                            "make a save point first so this release can be found again"})
        except DecompError:
            pass
        rep = self.dp.check(only_changed=not full)
        errors = [i for i in rep.issues if i.level == "error"]
        if errors:
            out.append({"level": "error", "message": f"{len(errors)} problem(s) found by Check for problems, e.g. "
                        f"{errors[0].file}: {errors[0].message}"})
        return out

    # ------------------------------------------------------------------ build
    def make_command(self) -> list[str]:
        cmd = shlex.split(self.dp.config.get("build", {}).get("command", "make") or "make")
        if not cmd or Path(cmd[0]).name != "make":
            cmd = ["make", "-j4"]
        return cmd + ["release"]

    def release_rom(self) -> Path | None:
        roms = sorted(self.root.glob("*-release.gba"), key=lambda p: p.stat().st_mtime)
        return roms[-1] if roms else None

    def build(self, notes: str | None = None, build: bool = True, formats=("bps",), force: bool = False,
              **settings) -> dict:
        """Build the release ROM, make the patches, README and zip. -> {dir, zip, files, …}"""
        s = self.save_settings(settings) if settings else self.settings()
        problems = [c for c in self.checks(full=True) if c["level"] == "error"]
        if problems and not force:
            raise DecompError("can't make a release yet: " + "; ".join(c["message"] for c in problems))
        base = self.base_info()
        if not base["ok"]:
            raise DecompError(base["problem"])
        t0 = time.time()
        log = ""
        if build:
            r = subprocess.run(self.make_command(), cwd=self.root, capture_output=True, text=True, timeout=7200)
            log = (r.stdout + r.stderr)[-6000:]
            if r.returncode != 0:
                raise DecompError("the release build failed:\n" + log[-3000:])
        rom_path = self.release_rom()
        if rom_path is None:
            raise DecompError("no *-release.gba was built (does your Makefile have a `release` target?)")
        rom = rom_path.read_bytes()
        src = Path(base["path"]).read_bytes()
        name, version = s["name"] or self.root.name, s["version"] or "1.0"
        stem = f"{_slug(name)}-{_slug(version)}"
        out = self.out_dir / stem
        out.mkdir(parents=True, exist_ok=True)
        files = []
        meta = json.dumps({"name": name, "version": version, "author": s.get("author", "")}).encode()
        if "bps" in formats:
            patch = make_bps(src, rom, meta)
            if apply_bps(src, patch) != rom:  # check the patch really makes the game
                raise DecompError("the BPS patch didn't recreate the game (please report this)")
            (out / f"{stem}.bps").write_bytes(patch)
            files.append(f"{stem}.bps")
        if "ups" in formats:
            patch = make_ups(src, rom)
            if apply_ups(src, patch) != rom:
                raise DecompError("the UPS patch didn't recreate the game (please report this)")
            (out / f"{stem}.ups").write_bytes(patch)
            files.append(f"{stem}.ups")
        changes = [x for x in (notes.splitlines() if notes is not None else self.changelog()) if x.strip()]
        (out / "README.txt").write_text(self.readme(s, base, files, changes, rom))
        files.append("README.txt")
        zip_path = self.out_dir / f"{stem}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in files:
                z.write(out / f, f"{stem}/{f}")
        head = None
        try:
            from .history import History
            pts = History(self.dp).points(1)
            head = pts[0]["sha"] if pts else None
        except DecompError:
            pass
        d = self.settings()
        d["last_release_sha"] = head
        d["releases"] = [{"version": version, "date": time.strftime("%Y-%m-%d %H:%M"), "zip": zip_path.name,
                          "sha": head}] + [x for x in d.get("releases", []) if x.get("version") != version]
        self.path.write_text(json.dumps(d, indent=2))
        return {"dir": str(out), "zip": str(zip_path), "zip_name": zip_path.name, "files": files,
                "rom": str(rom_path), "rom_size": len(rom), "rom_crc32": f"{zlib.crc32(rom):08X}",
                "sizes": {f: (out / f).stat().st_size for f in files}, "zip_size": zip_path.stat().st_size,
                "seconds": round(time.time() - t0, 1), "build_log": log, "changes": changes}

    def readme(self, s: dict, base: dict, files: list[str], changes: list[str], rom: bytes) -> str:
        name, version = s["name"], s["version"]
        patch = next((f for f in files if f.endswith(".bps")), files[0] if files else "")
        lines = [f"{name} {version}"]
        if s.get("author"):
            lines.append(f"by {s['author']}")
        lines.append("")
        if s.get("description"):
            lines += [s["description"], ""]
        lines += ["What you need",
                  "-------------",
                  f"- Your own copy of {base.get('name') or 'the original game'} as a .gba file.",
                  f"  It must be exactly this file (CRC32 {base['crc32']}, SHA-1 {base['sha1']}),",
                  "  or the patch won't work.",
                  "",
                  "How to play",
                  "-----------",
                  f"1. Open {PATCHER_URL} (or use Flips on your computer).",
                  f"2. Choose your original ROM and the patch {patch}, then press \"Apply patch\".",
                  "3. Play the new .gba file in an emulator (like mGBA) or on a flash cart.",
                  f"   The patched game's CRC32 is {zlib.crc32(rom):08X}.",
                  ""]
        if any(f.endswith(".ups") for f in files):
            lines += ["The .ups file is the same patch for patchers that use UPS.", ""]
        if changes:
            lines += ["What's new", "----------"] + [f"- {c}" for c in changes] + [""]
        lines += ["Made with pokeemerald-expansion (https://github.com/rh-hideout/pokeemerald-expansion)",
                  "and pkmn-asset-editor.", ""]
        return "\n".join(lines)

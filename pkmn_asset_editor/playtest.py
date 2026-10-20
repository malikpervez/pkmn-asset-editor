"""Play-test a built pokeemerald-style ROM headlessly.

Uses a tiny driver around libmgba (``native/pae_mgba.c``, compiled on first
use) to boot the ROM, start a fresh game through expansion's Quickstart (press
SELECT on the title screen), and then *warp to any map* by writing the target
location into ``gSaveBlock1Ptr->location``/``pos`` and handing control to
``CB2_LoadMap`` - the same path a door warp takes, so the map's NPCs, map
scripts and music load normally. Addresses come from the ROM's ``.elf``.

After arriving you can press buttons for a number of frames and take
screenshots, which is enough to look at an edited map, walk up to an NPC and
read its text, or check a cutscene trigger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import time
from pathlib import Path

from PIL import Image

KEYS = {"A": 1, "B": 2, "SELECT": 4, "START": 8, "RIGHT": 16, "LEFT": 32, "UP": 64, "DOWN": 128, "R": 256, "L": 512}
NATIVE = Path(__file__).with_name("native") / "pae_mgba.c"


class PlaytestError(RuntimeError):
    pass


# ---------------------------------------------------------------------- ELF symbols

def elf_symbols(path: Path, sizes: dict[str, int] | None = None) -> dict[str, int]:
    """Symbol name -> address from an ELF32 little-endian .symtab (globals and file statics).
    ``sizes``, if given, is filled with each symbol's size in bytes."""
    data = Path(path).read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 1:
        raise PlaytestError(f"{path} is not a 32-bit ELF file")
    shoff, = struct.unpack_from("<I", data, 0x20)
    shentsize, shnum, _shstrndx = struct.unpack_from("<HHH", data, 0x2E)
    sections = [struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize) for i in range(shnum)]
    out: dict[str, int] = {}
    for sh in sections:
        if sh[1] != 2:  # SHT_SYMTAB
            continue
        strtab = sections[sh[6]]
        str_off = strtab[4]
        for i in range(sh[5] // 16):
            name_off, value, size, info, _other, shndx = struct.unpack_from("<IIIBBH", data, sh[4] + i * 16)
            typ, bind = info & 0xF, info >> 4
            # OBJECT / FUNC, plus NOTYPE for assembler labels (scripts, movement data) that live in sections
            if not name_off or typ not in (0, 1, 2) or (typ == 0 and (shndx == 0 or shndx >= 0xFF00)):
                continue
            end = data.index(b"\0", str_off + name_off)
            name = data[str_off + name_off:end].decode("ascii", "replace")
            if name.startswith(("$", ".L")):
                continue
            if bind == 1 or name not in out:  # GLOBAL wins over a same-named local
                out[name] = value
                if sizes is not None:
                    sizes[name] = size
    return out


# ---------------------------------------------------------------------- the emulator process

def driver_path(cache_dir: Path) -> Path:
    """Compile (once) and return the libmgba driver."""
    src = NATIVE.read_bytes()
    digest = hashlib.sha1(src).hexdigest()[:10]
    exe = cache_dir / f"pae_mgba-{digest}"
    if exe.exists():
        return exe
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if cc is None:
        raise PlaytestError("a C compiler is needed to build the emulator driver")
    cache_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([cc, "-O2", "-o", str(exe), str(NATIVE), "-lmgba"], capture_output=True, text=True)
    if r.returncode != 0:
        raise PlaytestError("could not build the emulator driver – install libmgba (Debian/Ubuntu: "
                            "`sudo apt install libmgba-dev`, macOS: `brew install mgba`).\n" + r.stderr[-1500:])
    return exe


class Emulator:
    def __init__(self, exe: Path, rom: Path, save: Path | None = None):
        args = [str(exe), str(rom)] + ([str(save)] if save else [])
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        line = self.proc.stdout.readline()
        if not line.startswith("ready"):
            raise PlaytestError("emulator failed to start: " + (line + self.proc.stderr.read())[-800:])
        _, w, h = line.split()
        self.width, self.height = int(w), int(h)
        self._tmp = Path(os.environ.get("TMPDIR", "/tmp")) / f"pae-shot-{os.getpid()}-{id(self)}.raw"

    def cmd(self, line: str) -> str:
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        out = self.proc.stdout.readline().strip()
        if not out.startswith("ok"):
            raise PlaytestError(f"emulator: {out or self.proc.stderr.read()[-500:]}")
        return out[3:]

    def frames(self, n: int, keys: int = 0) -> None:
        self.cmd(f"frames {int(n)} {int(keys)}")

    def read(self, addr: int, size: int = 4) -> int:
        return int(self.cmd(f"r{size * 8} {addr}"))

    def write(self, addr: int, value: int, size: int = 4) -> None:
        self.cmd(f"w{size * 8} {addr} {value & ((1 << (size * 8)) - 1)}")

    def screenshot(self) -> Image.Image:
        self.cmd(f"shot {self._tmp}")
        img = Image.frombytes("RGBA", (self.width, self.height), self._tmp.read_bytes())
        self._tmp.unlink(missing_ok=True)
        return img.convert("RGB")

    def save_state(self, path: Path) -> None:
        self.cmd(f"save {path}")

    def load_state(self, path: Path) -> None:
        self.cmd(f"load {path}")

    def call(self, addr: int, *args: int) -> int:
        """Run a game function (thumb address) to completion and return r0."""
        return int(self.cmd(" ".join(["call", str(addr), *(str(a & 0xFFFFFFFF) for a in args)])))

    def close(self) -> None:
        try:
            self.cmd("quit")
        except (PlaytestError, BrokenPipeError, OSError):
            pass
        self.proc.kill()


# ---------------------------------------------------------------------- game-level control

def parse_inputs(spec: str | list) -> list[tuple[int, int]]:
    """'A, wait 30, DOWN*16, A+B 4' -> [(keys, frames)].

    Each step is ``KEY[+KEY..][*FRAMES]`` or ``wait FRAMES``. A bare key is a short press
    (6 frames held, then 10 released). ``*N`` holds for N frames (16 frames = one step walking).
    """
    steps: list[tuple[int, int]] = []
    items = spec if isinstance(spec, list) else [s for s in spec.replace("\n", ",").split(",")]
    for raw in items:
        s = str(raw).strip().upper()
        if not s:
            continue
        if s.startswith("WAIT"):
            steps.append((0, int(s.split()[1] if len(s.split()) > 1 else 30)))
            continue
        name, _, count = s.partition("*")
        name, _, count2 = name.strip().partition(" ")
        count = count or count2
        mask = 0
        for k in name.split("+"):
            k = k.strip()
            if k not in KEYS:
                raise PlaytestError(f"unknown button '{k}' (use {', '.join(KEYS)})")
            mask |= KEYS[k]
        if count:
            steps.append((mask, int(count)))
        else:
            steps += [(mask, 6), (0, 10)]
    return steps


def find_rom(root: Path) -> Path:
    """The most recently built ROM in the repository that has its .elf next to it."""
    roms = sorted((p for p in Path(root).glob("*.gba") if p.with_suffix(".elf").exists()),
                  key=lambda p: p.stat().st_mtime)
    if not roms:
        raise PlaytestError("no built ROM (.gba + .elf) in the repository – run `pae build` first")
    return roms[-1]


class Game:
    """A running ROM plus the symbols needed to drive it."""

    def __init__(self, root: Path, rom: Path | None = None, cache_dir: Path | None = None, log=None,
                 save: Path | None = None):
        self.root = Path(root)
        self.rom = Path(rom) if rom else self.find_rom()
        self.save = Path(save) if save else None
        elf = self.rom.with_suffix(".elf")
        if not elf.exists():
            raise PlaytestError(f"{elf.name} not found next to {self.rom.name}; it is produced by `make`")
        self.sym_size: dict[str, int] = {}
        self.sym = elf_symbols(elf, self.sym_size)
        for need in ("gMain", "gSaveBlock1Ptr", "CB2_Overworld", "CB2_LoadMap"):
            if need not in self.sym:
                raise PlaytestError(f"symbol {need} not found in {elf.name}")
        self.cache_dir = cache_dir or self.root / ".pae" / "cache"
        self.log = log or (lambda *_: None)
        self.emu = Emulator(driver_path(self.cache_dir), self.rom, self.save)
        self.rom_hash = hashlib.sha1(self.rom.read_bytes()).hexdigest()[:12]
        self.save_hash = hashlib.sha1(self.save.read_bytes()).hexdigest()[:12] \
            if self.save and self.save.exists() and self.save.stat().st_size else "fresh"

    def find_rom(self) -> Path:
        return find_rom(self.root)

    # -- state helpers
    @property
    def callback2(self) -> int:
        return self.emu.read(self.sym["gMain"] + 4)

    def in_overworld(self) -> bool:
        return self.callback2 == self.sym["CB2_Overworld"]

    def fade_active(self) -> bool:
        pf = self.sym.get("gPaletteFade")
        if pf is None:
            return False
        # struct PaletteFadeControl: the 'active' bit lives in byte 7 (bit 7) in pokeemerald/expansion
        return bool(self.emu.read(pf + 7, 1) & 0x80)

    def wait_until(self, cond, timeout_frames: int, keys: int = 0, step: int = 4) -> bool:
        for _ in range(0, timeout_frames, step):
            if cond():
                return True
            self.emu.frames(step, keys)
        return cond()

    # -- boot
    @property
    def boot_state(self) -> Path:
        return self.cache_dir / f"boot-{self.rom_hash}-{self.save_hash}.state"

    def call(self, symbol: str, *args: int) -> int:
        if symbol not in self.sym:
            raise PlaytestError(f"function {symbol} not found in the .elf")
        return self.emu.call(self.sym[symbol], *args)

    def boot(self) -> None:
        """Reach the overworld: Quickstart for a fresh game, Continue when a save is loaded (cached)."""
        state = self.boot_state
        if state.exists():
            try:
                self.emu.load_state(state)
                if self.in_overworld():
                    return
            except PlaytestError:
                pass
            state.unlink(missing_ok=True)
            self.emu.close()
            self.emu = Emulator(driver_path(self.cache_dir), self.rom, self.save)
        t0 = time.time()
        if self.save_hash == "fresh":
            # SELECT pulses: skip the intros, then Quickstart on the title screen.
            key, why = KEYS["SELECT"], ("could not reach the overworld. Quickstart (SELECT on the title screen) must "
                                        "be enabled: ENABLE_QUICKSTART in include/config/quickstart.h, non-release build")
        else:
            # A pulses: skip the intros, title -> main menu -> CONTINUE.
            key, why = KEYS["A"], ("could not continue from this save. It may come from a build with a different "
                                   "save layout, or be empty/corrupt.")
        for _ in range(1500):
            if self.in_overworld():
                break
            self.emu.frames(2, key)
            self.emu.frames(8, 0)
        else:
            raise PlaytestError(why)
        self.emu.frames(240)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.emu.save_state(state)
        self.log(f"booted into the overworld in {time.time() - t0:.1f}s (cached)")

    # -- warp
    def warp(self, group: int, num: int, x: int, y: int) -> None:
        sb1 = self.emu.read(self.sym["gSaveBlock1Ptr"])
        if not 0x02000000 <= sb1 < 0x04000000:
            raise PlaytestError("save block pointer looks invalid; is the game running?")
        # SaveBlock1: pos (s16 x, y) at 0x00, location (s8 group, s8 num, s8 warpId, pad, s16 x, s16 y) at 0x04
        self.emu.write(sb1 + 0, x, 2)
        self.emu.write(sb1 + 2, y, 2)
        self.emu.write(sb1 + 4, group, 1)
        self.emu.write(sb1 + 5, num, 1)
        self.emu.write(sb1 + 6, 0xFF, 1)  # WARP_ID_NONE: use x/y
        self.emu.write(sb1 + 8, x, 2)
        self.emu.write(sb1 + 10, y, 2)
        self.emu.write(self.sym["gMain"] + 4, self.sym["CB2_LoadMap"])
        self.emu.frames(2)
        if not self.wait_until(self.in_overworld, 900):
            raise PlaytestError("the map did not finish loading")
        self.wait_until(lambda: not self.fade_active(), 240)
        self.emu.frames(30)

    def play(self, steps: list[tuple[int, int]], shots_every_step: bool = False) -> list[Image.Image]:
        shots = []
        for keys, n in steps:
            self.emu.frames(n, keys)
            if shots_every_step and keys:
                shots.append(self.emu.screenshot())
        return shots

    def close(self) -> None:
        self.emu.close()


def map_numbers(root: Path) -> dict[str, tuple[int, int]]:
    """Map name -> (group, num), exactly as mapjson numbers them from map_groups.json."""
    groups = json.loads((Path(root) / "data" / "maps" / "map_groups.json").read_text())
    out = {}
    for gi, g in enumerate(groups.get("group_order", [])):
        for mi, name in enumerate(groups.get(g, [])):
            out[name] = (gi, mi)
    return out


def spawn_point(map_json: dict, x: int | None, y: int | None) -> tuple[int, int]:
    if x is not None and y is not None:
        return int(x), int(y)
    warps = map_json.get("warp_events") or []
    if warps:
        return int(warps[0]["x"]), int(warps[0]["y"])
    return 2, 2


class PlaySession:
    """A long-lived play-test: start at a map (fresh game or from a checkpoint save), set story state,
    press buttons, run scripts, save checkpoints, look at the screen."""

    def __init__(self, root: Path, cache_dir: Path | None = None, log=None):
        self.root = Path(root)
        self.cache_dir = cache_dir or self.root / ".pae" / "cache"
        self.checkpoint_dir = self.root / ".pae" / "checkpoints"
        self.log = log or (lambda *_: None)
        self.game: Game | None = None
        self._stamp = None
        self.where: dict = {}
        self.scratch = self.cache_dir / "session.sav"
        self._constants: Constants | None = None

    # -- checkpoints (cartridge saves kept by name)
    def checkpoints(self) -> list[dict]:
        out = []
        for meta in sorted(self.checkpoint_dir.glob("*.json")):
            try:
                d = json.loads(meta.read_text())
            except ValueError:
                continue
            d["name"] = meta.stem
            out.append(d)
        return out

    def _checkpoint_path(self, name: str) -> Path:
        if not name or not all(c.isalnum() or c in "-_ " for c in name):
            raise PlaytestError("checkpoint names may use letters, digits, spaces, - and _")
        return self.checkpoint_dir / f"{name}.sav"

    def import_save(self, src, name: str, note: str = "") -> dict:
        data = Path(src).read_bytes()
        if len(data) not in (0x10000, 0x10010, 0x20000, 0x20010):
            raise PlaytestError(f"{src} is {len(data)} bytes; a Gen 3 flash save is 64 or 128 KiB")
        dst = self._checkpoint_path(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        meta = {"note": note or f"imported from {Path(src).name}", "created": time.strftime("%Y-%m-%d %H:%M"),
                "source": "import"}
        dst.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        return {"name": name, **meta}

    def delete_checkpoint(self, name: str) -> None:
        p = self._checkpoint_path(name)
        if not p.exists():
            raise PlaytestError(f"no checkpoint named {name}")
        p.unlink()
        p.with_suffix(".json").unlink(missing_ok=True)

    def save_checkpoint(self, name: str, note: str = "") -> dict:
        """Save the running game exactly like the in-game Save, and keep the save as a named checkpoint."""
        g = self._require()
        if not g.in_overworld():
            raise PlaytestError("checkpoints can only be saved while walking around in the overworld")
        if "SaveMapView" in g.sym:
            g.call("SaveMapView")
        status = g.call("TrySavingData", 0)  # SAVE_NORMAL
        g.emu.frames(30)  # let the emulator flush the flash writes to the save file
        if status != 1:
            raise PlaytestError(f"the game reported a save error (status {status})")
        dst = self._checkpoint_path(name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.scratch, dst)
        where = self.location()
        meta = {"note": note, "created": time.strftime("%Y-%m-%d %H:%M"), "source": "play-test",
                "map": where.get("map"), "x": where.get("x"), "y": where.get("y"), "rom": g.rom.name}
        dst.with_suffix(".json").write_text(json.dumps(meta, indent=2))
        return {"name": name, **meta}

    def location(self) -> dict:
        g = self._require()
        sb1 = g.emu.read(g.sym["gSaveBlock1Ptr"])
        grp, num = g.emu.read(sb1 + 4, 1), g.emu.read(sb1 + 5, 1)
        names = {v: k for k, v in map_numbers(self.root).items()}
        return {"map": names.get((grp, num)), "x": g.emu.read(sb1, 2), "y": g.emu.read(sb1 + 2, 2)}

    # -- story state
    @property
    def constants(self) -> "Constants":
        if self._constants is None:
            self._constants = Constants(self.root, self.cache_dir)
        return self._constants

    def apply_state(self, ops) -> list[str]:
        """Story state lines (see :func:`parse_state`): flags, vars, items and Pokémon for the team."""
        g = self._require()
        parsed = parse_state(ops)
        names = [name for kind, name, _ in parsed if name]
        names += [x for kind, _, arg in parsed if kind == "mon" for x in [arg["item"], *arg["moves"]] if x]
        values = self.constants.values(list(dict.fromkeys(names)), game_version(g.rom))
        done = []
        for kind, name, arg in parsed:
            if kind == "clear_team":
                self.clear_team()
                done.append("emptied the team")
                continue
            if kind == "mon":
                done.append(self.give_mon(values[name], arg["level"], values.get(arg["item"]) if arg["item"] else 0,
                                          [values[mv] for mv in arg["moves"]], name))
                continue
            v = values[name]
            if kind == "flag":
                g.call("FlagSet" if arg else "FlagClear", v)
                done.append(f"{'set' if arg else 'cleared'} {name}")
            elif kind == "var":
                g.call("VarSet", v, arg)
                done.append(f"{name} = {arg}")
            else:
                if g.call("AddBagItem", v, arg) & 0xFF:
                    done.append(f"gave {arg} x {name}")
                else:
                    done.append(f"couldn't fit {arg} x {name} in the bag (that pocket is full)")
        return done

    # -- the team
    def _party(self) -> tuple[int, int]:
        """(address of gPlayerParty, size of one struct Pokemon)."""
        g = self._require()
        if "gPlayerParty" in g.sym:  # pokeemerald
            return g.sym["gPlayerParty"], g.sym_size.get("gPlayerParty", 600) // 6
        if "gPlayerPartyPtr" in g.sym:  # expansion: gParties[trainer][6], player first, then opponent A
            base = g.emu.read(g.sym["gPlayerPartyPtr"])
            size = 0
            if "gEnemyPartyPtr" in g.sym:
                size = (g.emu.read(g.sym["gEnemyPartyPtr"]) - base) // 6
            if not 60 <= size <= 200 and "gParties" in g.sym:
                size = g.sym_size.get("gParties", 0) // 24
            if 60 <= size <= 200:
                return base, size
        raise PlaytestError("couldn't find the player's team (gPlayerParty) in the .elf")

    def clear_team(self) -> None:
        g = self._require()
        g.call("ZeroPlayerPartyMons")
        g.call("CalculatePlayerPartyCount")

    def give_mon(self, species: int, level: int, item: int = 0, moves: list[int] | None = None,
                 label: str = "") -> str:
        """Add a Pokémon to the team (or the PC when the team is full), like the givemon script command."""
        g = self._require()
        if "ScriptGiveMon" not in g.sym:
            raise PlaytestError("ScriptGiveMon not found in the .elf")
        before = g.call("CalculatePlayerPartyCount") & 0xFF
        res = g.call("ScriptGiveMon", species, level, item) & 0xFF
        if res == 2:
            raise PlaytestError(f"could not give {label or species}: the team and the PC are full")
        where = "the team" if res == 0 else "the PC (the team is full)"
        if moves and res == 0:
            base, size = self._party()
            mon = base + before * size
            for slot in range(4):
                g.call("SetMonMoveSlot", mon, moves[slot] if slot < len(moves) else 0, slot)
        try:  # the start menu only shows POKéMON once this is set
            g.call("FlagSet", self._mon_field("FLAG_SYS_POKEMON_GET"))
        except PlaytestError:
            pass
        return f"added {label or species} lv{level} to {where}"

    # -- battles
    def start_battle(self, trainer: str | None = None, species: str | None = None, level: int = 5,
                     item: str | None = None, double: bool = False, frames: int = 240) -> Image.Image:
        """Start a battle right now: against ``trainer`` (TRAINER_X, using its team from trainers.party) or a wild
        ``species`` at ``level``. The player uses their current team."""
        g = self._require()
        if not g.in_overworld():
            raise PlaytestError("battles can only be started while walking around in the overworld")
        if self.clear_team_needed():
            raise PlaytestError("you have no Pokémon: add some to your team first")
        version = game_version(g.rom)
        if trainer:
            tid = trainer.upper() if trainer.upper().startswith("TRAINER_") else "TRAINER_" + trainer.upper()
            need = ["BattleSetup_StartTrainerBattle_Debug", "CreateNPCTrainerPartyFromTrainer", "gTrainers",
                    "gTrainerBattleParameter", "gBattleTypeFlags", "CalculateEnemyPartyCount"]
            missing = [n for n in need if n not in g.sym]
            if missing:
                raise PlaytestError(f"this build can't start trainer battles directly (missing {', '.join(missing)}; "
                                    "it needs pokeemerald-expansion)")
            v = self.constants.values([tid, "TRAINERS_COUNT", "DIFFICULTY_COUNT"], version)
            n_tr, n_diff = v["TRAINERS_COUNT"], max(1, v["DIFFICULTY_COUNT"])
            size = g.sym_size.get("gTrainers", 0) // (n_tr * n_diff)
            if size <= 0 or not 0 <= v[tid] < n_tr:
                raise PlaytestError(f"{tid} is not a regular trainer (partners and debug trainers can't be battled here)")
            diff = g.call("GetTrainerDifficultyLevel", v[tid]) & 0xFF if "GetTrainerDifficultyLevel" in g.sym else 0
            tr_ptr = g.sym["gTrainers"] + (min(diff, n_diff - 1) * n_tr + v[tid]) * size
            param = g.sym["gTrainerBattleParameter"]
            for i in range(g.sym_size.get("gTrainerBattleParameter", 40)):
                g.emu.write(param + i, 0, 1)
            g.emu.write(param + 2, v[tid] & 0xFF, 1)       # params.opponentA (packed struct)
            g.emu.write(param + 3, v[tid] >> 8, 1)
            g.emu.write(param + 17, 0xFF, 1)                # params.opponentB = 0xFFFF
            g.emu.write(param + 18, 0xFF, 1)
            g.emu.write(g.sym["gBattleTypeFlags"], (1 << 3) | (1 if double else 0), 4)  # BATTLE_TYPE_TRAINER (+DOUBLE)
            base, mon_size = self._party()
            g.call("CreateNPCTrainerPartyFromTrainer", base + 6 * mon_size, tr_ptr)
            if "BattleSetup_GetEnvironmentId" in g.sym and "gBattleEnvironment" in g.sym:
                g.emu.write(g.sym["gBattleEnvironment"], g.call("BattleSetup_GetEnvironmentId") & 0xFF, 1)
            g.call("CalculateEnemyPartyCount")
            g.call("BattleSetup_StartTrainerBattle_Debug")
            what = tid
        elif species:
            sp = species.upper() if species.upper().startswith("SPECIES_") else "SPECIES_" + species.upper()
            names = [sp] + ([item] if item else [])
            v = self.constants.values(names, version)
            if not 1 <= int(level) <= 100:
                raise PlaytestError("level must be 1-100")
            g.call("CreateScriptedWildMon", v[sp], int(level), v.get(item, 0) if item else 0)
            g.call("BattleSetup_StartWildBattle")
            what = f"a wild {sp} lv{level}"
        else:
            raise PlaytestError("give a trainer, or a species for a wild battle")
        g.emu.frames(frames)
        self.where = {**self.where, "battle": what}
        return g.emu.screenshot()

    def clear_team_needed(self) -> bool:
        g = self._require()
        return (g.call("CalculatePlayerPartyCount") & 0xFF) == 0

    def in_battle(self) -> bool:
        g = self._require()
        return not g.in_overworld() and g.sym.get("BattleMainCB2") == g.callback2

    def team(self) -> list[dict]:
        """The current team: species / level / held item / moves, as numbers (see team_names for names)."""
        g = self._require()
        base, size = self._party()
        n = g.call("CalculatePlayerPartyCount") & 0xFF
        out = []
        for i in range(min(n, 6)):
            mon = base + i * size
            fn = "GetMonData2" if "GetMonData2" in g.sym else "GetMonData"
            get = lambda field: g.call(fn, mon, field, 0)
            out.append({"slot": i, "species": get(self._mon_field("MON_DATA_SPECIES")),
                        "level": get(self._mon_field("MON_DATA_LEVEL")),
                        "item": get(self._mon_field("MON_DATA_HELD_ITEM")),
                        "moves": [get(self._mon_field(f"MON_DATA_MOVE{k}")) for k in range(1, 5)]})
        return out

    def _mon_field(self, name: str) -> int:
        return self.constants.values([name], game_version(self._require().rom))[name]

    def run_script(self, label: str, frames: int = 30) -> Image.Image:
        """Start any script label (an event, cutscene, sign…) right now, as if it had been triggered."""
        g = self._require()
        if label not in g.sym:
            raise PlaytestError(f"script {label} is not in the built ROM (is it defined, and built?)")
        if not g.in_overworld():
            raise PlaytestError("scripts can only be started in the overworld")
        g.call("ScriptContext_SetupScript", g.sym[label])
        g.emu.frames(frames)
        return g.emu.screenshot()

    # -- session
    def _require(self) -> Game:
        if self.game is None:
            raise PlaytestError("start a play-test first")
        return self.game

    def _game(self, checkpoint: str | None) -> Game:
        rom = find_rom(self.root)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if checkpoint:
            src = self._checkpoint_path(checkpoint)
            if not src.exists():
                raise PlaytestError(f"no checkpoint named {checkpoint}")
            data = src.read_bytes()
        else:
            data = b""
        stamp = (str(rom), rom.stat().st_mtime, hashlib.sha1(data).hexdigest())
        if self.game is None or stamp != self._stamp:
            if self.game is not None:
                self.game.close()
            self.scratch.write_bytes(data)
            self.game = Game(self.root, rom, self.cache_dir, self.log, save=self.scratch)
            self._stamp = stamp
        return self.game

    def start(self, map_name: str | None = None, x: int | None = None, y: int | None = None,
              checkpoint: str | None = None, state=None, script: str | None = None) -> Image.Image:
        """Start a play-test.

        * fresh game (Quickstart), or ``checkpoint`` = continue from a saved checkpoint (story progress, party…)
        * ``state``: story state applied before arriving (see :meth:`apply_state`)
        * ``map_name``: warp there at (x, y) (default its first warp); with a checkpoint and no map, stay
          where the save was made
        * ``script``: start this script label once there
        """
        nums = map_numbers(self.root)
        if map_name is not None and map_name not in nums:
            raise PlaytestError(f"{map_name} is not in map_groups.json (rebuild after adding maps)")
        if map_name is None and not checkpoint:
            raise PlaytestError("give a map, or a checkpoint to continue from")
        g = self._game(checkpoint)
        g.boot()
        g.emu.load_state(g.boot_state)  # same starting state every time
        notes = self.apply_state(state) if state else []
        if map_name is not None:
            mj = json.loads((self.root / "data" / "maps" / map_name / "map.json").read_text())
            x, y = spawn_point(mj, x, y)
            g.warp(*nums[map_name], x, y)
        elif notes:
            # reload the current map so NPCs and map scripts see the new flags/vars
            here = self.location()
            g.warp(*nums[here["map"]], here["x"], here["y"])
        img = run = None
        if script:
            img = self.run_script(script)
            run = script
        where = self.location()
        self.where = {**where, "rom": g.rom.name, "checkpoint": checkpoint, "state": notes, "script": run}
        return img or g.emu.screenshot()

    def press(self, inputs, record_every: int = 0) -> tuple[Image.Image, list[Image.Image]]:
        """Run input steps; returns the final screen and (optionally) frames sampled every N frames."""
        if self.game is None:
            raise PlaytestError("start a play-test first")
        frames: list[Image.Image] = []
        for keys, n in parse_inputs(inputs):
            if record_every:
                left = n
                while left > 0:
                    k = min(record_every, left)
                    self.game.emu.frames(k, keys)
                    left -= k
                    frames.append(self.game.emu.screenshot())
            else:
                self.game.emu.frames(n, keys)
        return self.game.emu.screenshot(), frames

    def screenshot(self) -> Image.Image:
        if self.game is None:
            raise PlaytestError("start a play-test first")
        return self.game.emu.screenshot()

    def close(self) -> None:
        if self.game is not None:
            self.game.close()
            self.game = None


def save_gif(frames: list[Image.Image], path, scale: int = 2, ms: int = 66) -> None:
    if not frames:
        return
    big = [f.resize((f.width * scale, f.height * scale), Image.NEAREST) for f in frames]
    big[0].save(path, save_all=True, append_images=big[1:], duration=ms, loop=0)


def strip(frames: list[Image.Image], max_frames: int = 6) -> Image.Image:
    """A horizontal strip of evenly spaced frames (for AI tools that take one image)."""
    if not frames:
        raise PlaytestError("no frames")
    step = max(1, len(frames) // max_frames)
    pick = frames[::step][:max_frames]
    if pick[-1] is not frames[-1]:
        pick[-1] = frames[-1]
    out = Image.new("RGB", (sum(f.width for f in pick) + 4 * (len(pick) - 1), pick[0].height), (20, 20, 24))
    x = 0
    for f in pick:
        out.paste(f, (x, 0))
        x += f.width + 4
    return out


# ---------------------------------------------------------------------- constants & story state

GAME_VERSIONS = {"BPE": "EMERALD", "BPR": "FIRERED", "BPG": "LEAFGREEN", "AXV": "RUBY", "AXP": "SAPPHIRE"}


def game_version(rom: Path) -> str:
    with open(rom, "rb") as fh:
        fh.seek(0xAC)
        code = fh.read(3).decode("ascii", "replace")
    return GAME_VERSIONS.get(code, "EMERALD")


def parse_state(ops) -> list[tuple[str, str, object]]:
    """Story state lines -> [(kind, name, arg)].

    * ``FLAG_BADGE01_GET`` (set), ``!FLAG_HIDE_X`` (clear), ``VAR_STORY=3``, ``ITEM_POTION*5`` (give)
    * ``SPECIES_MUDKIP lv20 ITEM_ORAN_BERRY MOVE_TACKLE MOVE_WATER_GUN`` (add to the team: level, held item and up
      to 4 moves are optional), ``CLEAR_TEAM`` (empty the team first)
    """
    items = ops if isinstance(ops, list) else re.split(r"[,\n]", ops or "")
    out: list[tuple[str, str, object]] = []
    for raw in items:
        s = str(raw).strip()
        if not s or s.startswith("#"):
            continue
        if s.upper() in ("CLEAR_TEAM", "!TEAM", "CLEAR_PARTY"):
            out.append(("clear_team", "", 0))
            continue
        if re.match(r"SPECIES_\w+", s, re.I):
            out.append(("mon", *_parse_mon(s)))
            continue
        m = re.fullmatch(r"(!?)\s*(FLAG_\w+)(?:\s*=\s*(\d+|true|false))?", s, re.I)
        if m:
            on = not m.group(1) and (m.group(3) or "1").lower() not in ("0", "false")
            out.append(("flag", m.group(2).upper(), int(on)))
            continue
        m = re.fullmatch(r"(VAR_\w+)\s*=\s*(-?\w+)", s, re.I)
        if m:
            out.append(("var", m.group(1).upper(), int(m.group(2), 0) & 0xFFFF))
            continue
        m = re.fullmatch(r"(ITEM_\w+)(?:\s*[*x]\s*(\d+))?", s, re.I)
        if m:
            out.append(("item", m.group(1).upper(), int(m.group(2) or 1)))
            continue
        raise PlaytestError(f"can't read '{s}'. Write one of: FLAG_X (turn a flag on), !FLAG_X (turn it off), "
                            "VAR_X=5, ITEM_X*3 (give items), SPECIES_X lv20 (add a Pokémon), CLEAR_TEAM")
    return out


def _parse_mon(line: str) -> tuple[str, dict]:
    """'SPECIES_MUDKIP lv20 ITEM_ORAN_BERRY MOVE_TACKLE' -> ('SPECIES_MUDKIP', {level, item, moves})."""
    tokens = re.split(r"[\s,;]+", line.strip())
    species = tokens[0].upper()
    mon = {"level": 5, "item": None, "moves": []}
    i = 1
    while i < len(tokens):
        t = tokens[i]
        low = t.lower()
        if low in ("lv", "lvl", "level", "l") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            mon["level"] = int(tokens[i + 1])
            i += 2
            continue
        m = re.fullmatch(r"(?:lv|lvl|level|l)?\.?(\d+)", low)
        if m:
            mon["level"] = int(m.group(1))
        elif low in ("holding", "with", "moves", "knows", "@"):
            pass
        elif t.upper().startswith("ITEM_"):
            mon["item"] = t.upper()
        elif t.upper().startswith("MOVE_"):
            if len(mon["moves"]) == 4:
                raise PlaytestError(f"{species} can know at most 4 moves")
            mon["moves"].append(t.upper())
        else:
            raise PlaytestError(f"can't read '{t}' in '{line}'. Example: SPECIES_MUDKIP lv20 ITEM_ORAN_BERRY "
                                "MOVE_TACKLE MOVE_WATER_GUN")
        i += 1
    if not 1 <= mon["level"] <= 100:
        raise PlaytestError(f"{species}: level must be 1-100")
    return species, mon


class Constants:
    """Values of FLAG_/VAR_/ITEM_ (any C constant) as *your* headers define them, by compiling a tiny file
    with the project's own toolchain, so renumbered or added constants in a fork are always right."""

    HEADERS = ("global.h", "constants/flags.h", "constants/vars.h", "constants/items.h", "constants/species.h",
               "constants/moves.h", "pokemon.h", "constants/opponents.h", "constants/difficulty.h")

    def __init__(self, root: Path, cache_dir: Path):
        self.root = Path(root)
        self.cache_file = Path(cache_dir) / "constants.json"
        self._cache: dict | None = None

    def _signature(self) -> str:
        files = list((self.root / "include" / "constants").glob("*.h")) + list((self.root / "include" / "config").glob("*.h"))
        return str(max((f.stat().st_mtime for f in files), default=0))

    def values(self, names: list[str], version: str = "EMERALD") -> dict[str, int]:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_file.read_text())
            except (OSError, ValueError):
                self._cache = {}
        sig = f"{self._signature()}:{version}"
        if self._cache.get("_sig") != sig:
            self._cache = {"_sig": sig}
        missing = [n for n in dict.fromkeys(names) if n not in self._cache]
        if missing:
            self._cache.update(self._compile(missing, version))
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(self._cache))
        return {n: self._cache[n] for n in names}

    def _compile(self, names: list[str], version: str) -> dict[str, int]:
        cc = shutil.which("arm-none-eabi-gcc")
        if cc is None:
            raise PlaytestError("arm-none-eabi-gcc is needed to read constant values (see `pae doctor`)")
        for n in names:
            if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", n):
                raise PlaytestError(f"not a constant name: {n}")
        work = self.cache_file.parent / "consts"
        work.mkdir(parents=True, exist_ok=True)
        src = work / "pae_consts.c"
        includes = "".join(f'#include "{h}"\n' for h in self.HEADERS if (self.root / "include" / h).exists())
        src.write_text(includes + "const unsigned int pae_v[] = {\n" + "".join(f"    {n},\n" for n in names) + "};\n")
        # the same target flags the repository's Makefile uses (apcs-gnu changes enum sizes)
        cmd = [cc, "-mthumb", "-mthumb-interwork", "-mabi=apcs-gnu", "-mtune=arm7tdmi", "-march=armv4t",
               "-std=gnu17", "-iquote", str(self.root / "include"), "-Wno-trigraphs", "-DMODERN=1",
               "-DTESTING=0", f"-D{version}", "-O0", "-w", "-S", "-o", "-", str(src)]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root)
        if r.returncode != 0:
            bad = sorted(set(re.findall(r"'([A-Z_][A-Z0-9_]*)' undeclared", r.stderr)))
            if bad:
                raise PlaytestError(f"unknown constant(s): {', '.join(bad)}")
            raise PlaytestError("could not evaluate constants:\n" + r.stderr[-1200:])
        body = r.stdout.split("pae_v:", 1)[-1]
        vals = [int(v, 0) for v in re.findall(r"\.(?:word|4byte|long)\s+(-?\w+)", body)][: len(names)]
        if len(vals) != len(names):
            raise PlaytestError("could not read the compiled constant values")
        return dict(zip(names, vals))

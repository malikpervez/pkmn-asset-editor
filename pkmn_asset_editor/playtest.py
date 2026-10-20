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

def elf_symbols(path: Path) -> dict[str, int]:
    """Symbol name -> address from an ELF32 little-endian .symtab (globals and file statics)."""
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
            name_off, value, _size, info, _other, _shndx = struct.unpack_from("<IIIBBH", data, sh[4] + i * 16)
            if not name_off or (info & 0xF) not in (1, 2):  # OBJECT / FUNC
                continue
            end = data.index(b"\0", str_off + name_off)
            name = data[str_off + name_off:end].decode("ascii", "replace")
            out.setdefault(name, value)
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
    def __init__(self, exe: Path, rom: Path):
        self.proc = subprocess.Popen([str(exe), str(rom)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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

    def __init__(self, root: Path, rom: Path | None = None, cache_dir: Path | None = None, log=None):
        self.root = Path(root)
        self.rom = Path(rom) if rom else self.find_rom()
        elf = self.rom.with_suffix(".elf")
        if not elf.exists():
            raise PlaytestError(f"{elf.name} not found next to {self.rom.name}; it is produced by `make`")
        self.sym = elf_symbols(elf)
        for need in ("gMain", "gSaveBlock1Ptr", "CB2_Overworld", "CB2_LoadMap"):
            if need not in self.sym:
                raise PlaytestError(f"symbol {need} not found in {elf.name}")
        self.cache_dir = cache_dir or self.root / ".pae" / "cache"
        self.log = log or (lambda *_: None)
        self.emu = Emulator(driver_path(self.cache_dir), self.rom)
        self.rom_hash = hashlib.sha1(self.rom.read_bytes()).hexdigest()[:12]

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
    def boot(self) -> None:
        """Reach the overworld in a fresh game (cached as a savestate per ROM)."""
        state = self.cache_dir / f"boot-{self.rom_hash}.state"
        if state.exists():
            try:
                self.emu.load_state(state)
                if self.in_overworld():
                    return
            except PlaytestError:
                pass
            state.unlink(missing_ok=True)
            self.emu.close()
            self.emu = Emulator(driver_path(self.cache_dir), self.rom)
        t0 = time.time()
        # Press SELECT in pulses: skips the intros, then triggers Quickstart on the title screen.
        for _ in range(900):
            if self.in_overworld():
                break
            self.emu.frames(2, KEYS["SELECT"])
            self.emu.frames(8, 0)
        else:
            raise PlaytestError("could not reach the overworld. Quickstart (SELECT on the title screen) must be "
                                "enabled: ENABLE_QUICKSTART in include/config/quickstart.h, non-release build")
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
    """A long-lived play-test: start at a map, press buttons, look at the screen."""

    def __init__(self, root: Path, cache_dir: Path | None = None, log=None):
        self.root = Path(root)
        self.cache_dir = cache_dir or self.root / ".pae" / "cache"
        self.log = log or (lambda *_: None)
        self.game: Game | None = None
        self._rom_stamp = None
        self.where: dict = {}

    def _game(self) -> Game:
        rom = find_rom(self.root)
        stamp = (str(rom), rom.stat().st_mtime)
        if self.game is None or stamp != self._rom_stamp:
            if self.game is not None:
                self.game.close()
            self.game = Game(self.root, rom, self.cache_dir, self.log)
            self._rom_stamp = stamp
        return self.game

    def start(self, map_name: str, x: int | None = None, y: int | None = None) -> Image.Image:
        """Fresh game state, warped to ``map_name`` at (x, y) (default: its first warp)."""
        nums = map_numbers(self.root)
        if map_name not in nums:
            raise PlaytestError(f"{map_name} is not in map_groups.json (rebuild after adding maps)")
        mj = json.loads((self.root / "data" / "maps" / map_name / "map.json").read_text())
        x, y = spawn_point(mj, x, y)
        g = self._game()
        g.boot()
        g.emu.load_state(g.cache_dir / f"boot-{g.rom_hash}.state")  # same starting state every time
        g.warp(*nums[map_name], x, y)
        self.where = {"map": map_name, "x": x, "y": y, "rom": g.rom.name}
        return g.emu.screenshot()

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

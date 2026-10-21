"""Game data editing for pokeemerald-expansion: trainer teams, wild Pokémon and Pokémon (species) data.

* **Trainers** live in ``src/data/trainers.party`` (Showdown-style text, turned into C by ``tools/trainerproc``):
  ``=== TRAINER_X ===`` blocks with ``Key: value`` lines, then one paragraph per Pokémon
  (``Nickname (Species) (F) @ Item``, ``Level: 12``, other ``Key: value`` lines, ``- Move`` lines).
  Edits rewrite only the trainer's own block.
* **Wild Pokémon** live in ``src/data/wild_encounters.json``: per map, ``land_mons`` / ``water_mons`` /
  ``rock_smash_mons`` / ``fishing_mons`` with an encounter rate and one entry per slot; the chance of each slot is in
  the group's ``fields``.
* **Pokémon** are ``[SPECIES_X] = { .baseHP = 45, ... }`` entries in ``src/data/pokemon/species_info/*.h`` (with
  ``#if`` branches for config options, evaluated here like the compiler would) and level-up learnsets in
  ``src/data/pokemon/level_up_learnsets/gen_N.h`` (the file ``P_LVL_UP_LEARNSETS`` selects).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .decomp import DecompError, strip_comments

PARTY_FILES = ("src/data/trainers.party", "src/data/trainers_frlg.party", "src/data/battle_partners.party")


def built_party_files(root: Path) -> list[str]:
    """The .party files the game is built from: those whose generated header the C code includes (directly, or
    through a make rule like `trainers_firered_johto.h: trainers_hns.h ... trainers_hns.party`). Falls back to the
    usual names when that can't be told."""
    root = Path(root)
    parties = sorted(str(p.relative_to(root)) for p in (root / "src" / "data").glob("*.party"))
    if not parties:
        return []
    included = set()
    for f in (root / "src").rglob("*.c"):
        included |= set(re.findall(r'^\s*#\s*include\s+"data/(\w+)\.h"', f.read_text(errors="replace"), re.M))
    rules = "\n".join(f.read_text(errors="replace") for f in list(root.glob("*.mk")) + [root / "Makefile"] if f.exists())
    used = set()
    for rel in parties:
        stem = Path(rel).stem
        if stem in included:
            used.add(rel)
        for m in re.finditer(rf"^src/data/(\w+)\.h\s*:[^\n]*\b{re.escape(rel)}\b", rules, re.M):
            if m.group(1) in included:
                used.add(rel)
    return [p for p in parties if p in used] or [f for f in PARTY_FILES if (root / f).exists()]
WILD_FILE = "src/data/wild_encounters.json"
SPECIES_DIR = "src/data/pokemon/species_info"
LEARNSET_DIR = "src/data/pokemon/level_up_learnsets"
ITEMS_FILE = "src/data/items.h"
MOVES_FILE = "src/data/moves_info.h"
TMS_FILE = "include/constants/tms_hms.h"
LEARNABLES_FILE = "src/data/pokemon/all_learnables.json"
SPECIAL_MOVESETS = "src/data/pokemon/special_movesets.json"
TEACHABLE_FILE = "src/data/pokemon/teachable_learnsets.h"
EGG_FILE = "src/data/pokemon/egg_moves.h"
NAME_LENGTHS = {"SPECIES_": ("POKEMON_NAME_LENGTH", 12), "ITEM_": ("ITEM_NAME_LENGTH", 20),
                "MOVE_": ("MOVE_NAME_LENGTH", 16)}
MOVE_NUMBERS = {"power": (0, 255), "accuracy": (0, 100), "pp": (1, 64), "priority": (-7, 7)}
WILD_KINDS = {"land_mons": "Grass & caves", "water_mons": "Surfing", "rock_smash_mons": "Rock Smash",
              "fishing_mons": "Fishing"}
STATS = ("baseHP", "baseAttack", "baseDefense", "baseSpAttack", "baseSpDefense", "baseSpeed")
STAT_NAMES = {"baseHP": "HP", "baseAttack": "Attack", "baseDefense": "Defense", "baseSpAttack": "Sp. Atk",
              "baseSpDefense": "Sp. Def", "baseSpeed": "Speed"}
NUMBER_FIELDS = STATS + ("catchRate", "expYield")
MON_KEY_ORDER = ("Level", "Ability", "Nature", "IVs", "EVs", "Ball", "Happiness", "Shiny")


def const_of(prefix: str, name: str) -> str:
    """A name as tools/trainerproc turns it into a constant ("Rock Tomb" -> MOVE_ROCK_TOMB)."""
    name = name.strip()
    if re.fullmatch(rf"{prefix}_[A-Z0-9_]+", name):
        return name
    out = []
    for c in name:
        if c.isascii() and c.isalnum():
            out.append(c.upper())
        elif c == "'":
            continue
        else:
            out.append("_")
    return f"{prefix}_" + ("".join(out) or "NONE")


def pretty(const: str) -> str:
    return re.sub(r"^[A-Z]+_", "", const or "").replace("_", " ").title()


# ---------------------------------------------------------------------- a small C preprocessor for #if blocks
def _cpp_value(expr: str, defines: dict[str, str], depth: int = 0) -> int:
    expr = expr.strip()
    expr = re.sub(r"defined\s*\(\s*(\w+)\s*\)|defined\s+(\w+)",
                  lambda m: "1" if (m.group(1) or m.group(2)) in defines else "0", expr)

    def sub(m):
        name = m.group(0)
        if name in defines and depth < 12:
            v = defines[name].strip() or "1"
            return f"({_cpp_value(v, defines, depth + 1)})"
        return "1" if name == "TRUE" else "0"
    expr = re.sub(r"\b[A-Za-z_]\w*\b", sub, expr)
    expr = expr.replace("&&", " and ").replace("||", " or ")
    expr = re.sub(r"!(?!=)", " not ", expr)
    expr = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)[uUlL]*\b", r"\1", expr)
    if not re.fullmatch(r"[\s0-9a-fA-FxX()+\-*/%<>=!andortTRUEFALS]*", expr):
        return 0
    try:
        return int(eval(expr.replace("/", "//"), {"__builtins__": {}}))  # digits, operators and/or/not only
    except Exception:  # noqa: BLE001 - anything we can't evaluate counts as false
        return 0


def _label_wild_variants(entries: list[dict]) -> None:
    """Name the tables of a map that has several (sRoute1_FireRed, sRoute1_LeafGreen, sRoute1_FireRed_Night…):
    what differs between their labels, e.g. "FireRed", "LeafGreen", "FireRed Night"."""
    labels = [e.get("base_label") or "" for e in entries]
    if len(labels) < 2:
        for e in entries:
            e["variant"] = ""
        return
    parts = [lb.split("_") for lb in labels]
    common = 0
    while all(len(x) > common for x in parts) and len({x[common] for x in parts}) == 1:
        common += 1
    for e, x in zip(entries, parts):
        e["variant"] = " ".join(x[common:]) or "default"


def _balanced(expr: str) -> bool:
    depth = 0
    for c in expr:
        depth += (c == "(") - (c == ")")
        if depth < 0:
            return False
    return depth == 0


def active_lines(text: str, defines: dict[str, str]) -> list[bool]:
    """For each line: is it compiled, given the #if/#elif/#else/#endif around it?"""
    out, stack = [], []  # stack of (parent_active, this_branch_active, any_branch_taken)
    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r"#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)", s)
        active = all(f[1] for f in stack)
        if m:
            kind, rest = m.group(1), m.group(2)
            if kind in ("if", "ifdef", "ifndef"):
                if kind == "if":
                    v = bool(_cpp_value(rest, defines))
                elif kind == "ifdef":
                    v = rest.strip() in defines
                else:
                    v = rest.strip() not in defines
                stack.append([active, v, v])
            elif kind == "elif" and stack:
                f = stack[-1]
                v = not f[2] and bool(_cpp_value(rest, defines))
                f[1], f[2] = v, f[2] or v
            elif kind == "else" and stack:
                f = stack[-1]
                f[1], f[2] = not f[2], True
            elif kind == "endif" and stack:
                stack.pop()
            out.append(False)
            continue
        out.append(active)
    return out


class GameData:
    def __init__(self, dp):
        self.dp = dp
        self.root: Path = dp.root
        self._defines: dict[str, str] | None = None
        self._species: dict | None = None
        self._names: dict | None = None
        self._spelling: dict[str, str] = {}  # constant -> how the .party file writes it
        self._icons: dict[str, str] = {}  # SPECIES_X -> icon symbol
        self._tables: dict = {}
        self._files: dict = {}

    @property
    def defines(self) -> dict[str, str]:
        if self._defines is None:
            d = dict(self.dp.defines)
            for f in [self.root / "include" / "constants" / "global.h"]:
                if f.exists():
                    for m in re.finditer(r"^\s*#define\s+(\w+)[ \t]+([^\n/]*)", f.read_text(errors="replace"), re.M):
                        d.setdefault(m.group(1), m.group(2).strip())
            self._defines = d
        return self._defines

    # ------------------------------------------------------------------ display names
    def names(self) -> dict[str, dict[str, str]]:
        """{"SPECIES_": {const: "Bulbasaur"}, "MOVE_": {...}, "ITEM_": {...}} from the game's own data."""
        if self._names is not None:
            return self._names
        out: dict[str, dict[str, str]] = {"SPECIES_": {}, "MOVE_": {}, "ITEM_": {}, "ABILITY_": {}}
        sources = [("SPECIES_", list((self.root / SPECIES_DIR).glob("*.h")), r"\.speciesName\s*=\s*_\(\s*\"([^\"]*)\""),
                   ("MOVE_", [self.root / "src/data/moves_info.h"], r"\.name\s*=\s*(?:COMPOUND_STRING|_)\(\s*\"([^\"]*)\""),
                   ("ITEM_", [self.root / "src/data/items.h"], r"\.name\s*=\s*(?:ITEM_NAME|COMPOUND_STRING|_)\(\s*\"([^\"]*)\""),
                   ("ABILITY_", [self.root / "src/data/abilities.h"], r"\.name\s*=\s*_\(\s*\"([^\"]*)\"")]
        for prefix, files, rx in sources:
            for f in files:
                if not f.exists():
                    continue
                text = strip_comments(f.read_text(errors="replace"))
                starts = list(re.finditer(rf"\[({prefix}\w+)\]\s*=\s*\{{", text))
                for i, m in enumerate(starts):
                    end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
                    nm = re.search(rx, text[m.end():min(end, m.end() + 3000)])
                    if nm and m.group(1) not in out[prefix]:
                        out[prefix][m.group(1)] = nm.group(1)
                    if prefix == "SPECIES_" and m.group(1) not in self._icons:
                        ic = re.search(r"\.iconSprite\s*=\s*(\w+)", text[m.end():end])
                        if ic:
                            self._icons[m.group(1)] = ic.group(1)
        self._names = out
        return out

    def icon_file(self, species: str) -> str | None:
        self.names()
        return self.dp.symbol_file.get(self._icons.get(const_of("SPECIES", species), ""))

    def display(self, const: str) -> str:
        if not const:
            return ""
        prefix = const.split("_")[0] + "_"
        return self.names().get(prefix, {}).get(const) or pretty(const)

    def known(self, prefix: str) -> set[str]:
        return set(self.dp.maps.constants(prefix)) if self.dp.maps else set(self.names().get(prefix, {}))

    def _name_for_file(self, const: str) -> str:
        """How to write a constant in trainers.party: its in-game name when that maps back to it, else the constant."""
        if const in self._spelling:
            return self._spelling[const]
        prefix = const.split("_")[0]
        name = self.display(const)
        return name if name and const_of(prefix, name) == const else const

    # ------------------------------------------------------------------ trainers
    def _party_files(self) -> list[str]:
        return built_party_files(self.root)

    def _parse_party(self, rel: str) -> list[dict]:
        raw = (self.root / rel).read_text(errors="replace")
        heads = list(re.finditer(r"^=== *(\w+) *===[ \t]*$", raw, re.M))
        out = []
        for i, m in enumerate(heads):
            start, end = m.start(), heads[i + 1].start() if i + 1 < len(heads) else len(raw)
            body = raw[m.end():end]
            paras = [p for p in re.split(r"\n[ \t]*\n", body.strip("\n")) if p.strip()]
            header, mons = [], []
            if paras:
                for line in paras[0].splitlines():
                    k, _, v = line.partition(":")
                    header.append([k.strip(), v.strip()] if _ else [line, None])
                for p in paras[1:]:
                    mons.append(self._parse_mon(p))
            out.append({"id": m.group(1), "file": rel, "line": raw.count("\n", 0, start) + 1, "span": (start, end),
                        "header": header, "mons": mons,
                        "has_comments": "/*" in body})
        return out

    def _parse_mon(self, para: str) -> dict:
        lines = [l.rstrip() for l in para.splitlines() if l.strip()]
        first = lines[0].strip() if lines else ""
        mon = {"species": "", "nickname": "", "gender": "", "item": "", "level": None, "moves": [], "extra": [],
               "order": []}
        rest = first
        im = re.search(r"\s@\s*(.+)$", rest)
        if im:
            mon["item"] = self._spell("ITEM", im.group(1))
            rest = rest[:im.start()]
        gm = re.search(r"\s*\(([MF])\)\s*$", rest)
        if gm:
            mon["gender"] = gm.group(1)
            rest = rest[:gm.start()]
        nm = re.fullmatch(r"(.+?)\s*\(([^()]+)\)", rest.strip())
        if nm:
            mon["nickname"], species = nm.group(1).strip(), nm.group(2)
        else:
            species = rest
        mon["species"] = self._spell("SPECIES", species)
        for line in lines[1:]:
            s = line.strip()
            if s.startswith("- "):
                mon["moves"].append(self._spell("MOVE", s[2:]))
                continue
            k, sep, v = s.partition(":")
            if sep and k.strip() == "Level" and v.strip().isdigit():
                mon["level"] = int(v)
                mon["order"].append("level")
            elif sep and k.strip() == "Ability":
                mon["ability"] = self._spell("ABILITY", v)
                mon["order"].append("ability")
            else:
                mon["extra"].append(s)
                mon["order"].append("extra")
        return mon

    def _spell(self, prefix: str, text: str) -> str:
        const = const_of(prefix, text)
        self._spelling.setdefault(const, text.strip())
        return const

    def trainers(self, query: str = "") -> list[dict]:
        q = query.lower()
        out = []
        for rel in self._party_files():
            for t in self._parse_party(rel):
                h = dict((k, v) for k, v in t["header"] if v is not None)
                row = {"id": t["id"], "name": h.get("Name", ""), "class": h.get("Class", ""), "pic": h.get("Pic", ""),
                       "file": rel, "line": t["line"], "party": [self.display(m["species"]) for m in t["mons"]],
                       "levels": [m["level"] for m in t["mons"]]}
                if not q or q in (t["id"] + " " + row["name"] + " " + row["class"]).lower():
                    out.append(row)
        return out

    def _find_trainer(self, tid: str) -> dict:
        tid = tid.strip().upper()
        if not tid.startswith("TRAINER_"):
            tid = "TRAINER_" + tid
        for rel in self._party_files():
            for t in self._parse_party(rel):
                if t["id"] == tid:
                    return t
        raise DecompError(f"no trainer {tid} in {', '.join(self._party_files()) or 'src/data/*.party'}")

    def get_trainer(self, tid: str) -> dict:
        t = self._find_trainer(tid)
        h = [{"key": k, "value": v} for k, v in t["header"] if v is not None]
        mons = [dict(m, species_name=self.display(m["species"]), item_name=self.display(m["item"]),
                     move_names=[self.display(x) for x in m["moves"]]) for m in t["mons"]]
        return {"id": t["id"], "file": t["file"], "line": t["line"], "fields": h, "mons": mons,
                "used_on": self.trainer_maps(t["id"]), "pic_file": self.trainer_pic(
                    dict((k, v) for k, v in t["header"] if v is not None).get("Pic", ""))}

    def trainer_pic(self, pic: str) -> str | None:
        if not pic:
            return None
        sym = "gTrainerFrontPic_" + "".join(w[:1].upper() + w[1:] for w in re.split(r"[\s_]+", pic.strip()) if w)
        sf = self.dp.symbol_file
        if sym in sf:
            return sf[sym]
        low = sym.lower()
        return next((f for s, f in sf.items() if s.lower() == low), None)

    def trainer_maps(self, tid: str) -> list[dict]:
        out = []
        maps_dir = self.root / "data" / "maps"
        for f in sorted(maps_dir.glob("*/scripts.inc")):
            text = f.read_text(errors="replace")
            from .playtest import game_trainer
            ids = "|".join(re.escape(x) for x in {tid, game_trainer(self.root, tid)})
            for m in re.finditer(rf"\btrainerbattle\w*\s+(?:{ids})\b", text):
                label = None
                for lm in re.finditer(r"^(\w+)::?", text[:m.start()], re.M):
                    label = lm.group(1)
                out.append({"map": f.parent.name, "script": label})
        return out

    def validate_trainer(self, data: dict) -> list[str]:
        problems = []
        mons = data.get("mons", [])
        species, moves, items = self.known("SPECIES_"), self.known("MOVE_"), self.known("ITEM_")
        if not mons:
            problems.append("a trainer needs at least one Pokémon")
        if len(mons) > 6:
            problems.append(f"{len(mons)} Pokémon; a team has at most 6")
        for i, m in enumerate(mons, 1):
            sp = m.get("species", "")
            if species and sp not in species:
                problems.append(f"Pokémon {i}: unknown species {sp}")
            lv = m.get("level")
            if lv is not None and not 1 <= int(lv) <= 100:
                problems.append(f"Pokémon {i}: level must be 1-100")
            if len(m.get("moves", [])) > 4:
                problems.append(f"Pokémon {i}: at most 4 moves")
            for mv in m.get("moves", []):
                if moves and mv not in moves:
                    problems.append(f"Pokémon {i}: unknown move {mv}")
            if m.get("item") and items and m["item"] not in items:
                problems.append(f"Pokémon {i}: unknown item {m['item']}")
        return problems

    def set_trainer(self, tid: str, data: dict) -> dict:
        """Replace a trainer's fields (``fields``: [{key, value}]) and/or team (``mons``)."""
        t = self._find_trainer(tid)
        mons = data.get("mons")
        if mons is not None:
            mons = [self._clean_mon(m) for m in mons]
            problems = self.validate_trainer({"mons": mons})
            if problems:
                raise DecompError("; ".join(problems))
        header = t["header"]
        if data.get("fields") is not None:
            new = {f["key"]: f["value"] for f in data["fields"]}
            header = [[k, new.pop(k)] if v is not None and k in new else [k, v] for k, v in header
                      if v is None or k in {f["key"] for f in data["fields"]}]
            header += [[k, v] for k, v in new.items()]
        text = self._trainer_text(t["id"], header, mons if mons is not None else t["mons"])
        raw = (self.root / t["file"]).read_text(errors="replace")
        start, end = t["span"]
        tail = raw[start:end]
        trailing = tail[len(tail.rstrip("\n")) + 1:]  # the blank line(s) before the next trainer
        self.dp._backup(t["file"])
        (self.root / t["file"]).write_text(raw[:start] + text + trailing + raw[end:])
        self.dp._changed = None
        return self.get_trainer(t["id"])

    def new_trainer(self, tid: str, like: str, data: dict | None = None) -> dict:
        """Add trainer ``tid`` as a copy of ``like`` (same file, class, picture, music, team), then apply ``data``
        like :meth:`set_trainer`. It gets the next number after the highest one of that file's trainers: its
        #define goes after that one's, a count define equal to the old count goes up by one, and an alias header that
        numbers the last trainer from a base (``#define TRAINER_JOHTO_X (JOHTO_TRAINER_ID_BASE + 652)``) gets
        the same line for the new one."""
        tid = tid.strip().upper()
        if not tid.startswith("TRAINER_"):
            tid = "TRAINER_" + tid
        if not re.fullmatch(r"TRAINER_[A-Z0-9_]+", tid):
            raise DecompError(f"{tid}: a trainer id is TRAINER_ followed by capital letters, digits and _")
        src = self._find_trainer(like)
        if any(t["id"] == tid for rel in self._party_files() for t in self._parse_party(rel)):
            raise DecompError(f"there is already a trainer {tid}")
        ids = {t["id"] for t in self._parse_party(src["file"])}
        consts = self.root / "include" / "constants"
        headers = sorted(consts.glob("*.h"))
        define = None  # the header numbering this file's trainers, and its highest-numbered trainer's line
        for h in headers:
            text = h.read_text(errors="replace")
            if not re.search(rf"^[ \t]*#[ \t]*define[ \t]+{src['id']}[ \t]+\d+", text, re.M):
                continue
            rows = [m for m in re.finditer(r"^([ \t]*#[ \t]*define[ \t]+)(TRAINER_\w+)([ \t]+)(\d+)[ \t]*$", text, re.M)
                    if m.group(2) in ids]
            if rows:
                define = (h, max(rows, key=lambda m: int(m.group(4))))
                break
        if define is None:
            raise DecompError(f"can't find '#define {src['id']} <number>' in include/constants")
        h, m = define
        last, n = m.group(2), int(m.group(4))
        edits: dict[Path, str] = {}

        def bump_counts(text: str, old: int) -> str:
            def up(cm):  # a count equal to the old count goes up; a maximum never does
                if cm.group(2).startswith("MAX_") or int(cm.group(3)) != old:
                    return cm.group(0)
                return f"{cm.group(1)}{old + 1}"
            out = re.sub(r"^([ \t]*#[ \t]*define[ \t]+(\w*COUNT\w*)[ \t]+)(\d+)\b", up, text, flags=re.M)
            for mm in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(MAX_\w*)[ \t]+(\d+)\b", out, re.M):
                if int(mm.group(2)) < old + 1 and re.search(r"TRAINER", mm.group(1)):
                    raise DecompError(f"{h.name}: {mm.group(1)} is {mm.group(2)}; there is no room for another "
                                      "trainer")
            return out

        text = h.read_text(errors="replace")
        line_end = text.index("\n", m.end()) + 1 if "\n" in text[m.end():] else len(text)
        pad = " " * max(1, len(last) + len(m.group(3)) - len(tid))  # keep the numbers in one column
        text = text[:line_end] + f"{m.group(1)}{tid}{pad}{n + 1}\n" + text[line_end:]
        edits[h] = bump_counts(text, n + 1)  # trainers 0..n: the count was n + 1
        for cm in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+(\w*TRAINERS?_COUNT\w*)[ \t]+(\d+)\b", edits[h], re.M):
            if not cm.group(1).startswith("MAX_") and int(cm.group(2)) < n + 2:
                raise DecompError(f"{h.name}: {cm.group(1)} is {cm.group(2)}, but the highest trainer is {last} = "
                                  f"{n}; fix the count first")
        short_last, short_new = last[len("TRAINER_"):], tid[len("TRAINER_"):]
        for a in headers:
            if a == h:
                continue
            at = a.read_text(errors="replace")
            am = re.search(rf"^([ \t]*#[ \t]*define[ \t]+)(\w*?){short_last}([ \t]+\([ \t]*\w+[ \t]*\+[ \t]*){n}"
                           rf"([ \t]*\))[ \t]*$", at, re.M)
            if not am:
                continue
            end = at.index("\n", am.end()) + 1 if "\n" in at[am.end():] else len(at)
            at = at[:end] + f"{am.group(1)}{am.group(2)}{short_new}{am.group(3)}{n + 1}{am.group(4)}\n" + at[end:]
            edits[a] = bump_counts(at, n + 1)
        # the new trainer goes at the end of the party file
        raw = (self.root / src["file"]).read_text(errors="replace")
        body = self._trainer_text(tid, src["header"], src["mons"])
        self.dp._backup(src["file"])
        for f in edits:
            self.dp._backup(str(f.relative_to(self.root)))
        (self.root / src["file"]).write_text(raw.rstrip("\n") + "\n\n" + body + "\n")
        for f, t in edits.items():
            f.write_text(t)
        self.dp._changed = None
        if data:
            self.set_trainer(tid, data)
        return self.get_trainer(tid)

    def _clean_mon(self, m: dict) -> dict:
        def c(prefix, v):
            return const_of(prefix, v) if v else ""
        lv = m.get("level")
        return {"species": c("SPECIES", m.get("species", "")), "nickname": (m.get("nickname") or "").strip(),
                "gender": m.get("gender") or "", "item": c("ITEM", m.get("item", "")),
                "level": int(lv) if lv not in (None, "") else None,
                "ability": c("ABILITY", m.get("ability", "")) if m.get("ability") else "",
                "moves": [c("MOVE", x) for x in m.get("moves", []) if x and str(x).strip()],
                "extra": [str(x).strip() for x in m.get("extra", []) if str(x).strip()],
                "order": list(m.get("order") or [])}

    def _trainer_text(self, tid: str, header: list, mons: list[dict]) -> str:
        lines = [f"=== {tid} ==="]
        for k, v in header:
            lines.append(k if v is None else f"{k}: {v}".rstrip() if v else f"{k}:")
        for m in mons:
            lines.append("")
            first = self._name_for_file(m["species"])
            if m.get("nickname"):
                first = f"{m['nickname']} ({first})"
            if m.get("gender"):
                first += f" ({m['gender']})"
            if m.get("item"):
                first += f" @ {self._name_for_file(m['item'])}"
            lines.append(first)
            parts = {"level": [f"Level: {m['level']}"] if m.get("level") is not None else [],
                     "ability": [f"Ability: {self._name_for_file(m['ability'])}"] if m.get("ability") else []}
            extra = list(m.get("extra", []))
            order = list(m.get("order") or [])
            if order.count("extra") != len(extra) or any(k not in order for k in ("level", "ability") if parts[k]):
                order = ["level", "ability"] + ["extra"] * len(extra)  # the file's order is unknown: use the usual one
            for k in order:
                if k == "extra":
                    lines.append(extra.pop(0))
                else:
                    lines += parts.pop(k, [])
            lines += [f"- {self._name_for_file(mv)}" for mv in m.get("moves", [])]
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ wild Pokémon
    def _wild_json(self) -> dict:
        p = self.root / WILD_FILE
        if not p.exists():
            raise DecompError(f"{WILD_FILE} not found")
        return json.loads(p.read_text())

    def _map_id(self, map_name: str) -> str:
        m = self.dp.maps
        if map_name.startswith("MAP_"):
            return map_name
        return m.get_map(m.resolve(map_name)).get("id", "")

    def wild(self, map_name: str) -> dict:
        data = self._wild_json()
        mid = self._map_id(map_name)
        for g in data["wild_encounter_groups"]:
            if not g.get("for_maps"):
                continue
            rates = {f["type"]: f for f in g.get("fields", [])}
            entries = [e for e in g.get("encounters", []) if e.get("map") == mid]
            out = {"map": mid, "group": g.get("label"), "entries": []}
            for e in entries:
                kinds = {}
                for k, label in WILD_KINDS.items():
                    f = rates.get(k, {})
                    slots = f.get("encounter_rates", [])
                    cur = e.get(k)
                    kinds[k] = {"label": label, "slot_rates": slots, "groups": f.get("groups"),
                                "encounter_rate": cur.get("encounter_rate") if cur else 0,
                                "mons": [dict(x, name=self.display(x["species"])) for x in (cur or {}).get("mons", [])]}
                out["entries"].append({"base_label": e.get("base_label"), "kinds": kinds})
            out["slot_counts"] = {k: len(rates.get(k, {}).get("encounter_rates", [])) for k in WILD_KINDS}
            _label_wild_variants(out["entries"])
            return out
        raise DecompError("wild_encounters.json has no group for maps")

    def set_wild(self, map_name: str, kind: str, encounter_rate: int, mons: list[dict], entry: int = 0) -> dict:
        """Set one kind of encounter (land_mons, water_mons, rock_smash_mons, fishing_mons) for a map.
        ``mons`` must have one entry per slot; an encounter_rate of 0 with no mons removes it."""
        if kind not in WILD_KINDS:
            raise DecompError(f"kind must be one of {', '.join(WILD_KINDS)}")
        data = self._wild_json()
        mid = self._map_id(map_name)
        group = next((g for g in data["wild_encounter_groups"] if g.get("for_maps")), None)
        if group is None:
            raise DecompError("wild_encounters.json has no group for maps")
        slots = len(next((f for f in group.get("fields", []) if f["type"] == kind), {}).get("encounter_rates", []))
        species = self.known("SPECIES_")
        clean = []
        for i, m in enumerate(mons, 1):
            sp = const_of("SPECIES", m.get("species", ""))
            lo, hi = int(m.get("min_level", 1)), int(m.get("max_level", m.get("min_level", 1)))
            if species and sp not in species:
                raise DecompError(f"slot {i}: unknown species {sp}")
            if not (1 <= lo <= hi <= 100):
                raise DecompError(f"slot {i}: levels must be 1-100, min ≤ max")
            clean.append({"min_level": lo, "max_level": hi, "species": sp})
        if clean and len(clean) != slots:
            raise DecompError(f"{WILD_KINDS[kind]} needs exactly {slots} slots (got {len(clean)})")
        if not 0 <= int(encounter_rate) <= 255:
            raise DecompError("the encounter rate must be 0-255")
        entries = [e for e in group["encounters"] if e.get("map") == mid]
        if not entries:
            if not clean:
                return self.wild(map_name)
            name = self.dp.maps.resolve(map_name) if not map_name.startswith("MAP_") else map_name
            e = {"map": mid, "base_label": "g" + re.sub(r"[^A-Za-z0-9]", "", name)}
            group["encounters"].append(e)
            entries = [e]
        e = entries[min(entry, len(entries) - 1)]
        if clean:
            e[kind] = {"encounter_rate": int(encounter_rate), "mons": clean}
        else:
            e.pop(kind, None)
        self._write_json(WILD_FILE, data)
        self.dp._changed = None
        return self.wild(map_name)

    # ------------------------------------------------------------------ Pokémon (species)
    def _table_index(self, rels: list[str], prefix: str = "SPECIES_") -> dict:
        """{CONST: {file, start, end, line}} for the compiled ``[CONST] = { … }`` entries of C table files."""
        key = (tuple(rels), prefix)
        if key in self._tables:
            return self._tables[key]
        out = {}
        for rel in rels:
            f = self.root / rel
            if not f.exists():
                continue
            text, act = self._parsed(rel)
            for m in re.finditer(rf"^\s*\[({prefix}\w+)\]\s*=\s*\{{", text, re.M):
                lineno = text.count("\n", 0, m.start())
                if not act[lineno]:
                    continue
                depth, i = 1, m.end()
                while depth and i < len(text):
                    depth += {"{": 1, "}": -1}.get(text[i], 0)
                    i += 1
                out.setdefault(m.group(1), {"file": rel, "start": m.end(), "end": i - 1, "line": lineno + 1})
        self._tables[key] = out
        return out

    def _parsed(self, rel: str) -> tuple[str, list[bool]]:
        """A C file without comments, and which of its lines are compiled (cached until the file changes)."""
        p = self.root / rel
        st = p.stat().st_mtime_ns
        hit = self._files.get(rel)
        if hit and hit[0] == st:
            return hit[1], hit[2]
        text = strip_comments(p.read_text(errors="replace"))
        act = active_lines(text, self.defines)
        self._files[rel] = (st, text, act)
        return text, act

    def _species_index(self) -> dict:
        if self._species is None:
            files = sorted((self.root / SPECIES_DIR).glob("*.h"), key=lambda f: (f.name.startswith("custom"), f.name))
            rels = [str(f.relative_to(self.root)) for f in files]  # your own Pokémon (custom_families.h) last
            self._species = self._table_index(rels, "SPECIES_")
        return self._species

    def _entry_fields(self, info: dict) -> tuple[dict, dict]:
        """({field: value text}, {field: (start, end, suffix)}) for the compiled lines of one table entry.
        A value with #if lines inside it spans the whole #if … #endif block (so replacing it keeps the file valid);
        ``suffix`` is what the replacement must end with then."""
        text, act = self._parsed(info["file"])
        body_start, body_end = info["start"], info["end"]
        values, spans = {}, {}
        for m in re.finditer(r"\.(\w+)\s*=\s*", text[body_start:body_end]):
            a = body_start + m.end()
            if not act[text.count("\n", 0, a)]:
                continue
            depth, j = 0, a
            while j < body_end:
                ch = text[j]
                if ch == '"':
                    j = _string_end(text, j)
                    continue
                if ch in "({":
                    depth += 1
                elif ch in ")}":
                    if depth == 0:
                        break
                    depth -= 1
                elif ch == "," and depth == 0:
                    break
                elif ch == "\n" and depth == 0 and text[a:j].strip() and not text[a:j].rstrip().endswith(("?", ":")):
                    nxt = text[j + 1:j + 200].lstrip()
                    if nxt.startswith((".", "#", "}")) or re.match(r"[A-Z_]+\(", nxt):
                        break
                j += 1
            raw = text[a:j]
            if re.search(r"^\s*#", raw, re.M):
                lines = raw.split("\n")
                first = text.count("\n", 0, a)
                values[m.group(1)] = "\n".join(l for k, l in enumerate(lines) if k == 0 or act[first + k]).strip()
                # extend to the end of the #if block the value is in, so no half block is left behind
                depth_if = sum(1 if d.group(1) != "endif" else -1
                               for d in re.finditer(r"^\s*#\s*(if|ifdef|ifndef|endif)\b", raw, re.M))
                end = j
                while depth_if > 0:
                    nl = text.find("\n", end)
                    if nl < 0:
                        break
                    stop = text.find("\n", nl + 1)
                    end = stop if stop >= 0 else len(text)
                    d = re.match(r"\s*#\s*(if|ifdef|ifndef|endif)\b", text[nl + 1:end])
                    if d:
                        depth_if += 1 if d.group(1) != "endif" else -1
                after = text[end:end + 1]
                spans[m.group(1)] = (a, end, "" if after == "," or j == end else ",")
            else:
                values[m.group(1)] = raw.strip()
                spans[m.group(1)] = (a, j, "")
        self._local_defines = {m.group(1): m.group(2).strip()
                               for ln, on in zip(text.split("\n"), act) if on
                               for m in [re.match(r"\s*#\s*define\s+(\w+)\s+(.+)", ln)] if m}
        return values, spans

    def _write_entry(self, rel: str, spans: dict, repl: dict[str, str]) -> None:
        if not repl:
            return
        for k in repl:
            if k not in spans:
                raise DecompError(f"no .{k} field to change in {rel}")
        path = self.root / rel
        raw = path.read_text(errors="replace")
        for k in sorted(repl, key=lambda k: spans[k][0], reverse=True):
            a, b, suffix = spans[k]
            new = repl[k] + suffix  # suffix: the value was an #if block, so its comma goes too
            raw = raw[:a] + new + raw[b:]
        self.dp._backup(rel)
        path.write_text(raw)
        self._tables = {}
        self._species = None
        self._names = None
        self.dp._changed = None

    def _species_fields(self, sid: str) -> tuple[dict, dict]:
        idx = self._species_index()
        if sid not in idx:
            raise DecompError(f"unknown species {sid}")
        return self._entry_fields(idx[sid])

    def _value(self, expr: str, depth: int = 0) -> int | None:
        expr = expr.strip()
        while expr.startswith("(") and expr.endswith(")") and _balanced(expr[1:-1]):
            expr = expr[1:-1].strip()
        m = re.fullmatch(r"(.+?)\?(.+?):(.+)", expr, re.S)
        if m:
            return self._value(m.group(2) if _cpp_value(m.group(1), self.defines) else m.group(3), depth)
        if re.fullmatch(r"\d+", expr):
            return int(expr)
        # a macro, e.g. `.baseDefense = PIKACHU_DEFENSE` with the #define in the same file
        v = getattr(self, "_local_defines", {}).get(expr) or self.defines.get(expr)
        if v is None or depth > 8:
            return None
        return self._value(v, depth + 1)

    def species_list(self, query: str = "") -> list[dict]:
        q = query.lower()
        names = self.names()["SPECIES_"]
        out = []
        for sid, info in self._species_index().items():
            name = names.get(sid) or pretty(sid)
            if q and q not in sid.lower() and q not in name.lower():
                continue
            out.append({"id": sid, "name": name, "file": info["file"], "line": info["line"]})
        return out

    def get_species(self, sid: str) -> dict:
        sid = const_of("SPECIES", sid)
        values, _ = self._species_fields(sid)
        stats = {k: self._value(values.get(k, "")) for k in STATS}
        types_raw = values.get("types", "")
        types = re.findall(r"TYPE_\w+", self._local_defines.get(types_raw.strip(), types_raw))
        abilities = re.findall(r"ABILITY_\w+", values.get("abilities", ""))
        evos = []
        ev = values.get("evolutions", "")
        inner = re.match(r"EVOLUTION\((.*)\)\s*$", ev, re.S)
        if inner:
            for g in _top_level_groups(inner.group(1)):
                parts = [p.strip() for p in _split_top(g)]
                if len(parts) >= 3:
                    evos.append({"method": parts[0], "param": parts[1], "target": parts[2],
                                 "target_name": self.display(parts[2]), "extra": parts[3:]})
        learn_sym = values.get("levelUpLearnset", "").strip()
        info = self._species_index()[sid]
        return {"id": sid, "name": self.display(sid), "file": info["file"], "line": info["line"],
                "stats": stats, "stat_exprs": {k: values.get(k, "") for k in STATS},
                "total": sum(v for v in stats.values() if v), "types": types,
                "types_raw": values.get("types", ""), "abilities": abilities,
                "ability_names": [self.display(a) for a in abilities],
                "catchRate": self._value(values.get("catchRate", "")), "expYield": self._value(values.get("expYield", "")),
                "evolutions": evos, "evolutions_raw": ev, "learnset_symbol": learn_sym,
                "learnset": self.learnset(learn_sym) if learn_sym else [],
                "species_name": _game_text(values.get("speciesName", "")),
                "dex": {"category": _game_text(values.get("categoryName", "")),
                        "height": self._value(values.get("height", "")), "weight": self._value(values.get("weight", "")),
                        "description": _game_text(values.get("description", "")),
                        "fits": self._description_limits(info["file"], "SPECIES_")},
                "teaching_type": values.get("teachingType", "").strip(),
                "front_pic": self.dp.symbol_file.get(values.get("frontPic", "").strip()),
                "icon": self.dp.symbol_file.get(values.get("iconSprite", "").strip())}

    def set_species(self, sid: str, changes: dict) -> dict:
        """Change stats ({baseHP: 50, …}), catchRate, expYield, types ([TYPE_X, TYPE_Y]), abilities ([A, B, C]),
        evolutions ([{method, param, target, extra}]), learnset ([{level, move}]), name, and the Pokédex entry
        (category, height in 0.1 m, weight in 0.1 kg, description); also teachable ({MOVE_X: bool}) and egg_moves."""
        sid = const_of("SPECIES", sid)
        values, spans = self._species_fields(sid)
        info = self._species_index()[sid]
        repl: dict[str, str] = {}
        add_lines: list[str] = []  # fields the entry doesn't have yet
        for k in NUMBER_FIELDS:
            if k in changes and changes[k] not in (None, ""):
                v = int(changes[k])
                if not 0 <= v <= 255 and k != "expYield":
                    raise DecompError(f"{STAT_NAMES.get(k, k)} must be 0-255")
                repl[k] = str(v)
        types_known = self.known("TYPE_")
        if changes.get("types"):
            t = [x for x in changes["types"] if x]
            if not 1 <= len(t) <= 2 or (types_known and any(x not in types_known for x in t)):
                raise DecompError("give 1 or 2 types, like TYPE_FIRE")
            repl["types"] = f"MON_TYPES({', '.join(t)})"
        if changes.get("abilities") is not None:
            a = [x or "ABILITY_NONE" for x in changes["abilities"]][:3]
            a += ["ABILITY_NONE"] * (3 - len(a))
            known = self.known("ABILITY_")
            if known and any(x not in known for x in a):
                raise DecompError(f"unknown ability in {', '.join(a)}")
            repl["abilities"] = "{ " + ", ".join(a) + " }"
        if changes.get("evolutions") is not None:
            evs = [e for e in changes["evolutions"] if e.get("target")]
            species = self.known("SPECIES_")
            for e in evs:
                if species and e["target"] not in species:
                    raise DecompError(f"unknown species {e['target']}")
            groups = ", ".join("{" + ", ".join([e["method"], str(e["param"]), e["target"], *e.get("extra", [])]) + "}"
                               for e in evs)
            if "evolutions" in spans:
                repl["evolutions"] = f"EVOLUTION({groups})" if evs else "NULL"
            elif evs:
                add_lines.append(f".evolutions = EVOLUTION({groups}),")
        if changes.get("name") is not None:
            self._check_name("SPECIES_", changes["name"])
            repl["speciesName"] = c_string_value(values.get("speciesName", ""), changes["name"].strip())
        if changes.get("category") is not None:
            if len(changes["category"]) > 12 or self._text_problems(changes["category"]):
                raise DecompError("the category can have at most 12 letters the game can show")
            repl["categoryName"] = c_string_value(values.get("categoryName", ""), changes["category"].strip())
        for k in ("height", "weight"):
            if changes.get(k) not in (None, ""):
                v = int(changes[k])
                if not 0 < v <= 65535:
                    raise DecompError(f"{k} must be 1-65535 (tenths of a {'metre' if k == 'height' else 'kilogram'})")
                repl[k] = str(v)
        if changes.get("description") is not None:
            problems = self._check_description(info["file"], "SPECIES_", changes["description"])
            if any("can't be shown" in p for p in problems):
                raise DecompError("; ".join(problems))
            repl["description"] = c_string_value(values.get("description", "COMPOUND_STRING()"), changes["description"], default="COMPOUND_STRING")
        for k in repl:
            if k not in spans:
                raise DecompError(f"{sid} has no .{k} field to change")
        self._write_entry(info["file"], spans, repl)
        if add_lines:
            info = self._species_index()[sid]
            path = self.root / info["file"]
            raw = path.read_text(errors="replace")
            close = info["end"]
            line_start = raw.rfind("\n", 0, close) + 1
            prev = raw[raw.rfind("\n", 0, line_start - 1) + 1:line_start]
            indent = re.match(r"[ \t]*", prev).group(0) or "        "
            if raw[line_start:close].strip():  # "}" on the same line as the last field
                raw = raw[:close] + "\n" + "".join(f"{indent}{x}\n" for x in add_lines) + raw[close:]
            else:
                raw = raw[:line_start] + "".join(f"{indent}{x}\n" for x in add_lines) + raw[line_start:]
            self.dp._backup(info["file"])
            path.write_text(raw)
            self._tables, self._species, self._files = {}, None, {}
            self.dp._changed = None
        if changes.get("teachable"):
            self.set_teachable(sid, changes["teachable"])
        if changes.get("egg_moves") is not None:
            self.set_egg_moves(sid, changes["egg_moves"])
        if changes.get("learnset") is not None:
            self.set_learnset(values.get("levelUpLearnset", "").strip(), changes["learnset"])
        return self.get_species(sid)

    # ------------------------------------------------------------------ level-up learnsets
    def learnset_file(self) -> str | None:
        gen = self.defines.get("P_LVL_UP_LEARNSETS", "GEN_LATEST")
        for _ in range(4):
            gen = self.defines.get(gen, gen)
        m = re.fullmatch(r"\d+", str(gen).strip())
        n = int(m.group(0)) + 1 if m else 9
        p = self.root / LEARNSET_DIR / f"gen_{n}.h"
        if not p.exists():
            files = sorted((self.root / LEARNSET_DIR).glob("gen_*.h"))
            p = files[-1] if files else p
        return str(p.relative_to(self.root)) if p.exists() else None

    def _learnset_span(self, sym: str):
        rel = self.learnset_file()
        if not rel or not sym:
            return None
        raw = (self.root / rel).read_text(errors="replace")
        m = re.search(rf"\b{re.escape(sym)}\s*\[\s*\]\s*=\s*\{{", raw)
        if not m:
            return None
        close = raw.index("};", m.end())
        return rel, raw, m.end(), close

    def learnset(self, sym: str) -> list[dict]:
        hit = self._learnset_span(sym)
        if not hit:
            return []
        _, raw, a, b = hit
        return [{"level": int(m.group(1)), "move": m.group(2), "name": self.display(m.group(2))}
                for m in re.finditer(r"LEVEL_UP_MOVE\(\s*(\d+)\s*,\s*(MOVE_\w+)\s*\)", raw[a:b])]

    def set_learnset(self, sym: str, moves: list[dict]) -> None:
        hit = self._learnset_span(sym)
        if not hit:
            raise DecompError(f"learnset {sym} not found in {self.learnset_file()}")
        rel, raw, a, b = hit
        known = self.known("MOVE_")
        rows = []
        for m in sorted(moves, key=lambda m: int(m["level"])):
            mv = const_of("MOVE", m["move"])
            if known and mv not in known:
                raise DecompError(f"unknown move {mv}")
            if not 0 <= int(m["level"]) <= 100:
                raise DecompError("learnset levels must be 0-100")
            rows.append(f"    LEVEL_UP_MOVE({int(m['level']):>2}, {mv}),")
        body = "\n" + "\n".join(rows + ["    LEVEL_UP_END"]) + "\n"
        self.dp._backup(rel)
        (self.root / rel).write_text(raw[:a] + body + raw[b:])
        self.dp._changed = None

    def _write_json(self, rel: str, data) -> None:
        """Write JSON the way the file already was (escaped or plain non-ASCII, final newline or not)."""
        p = self.root / rel
        old = p.read_text() if p.exists() else ""
        out = json.dumps(data, indent=2, ensure_ascii="\\u00" in old)
        if not old or old.endswith("\n"):
            out += "\n"
        self.dp._backup(rel)
        p.write_text(out)

    # ------------------------------------------------------------------ text fields (names, descriptions)
    def _text_problems(self, text: str) -> list[str]:
        tb = getattr(self.dp, "textbox", None)
        if tb is None or not (self.root / "charmap.txt").exists():  # without it the letters the font has are a guess
            return []
        return [p for p in tb._tokens(text.replace("\n", "\\n"))[1] if "not a known code" not in p]

    def _check_name(self, prefix: str, name: str) -> None:
        define, default = NAME_LENGTHS[prefix]
        limit = self._value(define) or default
        if not name.strip():
            raise DecompError("the name can't be empty")
        if len(name) > limit:
            raise DecompError(f"'{name}' is {len(name)} letters; names can have at most {limit} ({define})")
        problems = self._text_problems(name)
        if problems:
            raise DecompError("; ".join(problems))

    def _description_limits(self, rel: str, prefix: str) -> tuple[int, int]:
        """(lines, letters per line) of the longest descriptions already in a table: what its window fits."""
        key = ("desc", rel)
        if key not in self._tables:
            text, _ = self._parsed(rel)
            lines = chars = 0
            for m in re.finditer(r"\.description\s*=\s*(?:COMPOUND_STRING|_)\(((?:\s*\"(?:[^\"\\]|\\.)*\")+)\s*\)", text):
                body = c_strings(m.group(1)).split("\\n")
                lines, chars = max(lines, len(body)), max(chars, max(len(x) for x in body))
            self._tables[key] = (lines or 3, chars or 20)
        return self._tables[key]

    def _check_description(self, rel: str, prefix: str, text: str) -> list[str]:
        max_lines, max_chars = self._description_limits(rel, prefix)
        out = self._text_problems(text)
        lines = text.split("\n")
        if len(lines) > max_lines:
            out.append(f"{len(lines)} lines; descriptions here fit {max_lines}")
        for ln in lines:
            if len(ln) > max_chars + 2:
                out.append(f"\"{ln}\" is {len(ln)} letters; the longest line in other descriptions is {max_chars}")
        return out

    # ------------------------------------------------------------------ items
    def _item_index(self) -> dict:
        return self._table_index([ITEMS_FILE], "ITEM_") if (self.root / ITEMS_FILE).exists() else {}

    def items(self, query: str = "") -> list[dict]:
        q = query.lower()
        names = self.names()["ITEM_"]
        out = []
        for iid, info in self._item_index().items():
            name = names.get(iid) or pretty(iid)
            if iid == "ITEM_NONE" or (q and q not in iid.lower() and q not in name.lower()):
                continue
            out.append({"id": iid, "name": name, "line": info["line"]})
        return out

    def get_item(self, iid: str) -> dict:
        iid = const_of("ITEM", iid)
        idx = self._item_index()
        if iid not in idx:
            raise DecompError(f"unknown item {iid}")
        values, _ = self._entry_fields(idx[iid])
        icon = values.get("iconPic", "").strip()
        marts = [m for m in self.marts() if iid in m["items"]]
        return {"id": iid, "file": ITEMS_FILE, "line": idx[iid]["line"],
                "name": _game_text(values.get("name", "")), "price": self._value(values.get("price", "")),
                "price_expr": values.get("price", ""), "description": _game_text(values.get("description", "")),
                "pocket": values.get("pocket", ""), "holdEffect": values.get("holdEffect", ""),
                "holdEffectParam": self._value(values.get("holdEffectParam", "")),
                "fields": {k: v for k, v in values.items() if k not in ("name", "description")},
                "icon_file": self.dp.symbol_file.get(icon), "sold_in": [{"map": m["map"], "label": m["label"]} for m in marts],
                "description_fits": self._description_limits(ITEMS_FILE, "ITEM_")}

    def set_item(self, iid: str, changes: dict) -> dict:
        """Change an item's name, price, description, pocket and/or holdEffectParam."""
        iid = const_of("ITEM", iid)
        info = self._item_index().get(iid)
        if not info:
            raise DecompError(f"unknown item {iid}")
        values, spans = self._entry_fields(info)
        repl = {}
        if changes.get("name") is not None:
            self._check_name("ITEM_", changes["name"])
            repl["name"] = c_string_value(values.get("name", ""), changes["name"].strip())
        if changes.get("price") not in (None, ""):
            v = int(changes["price"])
            if not 0 <= v <= 999999:
                raise DecompError("the price must be 0-999999")
            repl["price"] = str(v)
        if changes.get("holdEffectParam") not in (None, ""):
            v = int(changes["holdEffectParam"])
            if not 0 <= v <= 255:
                raise DecompError("the hold effect amount must be 0-255")
            repl["holdEffectParam"] = str(v)
        if changes.get("pocket"):
            pockets = self.known("POCKET_")
            if pockets and changes["pocket"] not in pockets:
                raise DecompError(f"unknown pocket {changes['pocket']}")
            repl["pocket"] = changes["pocket"]
        if changes.get("description") is not None:
            problems = self._check_description(ITEMS_FILE, "ITEM_", changes["description"])
            if any("can't be shown" in p for p in problems):
                raise DecompError("; ".join(problems))
            repl["description"] = c_string_value(values.get("description", "COMPOUND_STRING()"), changes["description"], default="COMPOUND_STRING")
        self._write_entry(info["file"], spans, repl)
        return self.get_item(iid)

    # ------------------------------------------------------------------ moves
    def _move_index(self) -> dict:
        return self._table_index([MOVES_FILE], "MOVE_") if (self.root / MOVES_FILE).exists() else {}

    def moves(self, query: str = "") -> list[dict]:
        q = query.lower()
        names = self.names()["MOVE_"]
        out = []
        for mid, info in self._move_index().items():
            name = names.get(mid) or pretty(mid)
            if mid == "MOVE_NONE" or (q and q not in mid.lower() and q not in name.lower()):
                continue
            out.append({"id": mid, "name": name, "line": info["line"]})
        return out

    def get_move(self, mid: str) -> dict:
        mid = const_of("MOVE", mid)
        idx = self._move_index()
        if mid not in idx:
            raise DecompError(f"unknown move {mid}")
        values, _ = self._entry_fields(idx[mid])
        tms = self.tms()
        tm = next((f"{k.upper()}{i + 1:02d}" for k in ("tm", "hm") for i, x in enumerate(tms[k + "s"])
                   if x["move"] == mid), None)
        return {"id": mid, "file": MOVES_FILE, "line": idx[mid]["line"],
                "name": _game_text(values.get("name", "")), "description": _game_text(values.get("description", "")),
                **{k: self._value(values.get(k, "")) for k in MOVE_NUMBERS},
                "exprs": {k: values.get(k, "") for k in MOVE_NUMBERS},
                "type": values.get("type", ""), "category": values.get("category", ""),
                "effect": values.get("effect", ""), "target": values.get("target", ""),
                "makesContact": values.get("makesContact", "FALSE").strip() == "TRUE",
                "fields": {k: v for k, v in values.items() if k not in ("name", "description")},
                "tm": tm, "learned_by": self.learned_by(mid),
                "description_fits": self._description_limits(MOVES_FILE, "MOVE_")}

    def set_move(self, mid: str, changes: dict) -> dict:
        """Change a move's name, description, power, accuracy, pp, priority, type, category and/or effect."""
        mid = const_of("MOVE", mid)
        info = self._move_index().get(mid)
        if not info:
            raise DecompError(f"unknown move {mid}")
        values, spans = self._entry_fields(info)
        repl = {}
        if changes.get("name") is not None:
            self._check_name("MOVE_", changes["name"])
            repl["name"] = c_string_value(values.get("name", ""), changes["name"].strip())
        for k, (lo, hi) in MOVE_NUMBERS.items():
            if changes.get(k) not in (None, ""):
                v = int(changes[k])
                if not lo <= v <= hi:
                    raise DecompError(f"{k} must be {lo} to {hi}")
                repl[k] = str(v)
        for k, prefix in (("type", "TYPE_"), ("category", "DAMAGE_CATEGORY_"), ("effect", "EFFECT_"),
                          ("target", "TARGET_")):
            if changes.get(k):
                known = self.known(prefix)
                if known and changes[k] not in known:
                    raise DecompError(f"unknown {k} {changes[k]}")
                repl[k] = changes[k]
        if changes.get("makesContact") is not None and "makesContact" in spans:
            repl["makesContact"] = "TRUE" if changes["makesContact"] else "FALSE"
        if changes.get("description") is not None:
            problems = self._check_description(MOVES_FILE, "MOVE_", changes["description"])
            if any("can't be shown" in p for p in problems):
                raise DecompError("; ".join(problems))
            repl["description"] = c_string_value(values.get("description", "COMPOUND_STRING()"), changes["description"], default="COMPOUND_STRING")
        self._write_entry(info["file"], spans, repl)
        return self.get_move(mid)

    def learned_by(self, mid: str) -> list[dict]:
        """Pokémon that learn a move by level up: [{species, name, level}]."""
        rel = self.learnset_file()
        if not rel:
            return []
        if ("learnsets", rel) not in self._tables:
            text, _ = self._parsed(rel)
            by_sym = {}
            for m in re.finditer(r"(\w+)\s*\[\s*\]\s*=\s*\{(.*?)\};", text, re.S):
                by_sym[m.group(1)] = re.findall(r"LEVEL_UP_MOVE\(\s*(\d+)\s*,\s*(MOVE_\w+)\s*\)", m.group(2))
            sym_of = {}
            for sid, info in self._species_index().items():
                body = self._parsed(info["file"])[0][info["start"]:info["end"]]
                lm = re.search(r"\.levelUpLearnset\s*=\s*(\w+)", body)
                if lm:
                    sym_of[sid] = lm.group(1)
            self._tables[("learnsets", rel)] = (by_sym, sym_of)
        by_sym, sym_of = self._tables[("learnsets", rel)]
        out = []
        for sid, sym in sym_of.items():
            for lv, mv in by_sym.get(sym, []):
                if mv == mid:
                    out.append({"species": sid, "name": self.display(sid), "level": int(lv)})
                    break
        return out

    # ------------------------------------------------------------------ TMs, HMs and tutors
    def tms(self) -> dict:
        """{"tms": [{move, name}], "hms": [...], "max_tms": N, "max_hms": N} from FOREACH_TM / FOREACH_HM."""
        p = self.root / TMS_FILE
        out = {"tms": [], "hms": [], "max_tms": 0, "max_hms": 0, "file": TMS_FILE}
        if not p.exists():
            return out
        text = strip_comments(p.read_text(errors="replace"))
        for kind in ("TM", "HM"):
            m = re.search(rf"#define\s+FOREACH_{kind}\(F\)((?:[^\n]*\\\n)*[^\n]*)", text)
            moves = re.findall(r"F\((\w+)\)", m.group(1)) if m else []
            out[kind.lower() + "s"] = [{"move": "MOVE_" + x, "name": self.display("MOVE_" + x)} for x in moves]
            out[f"max_{kind.lower()}s"] = len(self.known(f"ITEM_{kind}")) and len(
                [c for c in self.known(f"ITEM_{kind}") if re.fullmatch(rf"ITEM_{kind}\d+", c)])
        return out

    def set_tms(self, tms: list[str] | None = None, hms: list[str] | None = None) -> dict:
        p = self.root / TMS_FILE
        if not p.exists():
            raise DecompError(f"{TMS_FILE} not found")
        raw = p.read_text(errors="replace")
        known = self.known("MOVE_")
        cur = self.tms()
        final = {"TM": [const_of("MOVE", x) for x in tms if x and str(x).strip()] if tms is not None
                 else [x["move"] for x in cur["tms"]],
                 "HM": [const_of("MOVE", x) for x in hms if x and str(x).strip()] if hms is not None
                 else [x["move"] for x in cur["hms"]]}
        both = sorted(set(final["TM"]) & set(final["HM"]))
        if both:
            raise DecompError(f"{', '.join(both)} can't be a TM and an HM at the same time")
        for kind, moves in (("TM", tms), ("HM", hms)):
            if moves is None:
                continue
            moves = [const_of("MOVE", x) for x in moves if x and str(x).strip()]
            for mv in moves:
                if known and mv not in known:
                    raise DecompError(f"unknown move {mv}")
            dup = {m for m in moves if moves.count(m) > 1}
            if dup:
                raise DecompError(f"{', '.join(sorted(dup))} is in the {kind} list twice")
            limit = cur[f"max_{kind.lower()}s"]
            if limit and len(moves) > limit:
                raise DecompError(f"the game has {limit} {kind} items (ITEM_{kind}01…); add more items first")
            m = re.search(rf"#define\s+FOREACH_{kind}\(F\)((?:[^\n]*\\\n)*[^\n]*)", raw)
            if not m:
                raise DecompError(f"no FOREACH_{kind} in {TMS_FILE}")
            body = "".join(f" \\\n    F({mv[5:]})" for mv in moves)
            raw = raw[:m.start(1)] + body + raw[m.end(1):]
        self.dp._backup(TMS_FILE)
        p.write_text(raw)
        for kind in ("TM", "HM"):
            self._retarget_tm_items(kind, [x["move"] for x in cur[kind.lower() + "s"]], final[kind])
        self.dp._changed = None
        self._tables = {}
        return self.tms()

    def _retarget_tm_items(self, kind: str, old: list[str], new: list[str]) -> list[str]:
        """TM items are named after their move (ITEM_TM_OVERHEAT = ITEM_TM50): when TM50 teaches another move, rename
        the item everywhere (items.h, item balls, marts…) and give it the new move's description."""
        def item(moves, i):
            return f"ITEM_{kind}_{moves[i][5:]}" if i < len(moves) else f"ITEM_{kind}{i + 1:02d}"
        renames = {item(old, i): item(new, i) for i in range(max(len(old), len(new))) if item(old, i) != item(new, i)}
        if not renames:
            return []
        rx = re.compile(r"\b(" + "|".join(map(re.escape, renames)) + r")\b")
        files = [p for pat in ("data/**/*.inc", "data/**/*.pory", "src/**/*.c", "src/**/*.h", "src/data/*.party")
                 for p in self.root.glob(pat)]
        changed = []
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if f"ITEM_{kind}" not in text or not rx.search(text):
                continue
            rel = str(f.relative_to(self.root))
            self.dp._backup(rel)
            f.write_text(rx.sub(lambda m: renames[m.group(1)], text))
            changed.append(rel)
        # the item's description: the move's, wrapped to what item descriptions fit
        if (self.root / ITEMS_FILE).exists():
            self._tables = {}
            lines, width = self._description_limits(ITEMS_FILE, "ITEM_")
            for i, mv in enumerate(new):
                if i < len(old) and old[i] == mv:
                    continue
                info = self._item_index().get(item(new, i))
                minfo = self._move_index().get(mv)
                if not info or not minfo:
                    continue
                words = _game_text(self._entry_fields(minfo)[0].get("description", "")).split()
                rows = [""]
                for w in words:
                    if rows[-1] and len(rows[-1]) + 1 + len(w) > width:
                        rows.append(w)
                    else:
                        rows[-1] = (rows[-1] + " " + w).strip()
                values, spans = self._entry_fields(info)
                if "description" in spans and words:
                    self._write_entry(ITEMS_FILE, spans, {"description": c_string_value(
                        values["description"], "\n".join(rows[:lines]))})
        return changed

    def tutors(self) -> list[dict]:
        """Move tutors in scripts: [{map, label, move, name, file, line}], found the way
        tools/learnset_helpers/make_tutors.py finds them (``move_tutor MOVE_X``, or ``setvar VAR_0x8005, MOVE_X`` in
        a file that uses the ChooseMonForMoveTutor special), plus ``extraTutors`` in special_movesets.json."""
        out = []
        files = sorted((self.root / "data" / "scripts").glob("*.inc")) + sorted(
            (self.root / "data" / "maps").glob("*/scripts.inc"))
        for f in files:
            text = f.read_text(errors="replace")
            has_tutor = "special ChooseMonForMoveTutor" in text or "chooseboxmon SELECT_PC_MON_MOVE_TUTOR" in text
            rx = r"setvar VAR_0x8005, (MOVE_[A-Z_]*)|move_tutor (MOVE_[A-Z_]*)" if has_tutor else r"()move_tutor (MOVE_[A-Z_]*)"
            for m in re.finditer(rx, text):
                mv = m.group(1) or m.group(2)
                label = None
                for lm in re.finditer(r"^(\w+)::?", text[:m.start()], re.M):
                    label = lm.group(1)
                rel = str(f.relative_to(self.root))
                out.append({"map": f.parent.name if f.parent.parent.name == "maps" else rel, "label": label,
                            "move": mv, "name": self.display(mv), "file": rel,
                            "line": text.count("\n", 0, m.start()) + 1})
        p = self.root / SPECIAL_MOVESETS
        if p.exists():
            for mv in json.loads(p.read_text()).get("extraTutors", []):
                out.append({"map": None, "label": None, "move": mv, "name": self.display(mv), "file": SPECIAL_MOVESETS,
                            "line": None})
        return out

    def _teachable_key(self, sid: str) -> str | None:
        """The all_learnables.json key of a species: its teachable learnset's name (sBulbasaurTeachableLearnset ->
        BULBASAUR), the way make_teachables.py makes it."""
        values, _ = self._species_fields(sid)
        m = re.fullmatch(r"s(\w+)TeachableLearnset", values.get("teachableLearnset", "").strip())
        return re.sub(r"(?!^)([A-Z]+)", r"_\1", m.group(1)).upper() if m else None

    def _helper_on(self) -> bool:
        v = self.defines.get("P_LEARNSET_HELPER_TEACHABLE", "FALSE").strip()
        return v in ("TRUE", "1")

    def teachable(self, sid: str) -> dict:
        """Which TM/HM and tutor moves a Pokémon can learn, and how to change it."""
        sid = const_of("SPECIES", sid)
        values, _ = self._species_fields(sid)
        sym = values.get("teachableLearnset", "").strip()
        tms = self.tms()
        tm_moves = [x["move"] for x in tms["tms"] + tms["hms"]]
        tutor_moves = sorted({t["move"] for t in self.tutors()})
        helper = self._helper_on() and (self.root / LEARNABLES_FILE).exists()
        can = set()
        if helper:
            key = self._teachable_key(sid)
            data = json.loads((self.root / LEARNABLES_FILE).read_text())
            can = set(data.get(key, [])) if key else set()
            special = json.loads((self.root / SPECIAL_MOVESETS).read_text()) if (self.root / SPECIAL_MOVESETS).exists() else {}
            can |= set(special.get("universalMoves", []))
        elif (self.root / TEACHABLE_FILE).exists() and sym:
            m = re.search(rf"\b{re.escape(sym)}\s*\[\s*\]\s*=\s*\{{(.*?)\}};", (self.root / TEACHABLE_FILE).read_text(), re.S)
            can = set(re.findall(r"MOVE_\w+", m.group(1))) if m else set()
        rows = [{"move": mv, "name": self.display(mv), "how": f"{'TM' if i < len(tms['tms']) else 'HM'}"
                 f"{(i if i < len(tms['tms']) else i - len(tms['tms'])) + 1:02d}", "can": mv in can}
                for i, mv in enumerate(tm_moves)]
        rows += [{"move": mv, "name": self.display(mv), "how": "tutor", "can": mv in can}
                 for mv in tutor_moves if mv not in tm_moves]
        return {"species": sid, "moves": rows, "source": LEARNABLES_FILE if helper else TEACHABLE_FILE,
                "symbol": sym, "key": self._teachable_key(sid) if helper else None}

    def set_teachable(self, sid: str, moves: dict[str, bool]) -> dict:
        """Turn TM/HM/tutor moves on or off for a Pokémon: {MOVE_X: True, MOVE_Y: False}."""
        sid = const_of("SPECIES", sid)
        cur = self.teachable(sid)
        moves = {const_of("MOVE", k): bool(v) for k, v in moves.items()}
        if cur["source"] == LEARNABLES_FILE:
            key = cur["key"]
            if not key:
                raise DecompError(f"{sid} has no teachable learnset")
            p = self.root / LEARNABLES_FILE
            data = json.loads(p.read_text())
            have = list(data.get(key, []))
            for mv, on in moves.items():
                if on and mv not in have:
                    have.append(mv)
                elif not on and mv in have:
                    have.remove(mv)
            data[key] = sorted(have)
            self._write_json(LEARNABLES_FILE, data)
        else:
            p = self.root / TEACHABLE_FILE
            raw = p.read_text()
            m = re.search(rf"\b{re.escape(cur['symbol'])}\s*\[\s*\]\s*=\s*\{{(.*?)\}};", raw, re.S)
            if not m:
                raise DecompError(f"{cur['symbol']} not found in {TEACHABLE_FILE}")
            have = [x for x in re.findall(r"MOVE_\w+", m.group(1)) if x != "MOVE_UNAVAILABLE"]
            for mv, on in moves.items():
                if on and mv not in have:
                    have.append(mv)
                elif not on and mv in have:
                    have.remove(mv)
            body = "\n" + "".join(f"    {x},\n" for x in have + ["MOVE_UNAVAILABLE"])
            self.dp._backup(TEACHABLE_FILE)
            p.write_text(raw[:m.start(1)] + body + raw[m.end(1):])
        self.dp._changed = None
        return self.teachable(sid)

    def egg_moves(self, sid: str) -> dict:
        sid = const_of("SPECIES", sid)
        values, _ = self._species_fields(sid)
        sym = values.get("eggMoveLearnset", "").strip()
        p = self.root / EGG_FILE
        if not sym or not p.exists():
            return {"symbol": sym, "moves": []}
        m = re.search(rf"\b{re.escape(sym)}\s*\[\s*\]\s*=\s*\{{(.*?)\}};", p.read_text(), re.S)
        mv = [x for x in re.findall(r"MOVE_\w+", m.group(1)) if x != "MOVE_UNAVAILABLE"] if m else []
        return {"symbol": sym, "moves": [{"move": x, "name": self.display(x)} for x in mv], "found": bool(m)}

    def set_egg_moves(self, sid: str, moves: list[str]) -> dict:
        cur = self.egg_moves(sid)
        if not cur.get("found"):
            raise DecompError(f"{sid} has no egg move list to change")
        known = self.known("MOVE_")
        moves = [const_of("MOVE", x) for x in moves if x and str(x).strip()]
        for mv in moves:
            if known and mv not in known:
                raise DecompError(f"unknown move {mv}")
        p = self.root / EGG_FILE
        raw = p.read_text()
        m = re.search(rf"\b{re.escape(cur['symbol'])}\s*\[\s*\]\s*=\s*\{{(.*?)\}};", raw, re.S)
        body = "\n" + "".join(f"    {x},\n" for x in moves + ["MOVE_UNAVAILABLE"])
        self.dp._backup(EGG_FILE)
        p.write_text(raw[:m.start(1)] + body + raw[m.end(1):])
        self.dp._changed = None
        return self.egg_moves(sid)

    # ------------------------------------------------------------------ Poké Marts
    def marts(self) -> list[dict]:
        """Every ``pokemart LABEL`` list in map and common scripts: [{map, label, file, line, items, clerk}]."""
        files = sorted((self.root / "data" / "maps").glob("*/scripts.inc")) + sorted(
            (self.root / "data" / "scripts").glob("*.inc"))
        out = []
        for f in files:
            text = f.read_text(errors="replace")
            if "pokemart" not in text:
                continue
            for m in re.finditer(r"^\s*pokemart\s+(\w+)", text, re.M):
                lm = re.search(rf"^{re.escape(m.group(1))}::?[ \t]*\n((?:[ \t]*(?:\.2byte\s+\w+|@[^\n]*)?[ \t]*\n)*)"
                               rf"[ \t]*(pokemartlistend|\.2byte\s+ITEM_NONE)?", text, re.M)
                if not lm:
                    continue
                clerk = None
                for cm in re.finditer(r"^(\w+)::?", text[:m.start()], re.M):
                    clerk = cm.group(1)
                rel = str(f.relative_to(self.root))
                out.append({"map": f.parent.name if f.parent.parent.name == "maps" else rel, "label": m.group(1),
                            "file": rel, "line": text.count("\n", 0, lm.start()) + 1, "clerk": clerk,
                            "items": re.findall(r"\.2byte\s+(ITEM_\w+)", lm.group(1))})
        seen, uniq = set(), []
        for x in out:
            if x["label"] not in seen:
                seen.add(x["label"])
                uniq.append(x)
        return uniq

    def set_mart(self, label: str, items: list[str]) -> dict:
        mart = next((m for m in self.marts() if m["label"] == label), None)
        if not mart:
            raise DecompError(f"no Poké Mart list {label}")
        known = self.known("ITEM_")
        items = [const_of("ITEM", x) for x in items if x and str(x).strip()]
        if not items:
            raise DecompError("a shop needs at least one item")
        for it in items:
            if known and it not in known:
                raise DecompError(f"unknown item {it}")
        p = self.root / mart["file"]
        text = p.read_text(errors="replace")
        lm = re.search(rf"^{re.escape(label)}(::?)[ \t]*\n((?:[ \t]*(?:\.2byte\s+\w+|@[^\n]*)?[ \t]*\n)*)", text, re.M)
        body = "".join(f"\t.2byte {it}\n" for it in items)
        self.dp._backup(mart["file"])
        p.write_text(text[:lm.start(2)] + body + text[lm.end(2):])
        self.dp._changed = None
        return next(m for m in self.marts() if m["label"] == label)

    # ------------------------------------------------------------------ difficulty curve
    def trainer_map_index(self) -> dict[str, list[str]]:
        """TRAINER_X -> maps whose scripts battle them (one pass over every map script)."""
        out: dict[str, list[str]] = {}
        for f in sorted((self.root / "data" / "maps").glob("*/scripts.inc")):
            text = f.read_text(errors="replace")
            for line in re.findall(r"^\s*trainerbattle\w*\s+([^\n]*)", text, re.M):
                for tid in re.findall(r"\bTRAINER_\w+", line):
                    # a second region's trainers are battled under their game ID (TRAINER_JOHTO_AL_HNS) but written
                    # in their .party file as TRAINER_AL_HNS (see playtest.game_trainer)
                    names = [tid] + (["TRAINER_" + tid[len("TRAINER_JOHTO_"):]] if tid.startswith("TRAINER_JOHTO_") else [])
                    for n in names:
                        maps = out.setdefault(n, [])
                        if f.parent.name not in maps:
                            maps.append(f.parent.name)
        return out

    @staticmethod
    def is_boss(trainer_class: str) -> bool:
        c = trainer_class.lower()
        return "leader" in c or "elite" in c or "champion" in c

    def difficulty(self) -> dict:
        """Trainer and wild Pokémon levels per story chapter, and the spots where the curve looks wrong."""
        story = self.dp.maps.story
        chapters = story.chapters()
        where = self.trainer_map_index()
        rows = [dict(c, trainers=[], wild=None, index=i) for i, c in enumerate(chapters)]
        unplaced = 0
        for t in self.trainers():
            levels = [lv for lv in t["levels"] if lv]
            if not levels or t["id"] not in where:
                continue
            maps = where[t["id"]]
            ch = next((story.chapter_of(m, chapters) for m in maps if story.chapter_of(m, chapters) is not None), None)
            if ch is None:
                unplaced += 1
                continue
            rows[ch]["trainers"].append({"id": t["id"], "name": t["name"] or t["id"], "class": t["class"],
                                         "map": maps[0], "ace": max(levels), "avg": round(sum(levels) / len(levels), 1),
                                         "team": len(levels), "boss": self.is_boss(t["class"])})
        try:
            data = self._wild_json()
        except DecompError:
            data = {"wild_encounter_groups": []}
        by_id = self.dp.maps.map_by_id
        for g in data.get("wild_encounter_groups", []):
            if not g.get("for_maps"):
                continue
            for e in g.get("encounters", []):
                name = by_id.get(e.get("map"))
                ch = story.chapter_of(name, chapters) if name else None
                if ch is None:
                    continue
                # grass and caves only: surfing, fishing and Rock Smash need HMs and items from later chapters
                lv = [(x["min_level"], x["max_level"]) for x in e.get("land_mons", {}).get("mons", [])]
                if not lv:
                    continue
                w = rows[ch]["wild"] or {"min": 999, "max": 0, "maps": []}
                w["min"], w["max"] = min(w["min"], min(a for a, _ in lv)), max(w["max"], max(b for _, b in lv))
                w["maps"].append(name)
                rows[ch]["wild"] = w
        flags = []
        prev_boss = prev_median = None
        for r in rows:
            ts = sorted(r["trainers"], key=lambda t: t["ace"])
            normal = [t for t in ts if not t["boss"]]
            aces = sorted(t["ace"] for t in (normal or ts))
            r["median"] = aces[len(aces) // 2] if aces else None
            bosses = [t for t in ts if t["boss"]]
            r["boss"] = max(bosses, key=lambda t: t["ace"]) if bosses else None
            boss_ace = r["boss"]["ace"] if r["boss"] else None
            label = f"{r['index'] + 1}. {r['name']}"

            def flag(kind, msg, t=None):
                flags.append({"chapter": r["index"], "kind": kind, "message": msg,
                              "trainer": t["id"] if t else None, "map": t["map"] if t else None})
            for t in normal:
                if boss_ace is not None and t["ace"] > boss_ace + 2:
                    flag("stronger than the boss", f"{t['name']} ({t['class']}, lv {t['ace']}) in {t['map']} is "
                         f"stronger than this chapter's boss {r['boss']['name']} (lv {boss_ace})", t)
                elif r["median"] and t["ace"] >= r["median"] + max(6, r["median"] * 35 // 100):
                    flag("spike", f"{t['name']} ({t['class']}, lv {t['ace']}) in {t['map']} is much stronger than "
                         f"the other trainers of chapter {label} (about lv {r['median']})", t)
                if prev_median and t["ace"] <= prev_median - max(6, prev_median * 35 // 100):
                    flag("too easy", f"{t['name']} ({t['class']}, lv {t['ace']}) in {t['map']} is much weaker than "
                         f"the trainers of the chapter before (about lv {prev_median})", t)
            if boss_ace is not None and prev_boss is not None and boss_ace < prev_boss:
                flag("boss weaker", f"{r['boss']['name']} (lv {boss_ace}) is weaker than the previous chapter's boss "
                     f"(lv {prev_boss})", r["boss"])
            if r["wild"] and boss_ace is not None and r["wild"]["max"] > boss_ace:
                flags.append({"chapter": r["index"], "kind": "wild too strong", "trainer": None,
                              "map": r["wild"]["maps"][0],
                              "message": f"wild Pokémon go up to lv {r['wild']['max']}, above this chapter's boss "
                                         f"(lv {boss_ace})"})
            prev_boss = boss_ace if boss_ace is not None else prev_boss
            prev_median = r["median"] if r["median"] else prev_median
        seen, uniq = set(), []
        for f in flags:  # rivals have one trainer per starter: the same warning once
            if f["message"] not in seen:
                seen.add(f["message"])
                uniq.append(f)
        return {"chapters": [{k: r[k] for k in ("index", "name", "trainers", "wild", "median", "boss")} for r in rows],
                "flags": uniq, "unplaced_trainers": unplaced}

    # ------------------------------------------------------------------ checks
    def check(self, changed: dict[str, str]) -> list[tuple[str, str, str]]:
        issues = []
        for rel in self._party_files():
            if rel not in changed:
                continue
            for t in self._parse_party(rel):
                for p in self.validate_trainer({"mons": t["mons"]}):
                    issues.append(("error", f"{rel}:{t['line']}", f"{t['id']}: {p}"))
        items = self.known("ITEM_")
        for m in self.marts():
            if m["file"] in changed and items:
                for it in m["items"]:
                    if it not in items:
                        issues.append(("error", f"{m['file']}:{m['line']}", f"shop {m['label']}: unknown item {it}"))
        if TMS_FILE in changed:
            t = self.tms()
            moves = self.known("MOVE_")
            for kind in ("tms", "hms"):
                lst = [x["move"] for x in t[kind]]
                for mv in lst:
                    if moves and mv not in moves:
                        issues.append(("error", TMS_FILE, f"{kind[:2].upper()}: unknown move {mv}"))
                if t[f"max_{kind}"] and len(lst) > t[f"max_{kind}"]:
                    issues.append(("error", TMS_FILE, f"{len(lst)} {kind.upper()} but only {t[f'max_{kind}']} items"))
            for mv in {x["move"] for x in t["tms"]} & {x["move"] for x in t["hms"]}:
                issues.append(("error", TMS_FILE, f"{mv} is a TM and an HM"))
        for rel, prefix in ((ITEMS_FILE, "ITEM_"), (MOVES_FILE, "MOVE_")):
            if rel in changed and (self.root / rel).exists():
                define, default = NAME_LENGTHS[prefix]
                limit = self._value(define) or default
                for const, info in self._table_index([rel], prefix).items():
                    name = _game_text(self._entry_fields(info)[0].get("name", ""))
                    if len(name) > limit:
                        issues.append(("error", f"{rel}:{info['line']}", f"{const}: name '{name}' is longer than "
                                                                         f"{limit} letters"))
        if WILD_FILE in changed:
            try:
                data = self._wild_json()
            except (ValueError, DecompError) as e:
                return issues + [("error", WILD_FILE, f"not valid JSON: {e}")]
            species = self.known("SPECIES_")
            maps = set(self.dp.maps.map_by_id) if self.dp.maps else set()
            for g in data.get("wild_encounter_groups", []):
                if not g.get("for_maps"):
                    continue
                slots = {f["type"]: len(f.get("encounter_rates", [])) for f in g.get("fields", [])}
                for e in g.get("encounters", []):
                    if maps and e.get("map") not in maps:
                        issues.append(("warning", WILD_FILE, f"{e.get('map')}: no such map"))
                    for k in WILD_KINDS:
                        if k in e:
                            mons = e[k].get("mons", [])
                            if k in slots and len(mons) != slots[k]:
                                issues.append(("error", WILD_FILE, f"{e.get('map')} {k}: {len(mons)} slots, needs "
                                                                   f"{slots[k]}"))
                            for x in mons:
                                if species and x.get("species") not in species:
                                    issues.append(("error", WILD_FILE, f"{e.get('map')}: unknown {x.get('species')}"))
                                if not 1 <= int(x.get("min_level", 0)) <= int(x.get("max_level", 0)) <= 100:
                                    issues.append(("error", WILD_FILE, f"{e.get('map')}: bad levels {x}"))
        return issues


def _game_text(value: str) -> str:
    """A C string value as the player reads it: real newlines instead of \\n."""
    return c_strings(value).replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def _string_end(text: str, i: int) -> int:
    """Index just past the C string literal starting at text[i] == '"'."""
    j = i + 1
    while j < len(text) and text[j] != '"':
        j += 2 if text[j] == "\\" else 1
    return j + 1


def c_strings(value: str) -> str:
    """The text of a C value made of string literals: '_("A")', 'COMPOUND_STRING("a\\n" "b")' -> 'a\\nb'."""
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', value))


def c_string_value(old: str, text: str, indent: str = "        ", default: str = "_") -> str:
    """Write ``text`` (game text, with real newlines) the way ``old`` was written: same wrapper macro
    (_, COMPOUND_STRING, ITEM_NAME), one literal per line when it has several lines. When ``old`` isn't a string
    (a shared text symbol), ``default`` is the wrapper: COMPOUND_STRING for text pointers like descriptions."""
    wrapper = (re.match(r"\s*(\w+)\s*\(", old) or [None, default])[1]
    if default == "COMPOUND_STRING" and wrapper == "_":  # _() makes an array, which a text pointer can't hold
        wrapper = default
    esc = text.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "")
    pieces = esc.split("\n")
    if len(pieces) == 1:
        return f'{wrapper}("{pieces[0]}")'
    lits = [f'"{p}\\n"' for p in pieces[:-1]] + [f'"{pieces[-1]}"']
    return f"{wrapper}(\n" + "\n".join(indent + "    " + l for l in lits) + ")"


def _split_top(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
            continue
        depth += ch in "({"
        depth -= ch in ")}"
        cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _top_level_groups(s: str) -> list[str]:
    """'{A, 1, B}, {C, 2, D, X({..})}' -> ['A, 1, B', 'C, 2, D, X({..})']."""
    out, depth, start = [], 0, None
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(s[start:i])
    return out

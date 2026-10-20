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
WILD_FILE = "src/data/wild_encounters.json"
SPECIES_DIR = "src/data/pokemon/species_info"
LEARNSET_DIR = "src/data/pokemon/level_up_learnsets"
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
        return [f for f in PARTY_FILES if (self.root / f).exists()]

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
            for m in re.finditer(rf"\btrainerbattle\w*\s+{re.escape(tid)}\b", text):
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
        self.dp._backup(WILD_FILE)
        (self.root / WILD_FILE).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        self.dp._changed = None
        return self.wild(map_name)

    # ------------------------------------------------------------------ Pokémon (species)
    def _species_index(self) -> dict:
        if self._species is not None:
            return self._species
        out = {}
        for f in sorted((self.root / SPECIES_DIR).glob("*.h")):
            raw = f.read_text(errors="replace")
            text = strip_comments(raw)
            act = active_lines(text, self.defines)
            line_starts = [0]
            for ln in text.split("\n")[:-1]:
                line_starts.append(line_starts[-1] + len(ln) + 1)
            for m in re.finditer(r"^\s*\[(SPECIES_\w+)\]\s*=\s*\{", text, re.M):
                lineno = text.count("\n", 0, m.start())
                if not act[lineno]:
                    continue
                depth, i = 1, m.end()
                while depth and i < len(text):
                    depth += {"{": 1, "}": -1}.get(text[i], 0)
                    i += 1
                rel = str(f.relative_to(self.root))
                out.setdefault(m.group(1), {"file": rel, "start": m.end(), "end": i - 1, "line": lineno + 1})
        self._species = out
        return out

    def _species_fields(self, sid: str) -> tuple[dict, dict]:
        """({field: value text}, {field: (start, end) of the value in the file}) for the compiled lines."""
        idx = self._species_index()
        if sid not in idx:
            raise DecompError(f"unknown species {sid}")
        info = idx[sid]
        text = strip_comments((self.root / info["file"]).read_text(errors="replace"))
        body_start, body_end = info["start"], info["end"]
        first_line = text.count("\n", 0, body_start)
        act = active_lines(text, self.defines)
        values, spans = {}, {}
        for m in re.finditer(r"\.(\w+)\s*=\s*", text[body_start:body_end]):
            a = body_start + m.end()
            if not act[text.count("\n", 0, a)]:
                continue
            depth, j = 0, a
            while j < body_end:
                ch = text[j]
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
            values[m.group(1)] = text[a:j].strip()
            spans[m.group(1)] = (a, j)
        del first_line
        self._local_defines = {m.group(1): m.group(2).strip()
                               for ln, on in zip(text.split("\n"), act) if on
                               for m in [re.match(r"\s*#\s*define\s+(\w+)\s+(.+)", ln)] if m}
        return values, spans

    def _value(self, expr: str) -> int | None:
        expr = expr.strip()
        m = re.fullmatch(r"(.+?)\?(.+?):(.+)", expr, re.S)
        if m:
            return self._value(m.group(2) if _cpp_value(m.group(1), self.defines) else m.group(3))
        if re.fullmatch(r"\d+", expr):
            return int(expr)
        v = self.defines.get(expr)
        return int(v) if v and v.isdigit() else None

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
                "front_pic": self.dp.symbol_file.get(values.get("frontPic", "").strip()),
                "icon": self.dp.symbol_file.get(values.get("iconSprite", "").strip())}

    def set_species(self, sid: str, changes: dict) -> dict:
        """Change stats ({baseHP: 50, …}), catchRate, expYield, types ([TYPE_X, TYPE_Y]), abilities ([A, B, C]),
        evolutions ([{method, param, target, extra}]) and/or learnset ([{level, move}])."""
        sid = const_of("SPECIES", sid)
        values, spans = self._species_fields(sid)
        info = self._species_index()[sid]
        repl: dict[str, str] = {}
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
            if "evolutions" not in spans and evs:
                raise DecompError(f"{sid} has no .evolutions line to change; add one by hand first")
            if "evolutions" in spans:
                repl["evolutions"] = f"EVOLUTION({groups})" if evs else "NULL"
        for k in repl:
            if k not in spans:
                raise DecompError(f"{sid} has no .{k} field to change")
        path = self.root / info["file"]
        raw = path.read_text(errors="replace")
        for k in sorted(repl, key=lambda k: spans[k][0], reverse=True):
            a, b = spans[k]
            raw = raw[:a] + repl[k] + raw[b:]
        if repl:
            self.dp._backup(info["file"])
            path.write_text(raw)
            self._species = None
            self.dp._changed = None
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

    # ------------------------------------------------------------------ checks
    def check(self, changed: dict[str, str]) -> list[tuple[str, str, str]]:
        issues = []
        for rel in self._party_files():
            if rel not in changed:
                continue
            for t in self._parse_party(rel):
                for p in self.validate_trainer({"mons": t["mons"]}):
                    issues.append(("error", f"{rel}:{t['line']}", f"{t['id']}: {p}"))
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

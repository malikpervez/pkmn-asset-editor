"""Story chapters and the items a player would have collected by each one.

Test saves can start "as if you had played up to chapter N": the player gets the items from the events in the
maps of the earlier chapters (and that chapter), and those events are marked as done so they don't happen twice.

* **Chapters** are an ordered list of map names (a name also covers its sub-maps, so ``RustboroCity`` includes
  ``RustboroCity_Gym``) plus flags that are on once the chapter is finished (badges). A default for Emerald ships
  with the tool; any change (moving a map to another chapter, for maps you added) is saved to
  ``.pae/story.json``.
* **Item sources** are read from the maps: item balls (``Common_EventScript_FindItem`` / ``finditem``), hidden
  items (``hidden_item`` signs) and gifts in map scripts (``giveitem``, with the ``setflag`` that follows). Items
  that are *needed* (key items and HMs: ``.importance`` or the key items pocket in ``src/data/items.h``) are told
  apart from *extra* ones. Story items handed over later (``removeitem`` in a later map) are left out.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .decomp import DecompError, strip_comments

# Games that name maps with a suffix (PalletTown_Frlg, PalletTown_PlayersHouse_1F_Frlg; GoldenrodCity_hns...)
SUFFIXES = ("_Frlg", "_hns", "_Hns", "_frlg")


def covers(chapter_map: str, map_name: str) -> bool:
    """A chapter's map covers itself and its sub-maps: RustboroCity covers RustboroCity_Gym, and PalletTown_Frlg
    covers PalletTown_PlayersHouse_1F_Frlg (the same suffix, at the end)."""
    if map_name == chapter_map or map_name.startswith(chapter_map + "_"):
        return True
    for s in SUFFIXES:
        if chapter_map.endswith(s) and map_name.endswith(s):
            base, name = chapter_map[: -len(s)], map_name[: -len(s)]
            return name == base or name.startswith(base + "_")
    return False


# Emerald's story, in the order a player meets it. Each entry's maps also cover their sub-maps (name + "_...").
EMERALD_CHAPTERS = [
    {"name": "Littleroot Town & Route 101 (get your first Pokémon)", "maps": ["InsideOfTruck", "LittlerootTown", "Route101"]},
    {"name": "Oldale Town & Route 103 (first rival battle)", "maps": ["OldaleTown", "Route103"]},
    {"name": "Route 102 & Petalburg City (meet Dad)", "maps": ["Route102", "PetalburgCity"]},
    {"name": "Route 104 & Petalburg Woods", "maps": ["Route104", "PetalburgWoods"]},
    {"name": "Rustboro City: Badge 1 (Roxanne)", "maps": ["RustboroCity", "Route115"], "flags": ["FLAG_BADGE01_GET"]},
    {"name": "Route 116 & Rusturf Tunnel (get the Devon Goods back)", "maps": ["Route116", "RusturfTunnel"]},
    {"name": "Dewford Town: Badge 2 (Brawly) & Granite Cave", "maps": ["DewfordTown", "Route106", "GraniteCave"],
     "flags": ["FLAG_BADGE02_GET"]},
    {"name": "Route 109 & Slateport City", "maps": ["Route109", "SlateportCity"]},
    {"name": "Route 110 & Mauville City: Badge 3 (Wattson)", "maps": ["Route110", "MauvilleCity", "NewMauville"],
     "flags": ["FLAG_BADGE03_GET"]},
    {"name": "Route 117 & Verdanturf Town", "maps": ["Route117", "VerdanturfTown"]},
    {"name": "Routes 111-113, Fiery Path & Fallarbor Town",
     "maps": ["Route111", "Route112", "FieryPath", "Route113", "FallarborTown", "DesertRuins", "MirageTower"]},
    {"name": "Route 114, Meteor Falls, Mt. Chimney & Jagged Pass",
     "maps": ["Route114", "MeteorFalls", "MtChimney", "JaggedPass", "MagmaHideout"]},
    {"name": "Lavaridge Town: Badge 4 (Flannery)", "maps": ["LavaridgeTown"], "flags": ["FLAG_BADGE04_GET"]},
    {"name": "Petalburg Gym: Badge 5 (Dad) & Surf",
     "maps": ["PetalburgCity_Gym", "PetalburgCity_WallysHouse", "Route105", "Route107", "Route108", "AbandonedShip",
              "IslandCave"], "flags": ["FLAG_BADGE05_GET"]},
    {"name": "Routes 118 & 119, Weather Institute & Fortree City: Badge 6 (Winona)",
     "maps": ["Route118", "Route119", "FortreeCity"], "flags": ["FLAG_BADGE06_GET"]},
    {"name": "Routes 120-123, Safari Zone, Lilycove City & Mt. Pyre",
     "maps": ["Route120", "Route121", "Route122", "Route123", "SafariZone", "LilycoveCity", "MtPyre", "ScorchedSlab",
              "AncientTomb"]},
    {"name": "Team Aqua Hideout & Routes 124-125", "maps": ["AquaHideout", "Route124", "Route125", "ShoalCave"]},
    {"name": "Mossdeep City: Badge 7 (Tate & Liza)", "maps": ["MossdeepCity", "Route126", "Route127"],
     "flags": ["FLAG_BADGE07_GET"]},
    {"name": "Seafloor Cavern & Sootopolis City: Badge 8 (Juan)",
     "maps": ["Route128", "Underwater", "SeafloorCavern", "SootopolisCity", "CaveOfOrigin", "SkyPillar",
              "SealedChamber"], "flags": ["FLAG_BADGE08_GET"]},
    {"name": "Pacifidlog Town, Ever Grande City & Victory Road",
     "maps": ["Route129", "Route130", "Route131", "Route132", "Route133", "Route134", "PacifidlogTown",
              "EverGrandeCity", "VictoryRoad"]},
]


@dataclass
class ItemSource:
    item: str
    qty: int
    map: str
    kind: str  # "item ball", "hidden item", "gift"
    flag: str | None  # set once collected
    needed: bool  # key item / HM
    chapter: int | None = None
    name: str = ""  # display name, e.g. "Potion"
    where: str = ""  # script label or event


class Story:
    def __init__(self, maps):
        self.maps = maps
        self.dp = maps.dp
        self.root: Path = maps.root
        self._items_info: dict | None = None

    # ------------------------------------------------------------------ chapters
    @property
    def path(self) -> Path:
        return self.root / ".pae" / "story.json"

    def chapters(self) -> list[dict]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                return [dict(c, maps=list(c.get("maps", [])), flags=list(c.get("flags", [])))
                        for c in data.get("chapters", [])]
            except ValueError:
                pass
        names = set(self.maps.maps)
        chapters = []
        for c in EMERALD_CHAPTERS:
            present = [m for m in c["maps"] if any(n == m or n.startswith(m + "_") for n in names)]
            chapters.append({"name": c["name"], "maps": present, "flags": list(c.get("flags", []))})
        if not any(c["maps"] for c in chapters):
            return [{"name": "Chapter 1", "maps": [], "flags": []}]
        return chapters

    def save_chapters(self, chapters: list[dict]) -> None:
        for c in chapters:
            if not str(c.get("name", "")).strip():
                raise DecompError("every chapter needs a name")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"chapters": [{"name": c["name"], "maps": c.get("maps", []),
                                                       "flags": c.get("flags", [])} for c in chapters]}, indent=2))

    def chapter_of(self, map_name: str, chapters: list[dict] | None = None) -> int | None:
        """The chapter a map belongs to (the most specific name that covers it wins)."""
        best, best_len = None, -1
        for i, c in enumerate(chapters if chapters is not None else self.chapters()):
            for m in c["maps"]:
                if map_name != m and map_name.endswith("_Frlg") and not m.endswith("_Frlg"):
                    continue  # Kanto's VictoryRoad_1F_Frlg is not part of Hoenn's VictoryRoad
                if covers(m, map_name) and len(m) > best_len:
                    best, best_len = i, len(m)
        return best

    def set_chapter(self, map_name: str, chapter: int | None) -> None:
        """Put a map (and its sub-maps) in a chapter, or take it out of the story order (None)."""
        map_name = self.maps.resolve(map_name)
        chapters = self.chapters()
        for c in chapters:
            c["maps"] = [m for m in c["maps"] if m != map_name]
        if chapter is not None:
            if not 0 <= chapter < len(chapters):
                raise DecompError(f"there is no chapter {chapter + 1}")
            if self.chapter_of(map_name, chapters) != chapter:
                chapters[chapter]["maps"].append(map_name)
        elif self.chapter_of(map_name, chapters) is not None:
            raise DecompError(f"{map_name} is covered by "
                              f"'{chapters[self.chapter_of(map_name, chapters)]['name']}' through a parent map; "
                              "move it to another chapter instead")
        self.save_chapters(chapters)

    def add_chapter(self, name: str, at: int | None = None, flags: list[str] | None = None) -> int:
        """Add a new chapter (at position ``at``, default the end); returns its index."""
        chapters = self.chapters()
        if not chapters[0]["maps"] and len(chapters) == 1 and chapters[0]["name"] == "Chapter 1" \
                and not self.path.exists():
            chapters = []
        at = len(chapters) if at is None else max(0, min(at, len(chapters)))
        chapters.insert(at, {"name": name.strip(), "maps": [], "flags": list(flags or [])})
        self.save_chapters(chapters)
        return at

    def play_state(self, chapter: int | None = None, extras: bool = False, skip=None, team=None,
                   clear_team: bool = False, state=None) -> list[str]:
        """All story-state lines for a play-test: the catch-up for ``chapter``, then the team, then ``state``."""
        lines: list[str] = []
        if chapter is not None:
            lines += self.catch_up(int(chapter), extras, set(skip or []))["state"]
        if clear_team and team:
            lines.append("CLEAR_TEAM")
        lines += [t for t in (team or []) if str(t).strip()]
        if isinstance(state, str):
            state = [x for x in re.split(r"[,\n]", state) if x.strip()]
        return lines + list(state or [])

    def unplaced_maps(self) -> list[str]:
        chapters = self.chapters()
        return [n for n in self.maps.maps if self.chapter_of(n, chapters) is None]

    # ------------------------------------------------------------------ items
    def items_info(self) -> dict[str, dict]:
        """ITEM_X -> {name, pocket, needed} from src/data/items.h."""
        if self._items_info is not None:
            return self._items_info
        out: dict[str, dict] = {}
        p = self.root / "src" / "data" / "items.h"
        if p.exists():
            text = strip_comments(p.read_text(errors="replace"))
            starts = list(re.finditer(r"\[(ITEM_\w+)\]\s*=\s*\{", text))
            for i, m in enumerate(starts):
                body = text[m.end(): starts[i + 1].start() if i + 1 < len(starts) else len(text)]
                nm = re.search(r'\.name\s*=\s*(?:ITEM_NAME|COMPOUND_STRING|_)\(\s*"([^"]*)"', body)
                pocket = re.search(r"\.pocket\s*=\s*(\w+)", body)
                imp = re.search(r"\.importance\s*=\s*(\d+)", body)
                pocket_name = pocket.group(1) if pocket else ""
                label = nm.group(1) if nm else _pretty(m.group(1))
                tm = re.fullmatch(r"ITEM_(?:TM|HM)_?(\w+)", m.group(1))
                if tm and re.fullmatch(r"[TH]M\d+", label) and not tm.group(1).isdigit():
                    label = f"{label} {_pretty(tm.group(1))}"  # "HM06" -> "HM06 Rock Smash"
                out[m.group(1)] = {"name": label, "pocket": pocket_name,
                                   "needed": bool(imp and int(imp.group(1))) or pocket_name == "POCKET_KEY_ITEMS"}
        self._items_info = out
        return out

    def _script_blocks(self, map_name: str) -> list[tuple[str, str]]:
        d = self.root / "data" / "maps" / map_name
        blocks: list[tuple[str, str]] = []
        for f in ("scripts.inc", "scripts.pory"):
            p = d / f
            if not p.exists():
                continue
            text = re.sub(r"@[^\n]*|//[^\n]*", "", p.read_text(errors="replace"))
            label, cur = "", []
            for line in text.splitlines():
                m = re.match(r"^(\w+)::?\s*$", line) or re.match(r"^script\s+(\w+)\s*\{", line)
                if m:
                    if cur:
                        blocks.append((label, "\n".join(cur)))
                    label, cur = m.group(1), []
                else:
                    cur.append(line)
            if cur:
                blocks.append((label, "\n".join(cur)))
        return blocks

    def item_sources(self) -> list[ItemSource]:
        info = self.items_info()
        chapters = self.chapters()
        out: list[ItemSource] = []

        def add(item, qty, map_name, kind, flag, where):
            meta = info.get(item, {})
            out.append(ItemSource(item, max(1, qty), map_name, kind, flag, bool(meta.get("needed")),
                                  self.chapter_of(map_name, chapters), meta.get("name") or _pretty(item), where))

        for map_name in self.maps.maps:
            try:
                data = self.maps.get_map(map_name)
            except (OSError, ValueError, DecompError):
                continue
            blocks = self._script_blocks(map_name)
            by_label = dict(blocks)
            for i, e in enumerate(data.get("object_events", []) or []):
                script, flag = e.get("script", ""), e.get("flag") or None
                flag = None if flag in ("0", "0x0", "FLAG_NONE") else flag
                if script == "Common_EventScript_FindItem":
                    item = str(e.get("trainer_sight_or_berry_tree_id", ""))
                    if item.startswith("ITEM_"):
                        add(item, int(e.get("quantity", 1) or 1), map_name, "item ball", flag, f"item ball {i}")
                elif script in by_label:
                    m = re.search(r"\bfinditem\s*\(?\s*(ITEM_\w+)\s*(?:,\s*(\d+))?", by_label[script])
                    if m:
                        add(m.group(1), int(m.group(2) or 1), map_name, "item ball", flag, script)
            for e in data.get("bg_events", []) or []:
                if e.get("type") == "hidden_item" and str(e.get("item", "")).startswith("ITEM_"):
                    add(e["item"], int(e.get("quantity", 1) or 1), map_name, "hidden item", e.get("flag") or None,
                        "hidden item")
            gifts = set()
            for label, body in blocks:
                for m in re.finditer(r"\b(?:giveitem|additem)\s*\(?\s*(ITEM_\w+)\s*(?:,\s*(\d+))?", body):
                    if m.group(1) in gifts:
                        continue  # the same gift in another branch of the conversation
                    gifts.add(m.group(1))
                    after = body[m.end():]
                    stop = re.search(r"\b(?:end|return|goto|release)\b", after)
                    fm = re.search(r"\bsetflag\s*\(?\s*(FLAG_\w+)", after[: stop.start() if stop else len(after)])
                    add(m.group(1), int(m.group(2) or 1), map_name, "gift", fm.group(1) if fm else None, label)
        return out

    def _removals(self) -> list[tuple[str, str, int | None]]:
        chapters = self.chapters()
        out = []
        for map_name in self.maps.maps:
            for _, body in self._script_blocks(map_name):
                for m in re.finditer(r"\bremoveitem\s*\(?\s*(ITEM_\w+)", body):
                    out.append((m.group(1), map_name, self.chapter_of(map_name, chapters)))
                # setvar VAR_0x8004, ITEM_LETTER + call Common_EventScript_PlayerHandedOverTheItem
                if re.search(r"\bcall\s*\(?\s*\w*HandedOver\w*", body):
                    for m in re.finditer(r"\bsetvar\s*\(?\s*VAR_\w+\s*,\s*(ITEM_\w+)", body):
                        out.append((m.group(1), map_name, self.chapter_of(map_name, chapters)))
        return out

    def catch_up(self, chapter: int, extras: bool = True, skip: set[str] | None = None) -> dict:
        """What a player has by the end of ``chapter``: items (needed + extras), the flags that mark those
        events as done, and the chapters' own flags (badges)."""
        chapters = self.chapters()
        if not 0 <= chapter < len(chapters):
            raise DecompError(f"there is no chapter {chapter + 1}")
        removals = self._removals()
        items = []
        for s in self.item_sources():
            if s.chapter is None or s.chapter > chapter or (not s.needed and not extras):
                continue
            # story items handed over somewhere else (the Devon Goods, the Letter) are gone by then
            if any(it == s.item and m != s.map and c is not None and c <= chapter for it, m, c in removals):
                continue
            items.append(s)
        skip = skip or set()
        chosen = [s for s in items if _key(s) not in skip]
        flags = list(dict.fromkeys(f for c in chapters[: chapter + 1] for f in c.get("flags", [])))
        return {"chapter": chapter, "name": chapters[chapter]["name"], "items": [dict(asdict(s), key=_key(s))
                                                                                 for s in items],
                "flags": flags, "state": self.state_lines(chosen, flags)}

    @staticmethod
    def state_lines(items: list[ItemSource], flags: list[str]) -> list[str]:
        """Story state lines (see playtest.parse_state) giving the items and marking their events done."""
        totals: dict[str, int] = {}
        for s in items:
            totals[s.item] = totals.get(s.item, 0) + s.qty
        lines = [f"{item}*{n}" for item, n in totals.items()]
        lines += list(dict.fromkeys([*flags, *(s.flag for s in items if s.flag)]))
        return lines


    # ------------------------------------------------------------------ story flow
    def flow(self, include_temp: bool = False) -> dict:
        """Which events change and check which flags/vars, map by map in chapter order.

        {"chapters": [{name, maps: [{map, events: [{label, trigger, sets, clears, needs, vars_set, vars_checked}]}]}],
         "unplaced": [...], "flags": {FLAG_X: {"set_by": [..], "checked_by": [..]}}}"""
        chapters = self.chapters()
        triggers_by_map: dict[str, dict[str, str]] = {}
        names = {v: k for k, v in self.maps.map_by_id.items()}
        per_map: dict[str, list[dict]] = {}
        usage: dict[str, dict[str, list]] = {}
        temp = re.compile(r"^(FLAG_TEMP_|VAR_TEMP_|FLAG_HIDE_MAP_NAME_POPUP$|VAR_RESULT$|VAR_0x)")

        def use(name, how, ref):
            if not include_temp and temp.match(name):
                return
            usage.setdefault(name, {"set_by": [], "checked_by": []})[how].append(ref)

        for map_name in self.maps.maps:
            try:
                data = self.maps.get_map(map_name)
            except (OSError, ValueError, DecompError):
                continue
            trig: dict[str, str] = {}
            for e in data.get("object_events", []) or []:
                if e.get("script"):
                    who = str(e.get("graphics_id", "")).replace("OBJ_EVENT_GFX_", "").replace("_", " ").title()
                    trig.setdefault(e["script"], f"talk to {who} at ({e.get('x')}, {e.get('y')})")
                    fl = e.get("flag")
                    if fl and str(fl).startswith("FLAG_"):
                        use(fl, "checked_by", {"map": map_name, "label": e["script"],
                                               "how": f"{who} disappears while it is on"})
            for e in data.get("coord_events", []) or []:
                if e.get("script"):
                    trig.setdefault(e["script"], f"step on ({e.get('x')}, {e.get('y')})")
                    if str(e.get("var", "")).startswith("VAR_"):
                        use(e["var"], "checked_by", {"map": map_name, "label": e["script"],
                                                     "how": f"step trigger works while it is {e.get('var_value')}"})
            for e in data.get("bg_events", []) or []:
                if e.get("script") and e.get("type", "sign") == "sign":
                    trig.setdefault(e["script"], f"read the sign at ({e.get('x')}, {e.get('y')})")
            triggers_by_map[map_name] = trig
            events = []
            for label, body in self._script_blocks(map_name):
                ev = {"label": label, "trigger": trig.get(label, ""), "sets": [], "clears": [], "needs": [],
                      "vars_set": [], "vars_checked": []}
                if label.endswith(("_OnTransition", "_OnLoad", "_OnResume", "_OnFrame", "_OnWarp", "_MapScripts")):
                    ev["trigger"] = "when the map loads"
                for m in re.finditer(r"\bsetflag\s*\(?\s*(FLAG_\w+)", body):
                    ev["sets"].append(m.group(1))
                for m in re.finditer(r"\bclearflag\s*\(?\s*(FLAG_\w+)", body):
                    ev["clears"].append(m.group(1))
                for m in re.finditer(r"\b(?:goto|call)_if_(set|unset)\s*\(?\s*(FLAG_\w+)", body):
                    ev["needs"].append(f"{m.group(2)} {'on' if m.group(1) == 'set' else 'off'}")
                for m in re.finditer(r"\b(?:setvar|addvar)\s*\(?\s*(VAR_\w+)\s*,\s*(\w+)", body):
                    ev["vars_set"].append(f"{m.group(1)} = {m.group(2)}")
                for m in re.finditer(r"\b(?:goto|call)_if_(eq|ne|lt|le|gt|ge)\s*\(?\s*(VAR_\w+)\s*,\s*(\w+)", body):
                    op = {"eq": "=", "ne": "≠", "lt": "<", "le": "≤", "gt": ">", "ge": "≥"}[m.group(1)]
                    ev["vars_checked"].append(f"{m.group(2)} {op} {m.group(3)}")
                for m in re.finditer(r"\bmap_script_2\s+(VAR_\w+)\s*,\s*(\w+)\s*,\s*(\w+)", body):
                    ev["vars_checked"].append(f"{m.group(1)} = {m.group(2)} → {m.group(3)}")
                    use(m.group(1), "checked_by", {"map": map_name, "label": m.group(3),
                                                   "how": f"starts when it is {m.group(2)}"})
                for k in ev:
                    if isinstance(ev[k], list):
                        ev[k] = list(dict.fromkeys(ev[k]))
                if not include_temp:
                    for k in ("sets", "clears", "needs", "vars_set", "vars_checked"):
                        ev[k] = [x for x in ev[k] if not temp.match(x)]
                ref = {"map": map_name, "label": label}
                for f in ev["sets"]:
                    use(f, "set_by", dict(ref, how="turns it on"))
                for f in ev["clears"]:
                    use(f, "set_by", dict(ref, how="turns it off"))
                for f in ev["needs"]:
                    use(f.split()[0], "checked_by", dict(ref, how=f"needs it {f.split()[1]}"))
                for v in ev["vars_set"]:
                    use(v.split()[0], "set_by", dict(ref, how=f"sets it to {v.split()[-1]}"))
                for v in ev["vars_checked"]:
                    if "→" not in v:
                        use(v.split()[0], "checked_by", dict(ref, how="checks " + " ".join(v.split()[1:])))
                if any(ev[k] for k in ("sets", "clears", "needs", "vars_set", "vars_checked")):
                    events.append(ev)
            if events:
                per_map[map_name] = events
        out_chapters = []
        placed = set()
        for i, c in enumerate(chapters):
            maps_here = [n for n in self.maps.maps if self.chapter_of(n, chapters) == i]
            placed.update(maps_here)
            out_chapters.append({"name": c["name"], "flags": c.get("flags", []),
                                 "maps": [{"map": n, "events": per_map.get(n, [])} for n in maps_here]})
        unplaced = [{"map": n, "events": per_map.get(n, [])} for n in self.maps.maps if n not in placed]
        del names
        return {"chapters": out_chapters, "unplaced": unplaced, "usage": usage}

    def suggest_chapters(self) -> list[dict]:
        """For maps not in a chapter: the chapter of the nearest map they connect to by doors or edges."""
        chapters = self.chapters()
        by_id = self.maps.map_by_id
        links: dict[str, set[str]] = {n: set() for n in self.maps.maps}
        for n in self.maps.maps:
            try:
                data = self.maps.get_map(n)
            except (OSError, ValueError, DecompError):
                continue
            for w in data.get("warp_events", []) or []:
                dest = by_id.get(w.get("dest_map", ""))
                if dest and dest != n:
                    links[n].add(dest)
                    links.setdefault(dest, set()).add(n)
            for c in data.get("connections", []) or []:
                dest = by_id.get(c.get("map", ""))
                if dest:
                    links[n].add(dest)
                    links.setdefault(dest, set()).add(n)
        known = {n: self.chapter_of(n, chapters) for n in self.maps.maps}
        out = []
        for n, ch in known.items():
            if ch is not None:
                continue
            seen, frontier, found = {n}, [n], None
            for _ in range(6):  # up to 6 doors away
                nxt = []
                for x in frontier:
                    if x != n and len(links.get(x, ())) > 8:
                        continue  # hubs like the cable club rooms link every Pokémon Center: don't walk through them
                    for y in links.get(x, ()):
                        if y in seen:
                            continue
                        seen.add(y)
                        if known.get(y) is not None:
                            found = (known[y], y) if found is None or known[y] < found[0] else found
                        nxt.append(y)
                if found:
                    break
                frontier = nxt
            if found:
                out.append({"map": n, "chapter": found[0], "chapter_name": chapters[found[0]]["name"], "via": found[1]})
        return out

    def apply_suggestions(self, maps: list[str] | None = None) -> int:
        sugg = self.suggest_chapters()
        if maps is not None:
            sugg = [s for s in sugg if s["map"] in set(maps)]
        chapters = self.chapters()
        for s in sugg:
            if self.chapter_of(s["map"], chapters) != s["chapter"]:
                chapters[s["chapter"]]["maps"].append(s["map"])
        self.save_chapters(chapters)
        return len(sugg)


def _key(s: ItemSource) -> str:
    return f"{s.map}:{s.where}:{s.item}"


def _pretty(const: str) -> str:
    return re.sub(r"^(ITEM|SPECIES|MOVE)_", "", const).replace("_", " ").title()

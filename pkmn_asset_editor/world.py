"""The whole world at once: outdoor maps stitched together by their connections, and the region map with its fly spots.

* **World layout**: every map with connections is placed by walking them (``right``: the next map starts where this
  one ends, moved down by ``offset``; ``up``: it ends where this one starts, moved right by ``offset``…). Maps that
  reach each other form one *area* (Hoenn, Kanto, the Battle Frontier…). Connections that don't match on both sides,
  or that make two maps overlap, are reported.
* **Attach a new map**: a copy of a map you pick, connected to a side of an existing map in both directions.
* **Region map**: the town map's picture (``graphics/pokenav/region_map/map.png`` + ``map.bin``), the map section each
  8×8 square belongs to (``sRegionMap_MapSectionLayout`` in ``src/data/region_map/region_map_layout.h``) and the
  sections' names and boxes (``src/data/region_map/region_map_sections.json``).
* **Fly spots**: a town is a fly spot when ``sMapHealLocations`` in ``src/region_map.c`` gives its section a heal
  location (``src/data/heal_locations.json``) and ``GetMapsecType`` says it can be flown to (usually once a
  ``FLAG_VISITED_…`` flag is set).
"""

from __future__ import annotations

import json
import re
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

from .decomp import DecompError

SIDES = {"right": "left", "left": "right", "up": "down", "down": "up"}
LAYOUT_H = "src/data/region_map/region_map_layout.h"
SECTIONS_JSON = "src/data/region_map/region_map_sections.json"
REGION_MAP_C = "src/region_map.c"
HEAL_JSON = "src/data/heal_locations.json"
MAP_PNG = "graphics/pokenav/region_map/map.png"
MAP_BIN = "graphics/pokenav/region_map/map.bin"
CELL_X0, CELL_Y0 = 1, 2  # MAPCURSOR_X_MIN / _Y_MIN: where section (0, 0) is on the map picture, in 8×8 squares


class World:
    def __init__(self, maps):
        self.maps = maps
        self.dp = maps.dp
        self.root = Path(maps.root)

    # ------------------------------------------------------------------ stitched world
    def _size(self, name: str) -> tuple[int, int]:
        lay = self.maps.layout_of(name)
        return int(lay.get("width", 0)), int(lay.get("height", 0))

    def layout(self) -> dict:
        """{"areas": [{"maps": [{name, x, y, w, h, …}], "x", "y", "w", "h"}], "problems": [...]}, biggest area first.
        Positions and sizes are in blocks (16×16 pixels)."""
        m = self.maps
        conns = {}
        for name in m.maps:
            try:
                data = m.get_map(name)
            except DecompError:
                continue
            cs = [c for c in (data.get("connections") or []) if c.get("direction") in SIDES]
            if cs:
                conns[name] = cs
        placed, problems = {}, []
        story = getattr(m, "story", None)
        chapters = story.chapters() if story else []
        areas = []
        for start in sorted(conns, key=lambda n: (not n.startswith(("LittlerootTown", "PalletTown")), n)):
            if start in placed:
                continue
            area = []
            placed[start] = (0, 0)
            q = deque([start])
            while q:
                a = q.popleft()
                ax, ay = placed[a]
                aw, ah = self._size(a)
                area.append(a)
                for c in conns.get(a, []):
                    b = m.map_by_id.get(c.get("map"))
                    if not b:
                        problems.append({"map": a, "message": f"{a} connects {c['direction']} to {c.get('map')}, "
                                                              "which doesn't exist"})
                        continue
                    bw, bh = self._size(b)
                    off = int(c.get("offset") or 0)
                    pos = {"right": (ax + aw, ay + off), "left": (ax - bw, ay + off),
                           "down": (ax + off, ay + ah), "up": (ax + off, ay - bh)}[c["direction"]]
                    back = [x for x in conns.get(b, []) if m.map_by_id.get(x.get("map")) == a
                            and x.get("direction") == SIDES[c["direction"]]]
                    if not back:
                        problems.append({"map": a, "message": f"{a} connects {c['direction']} to {b}, but {b} doesn't "
                                                              f"connect {SIDES[c['direction']]} back"})
                    elif int(back[0].get("offset") or 0) != -off:
                        problems.append({"map": a, "message": f"{a} → {b} has offset {off} but {b} → {a} has "
                                                              f"{back[0].get('offset')} (should be {-off})"})
                    if b in placed:
                        if placed[b] != pos and b in area:
                            problems.append({"map": b, "note": True,
                                             "message": f"{b} is placed differently from {a} than from its other "
                                                        "neighbours (the maps don't fit together exactly)"})
                        continue
                    placed[b] = pos
                    q.append(b)
            rows = []
            for n in area:
                x, y = placed[n]
                w, h = self._size(n)
                data = m.get_map(n)
                rows.append({"name": n, "id": data.get("id"), "x": x, "y": y, "w": w, "h": h,
                             "section": data.get("region_map_section"), "type": data.get("map_type"),
                             "chapter": story.chapter_of(n, chapters) if story else None})
            x0 = min(r["x"] for r in rows)
            y0 = min(r["y"] for r in rows)
            for r in rows:
                r["x"] -= x0
                r["y"] -= y0
            for i, r in enumerate(rows):  # maps drawn on top of each other
                for s in rows[i + 1:]:
                    if r["x"] < s["x"] + s["w"] and s["x"] < r["x"] + r["w"] and r["y"] < s["y"] + s["h"] \
                            and s["y"] < r["y"] + r["h"]:
                        problems.append({"map": r["name"], "note": True,
                                         "message": f"{r['name']} and {s['name']} overlap"})
            areas.append({"maps": rows, "w": max(r["x"] + r["w"] for r in rows),
                          "h": max(r["y"] + r["h"] for r in rows), "first": start})
        areas.sort(key=lambda a: -len(a["maps"]))
        seen, uniq = set(), []
        for p in problems:
            if p["message"] not in seen:
                seen.add(p["message"])
                uniq.append(p)
        return {"areas": areas, "problems": [p for p in uniq if not p.get("note")],
                "notes": [p for p in uniq if p.get("note")], "chapters": [c["name"] for c in chapters]}

    def attach_map(self, existing: str, side: str, template: str, new_name: str, offset: int = 0) -> dict:
        """A new map, copied from ``template``, joined to ``side`` of ``existing`` (both ways)."""
        if side not in SIDES:
            raise DecompError("side must be up, down, left or right")
        m = self.maps
        existing, template = m.resolve(existing), m.resolve(template)
        data = m.get_map(existing)
        if any(c.get("direction") == side for c in data.get("connections") or []):
            other = next(c["map"] for c in data["connections"] if c.get("direction") == side)
            if not self._fits_beside(existing, side, other, offset, template):
                raise DecompError(f"{existing} already has {other} on its {side} side there")
        m.duplicate_map(template, new_name, copy_layout=True)
        m.reload()
        new = m.get_map(new_name)
        new["connections"] = [{"map": data["id"], "offset": -int(offset), "direction": SIDES[side]}]
        new.setdefault("connections_no_include", False)
        m.save_map(new_name, new)
        data = m.get_map(existing)
        data["connections"] = list(data.get("connections") or []) + [
            {"map": new["id"], "offset": int(offset), "direction": side}]
        m.save_map(existing, data)
        m.reload()
        return {"map": new_name, "id": new["id"], "connected_to": existing, "side": side}

    def _fits_beside(self, existing, side, other, offset, template) -> bool:
        """Two maps can share a side when they don't overlap along it (e.g. two routes below a wide town)."""
        m = self.maps
        axis = 0 if side in ("up", "down") else 1
        for c in m.get_map(existing).get("connections") or []:
            if c.get("direction") != side or c.get("map") != other:
                continue
            o = int(c.get("offset") or 0)
            ow = self._size(m.map_by_id.get(other, other))[axis]
            nw = self._size(template)[axis]
            if not (offset >= o + ow or offset + nw <= o):
                return False
        return True

    def render_area(self, area: dict, scale: float = 0.25) -> Image.Image:
        """One picture of a whole area (scale 0.25 = 4 pixels per block)."""
        px = max(1, int(16 * scale))
        out = Image.new("RGB", (area["w"] * px, area["h"] * px), (24, 24, 30))
        for r in area["maps"]:
            try:
                im = self.maps.render(r["name"], events=False)
            except Exception:  # noqa: BLE001 - a broken map shouldn't stop the whole picture
                continue
            out.paste(im.convert("RGB").resize((r["w"] * px, r["h"] * px), Image.BOX), (r["x"] * px, r["y"] * px))
        return out

    # ------------------------------------------------------------------ region map
    def region_map_image(self) -> Image.Image | None:
        """The town map picture as the game draws it: an affine background, 8-bit tiles from map.png and one byte per
        square of map.bin (64×64), of which the screen shows the top-left 240×160."""
        png, binf = self.root / MAP_PNG, self.root / MAP_BIN
        if not (png.exists() and binf.exists()):
            return None
        with Image.open(png) as im:
            idx = np.array(im.convert("P") if im.mode != "P" else im)
            pal = im.getpalette()
        tw = idx.shape[1] // 8
        n = (idx.shape[0] // 8) * tw
        tiles = idx[: (n // tw) * 8].reshape(n // tw, 8, tw, 8).transpose(0, 2, 1, 3).reshape(n, 8, 8)
        raw = np.frombuffer(binf.read_bytes(), dtype=np.uint8)
        mw = 64 if len(raw) >= 4096 else 32
        mh = len(raw) // mw
        t = np.where(raw < n, raw, 0)[: mw * mh].reshape(mh, mw)
        out = tiles[t].transpose(0, 2, 1, 3).reshape(mh * 8, mw * 8)
        im = Image.fromarray(np.ascontiguousarray(out), "P")
        im.putpalette(pal)
        return im.convert("RGB").crop((0, 0, 240, 160))

    def _layout_grid(self) -> tuple[list[list[str]], tuple[int, int]] | tuple[None, None]:
        p = self.root / LAYOUT_H
        if not p.exists():
            return None, None
        text = p.read_text(errors="replace")
        m = re.search(r"sRegionMap_MapSectionLayout\s*\[[^\]]*\]\s*\[[^\]]*\]\s*=\s*\{(.*?)\n\};", text, re.S)
        if not m:
            return None, None
        rows = [re.findall(r"MAPSEC_\w+", r) for r in re.findall(r"\{([^{}]*)\}", m.group(1))]
        return rows, m.span(1)

    def region_map(self) -> dict:
        """The Hoenn region map: its grid of sections, the sections (name, box, maps in it, fly spot)."""
        grid, _ = self._layout_grid()
        sections = []
        if (self.root / SECTIONS_JSON).exists():
            sections = json.loads((self.root / SECTIONS_JSON).read_text()).get("map_sections", [])
        fly = self.fly_spots()
        maps_in: dict[str, list[str]] = {}
        for name in self.maps.maps:
            try:
                sec = self.maps.get_map(name).get("region_map_section")
            except DecompError:
                continue
            maps_in.setdefault(sec, []).append(name)
        on_grid = {c for row in grid or [] for c in row}
        out = []
        for s in sections:
            sid = s.get("id")
            out.append({"id": sid, "name": s.get("name", ""), "x": s.get("x"), "y": s.get("y"),
                        "w": s.get("width"), "h": s.get("height"), "on_map": sid in on_grid,
                        "maps": sorted(maps_in.get(sid, []))[:40], "fly": fly.get(sid)})
        return {"grid": grid or [], "cell": 8, "origin": [CELL_X0, CELL_Y0], "sections": out,
                "heal_locations": [h["id"] for h in self._heal()], "has_image": (self.root / MAP_PNG).exists()}

    def set_region_grid(self, grid: list[list[str]]) -> dict:
        """Save which section each square of the region map is (and the sections' boxes to match)."""
        cur, span = self._layout_grid()
        if cur is None:
            raise DecompError(f"no sRegionMap_MapSectionLayout in {LAYOUT_H}")
        if len(grid) != len(cur) or any(len(a) != len(b) for a, b in zip(grid, cur)):
            raise DecompError(f"the region map is {len(cur[0])}×{len(cur)} squares")
        known = set(self.maps.constants("MAPSEC_")) | {c for row in cur for c in row} | {
            x.get("id") for x in json.loads((self.root / SECTIONS_JSON).read_text()).get("map_sections", [])} \
            if (self.root / SECTIONS_JSON).exists() else set(self.maps.constants("MAPSEC_"))
        for row in grid:
            for c in row:
                if known and c not in known:
                    raise DecompError(f"unknown map section {c}")
        p = self.root / LAYOUT_H
        text = p.read_text(errors="replace")
        body = "\n" + "".join("    {" + ", ".join(row) + "},\n" for row in grid)
        self.dp._backup(LAYOUT_H)
        p.write_text(text[:span[0]] + body + text[span[1]:])
        # each section's box (x, y, width, height) is where its squares are
        sp = self.root / SECTIONS_JSON
        if sp.exists():
            raw = sp.read_text()
            data = json.loads(raw)
            for s in data.get("map_sections", []):
                cells = [(x, y) for y, row in enumerate(grid) for x, c in enumerate(row) if c == s.get("id")]
                if cells:
                    xs, ys = [c[0] for c in cells], [c[1] for c in cells]
                    s.update({"x": min(xs), "y": min(ys), "width": max(xs) - min(xs) + 1, "height": max(ys) - min(ys) + 1})
            self._write_json(SECTIONS_JSON, data, raw)
        self.dp._changed = None
        return self.region_map()

    def rename_section(self, section: str, name: str) -> dict:
        sp = self.root / SECTIONS_JSON
        raw = sp.read_text()
        data = json.loads(raw)
        hit = next((s for s in data.get("map_sections", []) if s.get("id") == section), None)
        if not hit:
            raise DecompError(f"unknown map section {section}")
        if not name.strip() or len(name) > 18:
            raise DecompError("give the place a name of at most 18 letters")
        hit["name"] = name.strip().upper() if hit.get("name", "").isupper() else name.strip()
        self._write_json(SECTIONS_JSON, data, raw)
        self.dp._changed = None
        return {"id": section, "name": hit["name"]}

    def add_section(self, name: str) -> dict:
        """A new place for the town map (MAPSEC_<NAME>), added at the end so no other place's number changes."""
        sp = self.root / SECTIONS_JSON
        raw = sp.read_text()
        data = json.loads(raw)
        secs = data.get("map_sections", [])
        words = re.findall(r"[A-Za-z0-9]+", name)
        if not words or len(name.strip()) > 18:
            raise DecompError("give the place a name of at most 18 letters")
        sid = "MAPSEC_" + "_".join(w.upper() for w in words)
        if any(x.get("id") == sid for x in secs):
            raise DecompError(f"{sid} already exists")
        if len(secs) >= 0xFC:
            raise DecompError("the town map has no room for more places (at most 252)")
        secs.append({"id": sid, "name": name.strip().upper(), "x": 0, "y": 0, "width": 1, "height": 1})
        self._write_json(SECTIONS_JSON, data, raw)
        if self.maps._constants is not None:
            self.maps._constants = None
        self.dp._changed = None
        return {"id": sid, "name": name.strip().upper()}

    def _write_json(self, rel: str, data, old: str) -> None:
        out = json.dumps(data, indent=2, ensure_ascii="\\u00" in old)
        self.dp._backup(rel)
        (self.root / rel).write_text(out + ("\n" if old.endswith("\n") else ""))

    # ------------------------------------------------------------------ fly spots
    def _heal(self) -> list[dict]:
        p = self.root / HEAL_JSON
        return json.loads(p.read_text()).get("heal_locations", []) if p.exists() else []

    def fly_spots(self) -> dict[str, dict]:
        """MAPSEC -> {map, heal_location, flag} for every section you can fly to."""
        p = self.root / REGION_MAP_C
        if not p.exists():
            return {}
        text = p.read_text(errors="replace")
        out = {}
        m = re.search(r"sMapHealLocations\s*\[\][^=]*=\s*\{(.*?)\n\};", text, re.S)
        if m:
            for e in re.finditer(r"\[(MAPSEC_\w+)\]\s*=\s*\{\s*MAP_GROUP\((\w+)\)\s*,\s*MAP_NUM\(\w+\)\s*,\s*(\w+)\s*\}",
                                 m.group(1)):
                out[e.group(1)] = {"map": e.group(2), "heal_location": e.group(3), "flag": None, "flyable": False}
        f = re.search(r"static u8 GetMapsecType\(mapsec_u16_t mapSecId\)\s*\{(.*?)\n\}", text, re.S)
        if f:
            for c in re.finditer(r"case\s+(MAPSEC_\w+):\s*return\s+([^;]+);", f.group(1)):
                if c.group(1) in out and "CANFLY" in c.group(2):
                    fl = re.search(r"FlagGet\((\w+)\)", c.group(2))
                    out[c.group(1)].update({"flyable": True, "flag": fl.group(1) if fl else None})
        heal = {h["id"]: h for h in self._heal()}
        for v in out.values():
            h = heal.get(v["heal_location"], {})
            v.update({"x": h.get("x"), "y": h.get("y"), "map_name": self.maps.map_by_id.get(v["map"])})
        return out

    def set_fly_spot(self, section: str, map_name: str, x: int, y: int, flag: str | None = None) -> dict:
        """Make a section a fly spot: you land on ``map_name`` at (x, y). With ``flag`` (e.g. FLAG_VISITED_MY_TOWN)
        you can fly there once the flag is on; without one, always. The map's arrival script turns the flag on."""
        m = self.maps
        name = m.resolve(map_name)
        map_id = m.get_map(name)["id"]
        if section not in set(m.constants("MAPSEC_")) | {s["id"] for s in self.region_map()["sections"]}:
            raise DecompError(f"unknown map section {section}")
        heal_id = "HEAL_LOCATION_" + map_id[len("MAP_"):]
        # 1. the heal location (where you land, and where you go after losing a battle)
        hp = self.root / HEAL_JSON
        raw = hp.read_text()
        data = json.loads(raw)
        cur = next((h for h in data["heal_locations"] if h["id"] == heal_id), None)
        entry = {"id": heal_id, "map": map_id, "x": int(x), "y": int(y), "respawn_map": map_id,
                 "respawn_npc": "LOCALID_NONE", "respawn_x": int(x), "respawn_y": int(y)}
        if cur:
            cur.update({"map": map_id, "x": int(x), "y": int(y)})
        else:
            data["heal_locations"].append(entry)
        self._write_json(HEAL_JSON, data, raw)
        # 2. sMapHealLocations: the section flies to that heal location
        cp = self.root / REGION_MAP_C
        text = cp.read_text(errors="replace")
        line = f"    [{section}] = {{MAP_GROUP({map_id}), MAP_NUM({map_id}), {heal_id}}},"
        m2 = re.search(r"sMapHealLocations\s*\[\][^=]*=\s*\{(.*?)\n\};", text, re.S)
        if not m2:
            raise DecompError(f"no sMapHealLocations in {REGION_MAP_C}")
        body = m2.group(1)
        if re.search(rf"\[{section}\]", body):
            body = re.sub(rf"^[ \t]*\[{section}\][^\n]*", line, body, count=1, flags=re.M)
        else:
            body = body.rstrip() + "\n" + line
        text = text[:m2.start(1)] + body + text[m2.end(1):]
        # 3. GetMapsecType: it can be flown to (when the flag is on)
        ret = f"FlagGet({flag}) ? MAPSECTYPE_CITY_CANFLY : MAPSECTYPE_CITY_CANTFLY" if flag else "MAPSECTYPE_CITY_CANFLY"
        f = re.search(r"static u8 GetMapsecType\(mapsec_u16_t mapSecId\)\s*\{(.*?)\n\}", text, re.S)
        if not f:
            raise DecompError(f"no GetMapsecType in {REGION_MAP_C}")
        fb = f.group(1)
        case = f"    case {section}:\n        return {ret};"
        if re.search(rf"case\s+{section}:", fb):
            fb = re.sub(rf"case\s+{section}:\s*return\s+[^;]+;", case.strip(), fb, count=1)
        else:
            fb = re.sub(r"(\n[ \t]*default:)", "\n" + case + r"\1", fb, count=1)
        text = text[:f.start(1)] + fb + text[f.end(1):]
        self.dp._backup(REGION_MAP_C)
        cp.write_text(text)
        # 4. the arrival script turns the flag on
        if flag:
            self._set_flag_on_arrival(name, flag)
        self.dp._changed = None
        return {"section": section, **self.fly_spots().get(section, {})}

    def _set_flag_on_arrival(self, name: str, flag: str) -> None:
        m = self.maps
        script = m.read_script(name)
        if re.search(rf"\bsetflag\s+{flag}\b", script):
            return
        trans = re.search(r"map_script\s+MAP_SCRIPT_ON_TRANSITION\s*,\s*(\w+)", script)
        if trans:
            lab = re.search(rf"^{trans.group(1)}::?[ \t]*\n", script, re.M)
            if lab:
                script = script[:lab.end()] + f"\tsetflag {flag}\n" + script[lab.end():]
                m.write_script(name, script)
                return
        head = re.search(rf"^{name}_MapScripts::?[ \t]*\n", script, re.M)
        if not head:
            raise DecompError(f"{name} has no {name}_MapScripts to add an arrival script to")
        label = f"{name}_OnTransition"
        script = script[:head.end()] + f"\tmap_script MAP_SCRIPT_ON_TRANSITION, {label}\n" + script[head.end():]
        script = script.rstrip("\n") + f"\n\n{label}:\n\tsetflag {flag}\n\tend\n"
        m.write_script(name, script)

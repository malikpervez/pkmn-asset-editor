"""Moving events and walking routes for scripts.

* :func:`place` moves one event (an object by index or local id, a warp, a trigger, a sign) to another block.
* :func:`route` finds a walk between blocks over what the map lets you walk on (collision 0, not water unless
  surfing; other objects' starting blocks are avoided if asked), shortest first and then with the fewest turns.
  Sideways stairs are kept off: they move you diagonally, which a walk of single steps can't follow. A walk on a
  map (:func:`walk`) also keeps the game's rules for each step (:func:`steps`): not between two heights (an
  elevation other than 0 or 15 on both sides, and not the same), and not across a one-way edge (a block you can't
  leave or enter that way, e.g. ``MB_IMPASSABLE_NORTH``: not left going north, not entered going south).
* :func:`movement` turns it into movement commands (``walk_up`` …), and :func:`write_movement` puts them under a
  movement label in the map's script, replacing what was there up to its ``step_end``.

A cutscene where one character follows another: route the leader from A to B, and the follower from where it stands
through A to B, stopping one step short (``stop_short=1``), so it ends right behind the leader.
"""

from __future__ import annotations

import heapq
import re

from .decomp import DecompError
from .maps import EVENT_KINDS

DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
# blocks you can't leave going that way (src/metatile_behavior.c, MetatileBehavior_Is<Direction>Blocked)
BLOCKED = {
    "up": {"MB_IMPASSABLE_NORTH", "MB_IMPASSABLE_NORTHEAST", "MB_IMPASSABLE_NORTHWEST", "MB_IMPASSABLE_SOUTH_AND_NORTH",
           "MB_TALL_GRASS_IMPASSABLE_NORTH", "MB_CAVE_IMPASSABLE_NORTH"},
    "down": {"MB_IMPASSABLE_SOUTH", "MB_IMPASSABLE_SOUTHEAST", "MB_IMPASSABLE_SOUTHWEST", "MB_IMPASSABLE_SOUTH_AND_NORTH"},
    "right": {"MB_IMPASSABLE_EAST", "MB_IMPASSABLE_NORTHEAST", "MB_IMPASSABLE_SOUTHEAST", "MB_IMPASSABLE_WEST_AND_EAST",
              "MB_SECRET_BASE_BREAKABLE_DOOR"},
    "left": {"MB_IMPASSABLE_WEST", "MB_IMPASSABLE_NORTHWEST", "MB_IMPASSABLE_SOUTHWEST", "MB_IMPASSABLE_WEST_AND_EAST",
             "MB_SECRET_BASE_BREAKABLE_DOOR"},
}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
# (not DEEP: deep water says WATER, and deep sand and the deep south warp, a gangway's end, are land)
WATER = re.compile(r"WATER|OCEAN|POND|CURRENT|WATERFALL|PUDDLE_SURF", re.I)
OFF = re.compile(r"SIDEWAYS_STAIRS", re.I)  # walkable, but not in single steps (they move you diagonally)


def place(maps, map_name: str, kind: str, which: str, x: int, y: int, elevation: int | None = None) -> dict:
    """Move an event. ``kind``: object, warp, coord (trigger) or bg (sign); ``which``: its index, or for objects its
    local id."""
    key = kind if kind in EVENT_KINDS else f"{kind}_events"
    if key not in EVENT_KINDS:
        raise DecompError(f"event kind must be object, warp, coord or bg (not {kind})")
    name = maps.resolve(map_name)
    data = maps.get_map(name)
    events = data.get(key, [])
    idx = None
    if str(which).isdigit():
        idx = int(which)
    else:
        idx = next((i for i, e in enumerate(events) if e.get("local_id") == which), None)
    if idx is None or not 0 <= idx < len(events):
        raise DecompError(f"{name} has no {kind} event {which}")
    ev = events[idx]
    old = (ev.get("x"), ev.get("y"))
    ev["x"], ev["y"] = int(x), int(y)
    if elevation is not None:
        ev["elevation"] = int(elevation)
    maps.save_map(name, data)
    return {"kind": key, "index": idx, "from": old, "to": (int(x), int(y))}


def walkable(maps, map_name: str, surf: bool = False, avoid_objects: bool = False):
    """A [height][width] grid of blocks one can walk on (or surf on, with ``surf``)."""
    lay = maps.layout_of(map_name)
    blocks = maps.read_blocks(lay)
    behavior = _behaviors(maps, map_name, lay)
    h, w = blocks.shape
    grid = [[False] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            v = int(blocks[y, x])
            if (v >> 10) & 3:
                continue
            beh = behavior(v & 0x3FF)
            water = bool(WATER.search(beh)) or (v >> 12) == 1
            grid[y][x] = (water if surf else not water) and not OFF.search(beh)
    if avoid_objects:
        for e in maps.get_map(map_name).get("object_events", []):
            ex, ey = int(e.get("x", -1)), int(e.get("y", -1))
            if 0 <= ey < h and 0 <= ex < w:
                grid[ey][ex] = False
    return grid


def steps(maps, map_name: str):
    """The game's rule for one step on the map, ``ok(from, to, direction)``: not between two heights, not across a
    one-way edge (see the module notes)."""
    lay = maps.layout_of(map_name)
    blocks = maps.read_blocks(lay)
    of = _behaviors(maps, map_name, lay)

    def behavior(x, y):
        return of(int(blocks[y, x]) & 0x3FF)

    def ok(a, b, d):
        ea, eb = int(blocks[a[1], a[0]]) >> 12, int(blocks[b[1], b[0]]) >> 12
        if ea != eb and ea not in (0, 15) and eb not in (0, 15):
            return False
        return behavior(*a) not in BLOCKED[d] and behavior(*b) not in BLOCKED[OPPOSITE[d]]
    return ok


def _behaviors(maps, map_name: str, lay: dict):
    """``behavior(metatile id)``: its behaviour's name, from the map's tilesets, or for an id above them that the
    game makes for the map (its seams' adapters, its block fixes' metatiles: :func:`seams.engine_metatiles`) from
    the game's own."""
    te = maps.tileset_editor
    version = lay.get("layout_version", "emerald")
    cache: dict[int, str] = {}
    made: dict | None = None

    def behavior(mid: int) -> str:
        nonlocal made
        if mid not in cache:
            try:
                b, ver = int(te.get_metatile(map_name, mid).get("behavior", 0)), version
            except DecompError:
                if made is None:
                    from .seams import engine_metatiles
                    made = engine_metatiles(maps, map_name)
                if mid not in made:
                    raise
                attr, ver = made[mid]
                b = te._unpack_attr(attr, ver)["behavior"]
            names = te.behaviors(ver)
            cache[mid] = names[b] if b < len(names) else ""
        return cache[mid]
    return behavior


def route(grid, start: tuple[int, int], goal: tuple[int, int], ok=None) -> list[str]:
    """Directions from start to goal (the goal itself may be a door or other blocked block); ``ok(from, to,
    direction)``, if given, says whether a step is allowed (:func:`steps`)."""
    h, w = len(grid), len(grid[0])
    if start == goal:
        return []
    best: dict = {}
    q = [(0, 0, start, None, [])]
    while q:
        cost, turns, pos, last, path = heapq.heappop(q)
        if pos == goal:
            return path
        if best.get((pos, last), (1 << 30,))[0] <= cost:
            continue
        best[(pos, last)] = (cost,)
        for d, (dx, dy) in DIRS.items():
            nx, ny = pos[0] + dx, pos[1] + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            if (nx, ny) != goal and not grid[ny][nx]:
                continue
            if ok is not None and not ok(pos, (nx, ny), d):
                continue
            turn = int(last is not None and d != last)
            heapq.heappush(q, (cost + 100 + turn, turns + turn, (nx, ny), d, path + [d]))
    raise DecompError(f"no way to walk from {start} to {goal}")


def movement(directions: list[str], fast: bool = False) -> list[str]:
    return [f"walk_fast_{d}" if fast else f"walk_{d}" for d in directions]


def walk(maps, map_name: str, points: list[tuple[int, int]], surf: bool = False, avoid_objects: bool = False,
         stop_short: int = 0) -> tuple[list[str], tuple[int, int]]:
    """Directions through ``points`` in order (and where they end)."""
    if len(points) < 2:
        raise DecompError("give at least two blocks: where the walk starts and where it goes")
    grid = walkable(maps, map_name, surf, avoid_objects)
    ok = steps(maps, map_name)
    dirs: list[str] = []
    for a, b in zip(points, points[1:]):
        dirs += route(grid, a, b, ok)
    if stop_short:
        dirs = dirs[:max(0, len(dirs) - stop_short)]
    x, y = points[0]
    for d in dirs:
        x, y = x + DIRS[d][0], y + DIRS[d][1]
    return dirs, (x, y)


def write_movement(maps, map_name: str, label: str, commands: list[str]) -> None:
    """Replace the commands under ``label::`` (up to and including its ``step_end``) in the map's script."""
    text = maps.read_script(map_name)
    m = re.search(rf"^{re.escape(label)}::[ \t]*\n", text, re.M)
    if not m:
        raise DecompError(f"no {label}:: in {map_name}'s script")
    end = re.compile(r"^\s*step_end\b.*\n?", re.M).search(text, m.end())
    nxt = re.compile(r"^\w+::", re.M).search(text, m.end())
    if not end or (nxt and nxt.start() < end.start()):
        raise DecompError(f"{label} doesn't end with step_end before the next label")
    body = "".join(f"\t{c}\n" for c in commands) + "\tstep_end\n"
    maps.write_script(map_name, text[:m.end()] + body + text[end.end():])

"""Find and replace text across the whole game: dialogue in scripts, place names and trainer names.

* **Dialogue**: every text label (``Label:`` followed by ``.string "…"`` lines) in ``data/maps/*/scripts.inc``,
  ``data/scripts/*.inc`` and ``data/text/*.inc``. A match may span a line break ("PETALBURG\\nCITY" is found by
  "Petalburg City"); the line breaks stay where they were when the replacement has as many words, and a text that
  no longer fits its box is re-wrapped with the game's font.
* **Place names**: ``name`` of each section in ``src/data/region_map/region_map_sections.json`` (the map pop-up and
  the region map).
* **Trainer names**: ``Name:`` lines of ``src/data/*.party``.

The case of each match is kept: replacing "petalburg" with "Sunny" turns "PETALBURG" into "SUNNY".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .decomp import DecompError
from .textbox import LABEL_RE, STRING_RE

PLACES_FILE = "src/data/region_map/region_map_sections.json"
BREAK = r"(?:\\[nlp])"  # a line break code inside game text
WORD_CHARS = r"A-Za-z0-9À-ÿ"


def readable(raw: str) -> str:
    """Game text as a person reads it: \\n and \\l are new lines, \\p a blank line, no $."""
    return raw.replace("\\p", "\n\n").replace("\\n", "\n").replace("\\l", "\n").rstrip("$")


def _pattern(query: str, match_case: bool, whole_word: bool, game_text: bool) -> re.Pattern:
    words = query.split()
    if not words:
        raise DecompError("type something to look for")
    sep = rf"(?:\s|{BREAK})+" if game_text else r"\s+"
    body = sep.join(re.escape(w) for w in words)
    start = r"(?<!\\)"  # never the 'n' of a \n code
    if whole_word:
        body = rf"(?<![{WORD_CHARS}]){body}(?![{WORD_CHARS}])"
    return re.compile(start + body, 0 if match_case else re.I)


def _same_case(found: str, new: str) -> str:
    letters = [c for c in re.sub(BREAK, " ", found) if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(letters) > 1:
        return new.upper()
    if letters and all(c.islower() for c in letters):
        return new.lower()
    return new


def _fill(found: str, new: str, keep_case: bool) -> tuple[str, bool]:
    """The replacement for one match; keeps line breaks between words when it can. -> (text, needs a re-wrap)."""
    new = _same_case(found, new) if keep_case else new
    breaks = re.findall(rf"(?:\s|{BREAK})+", found)
    if not any("\\" in b for b in breaks):
        return new, False
    words = new.split()
    if len(words) == len(breaks) + 1:  # same number of words: keep each break where it was
        out = words[0]
        for b, w in zip(breaks, words[1:]):
            out += b + w
        return out, False
    return new, True  # the break can't be placed: re-wrap the whole text


class TextSearch:
    def __init__(self, dp):
        self.dp = dp
        self.root: Path = Path(dp.root)

    @property
    def tb(self):
        return self.dp.textbox

    def _script_files(self) -> list[Path]:
        r = self.root
        return sorted((r / "data" / "maps").glob("*/scripts.inc")) + sorted((r / "data" / "scripts").glob("*.inc")) \
            + sorted((r / "data" / "text").glob("*.inc"))

    def _labels(self, text: str):
        """(label, raw game text, start, end, line) for each text label: start/end cover its .string lines."""
        labels = list(LABEL_RE.finditer(text))
        for i, m in enumerate(labels):
            end = labels[i + 1].start() if i + 1 < len(labels) else len(text)
            body = text[m.end():end]
            first = re.search(r"\S", body)
            if not first or not body[first.start():].startswith(".string"):
                continue
            strings = list(re.finditer(r'^[ \t]*\.string[ \t]+"((?:[^"\\]|\\.)*)"[ \t]*\n?', body, re.M))
            if not strings:
                continue
            # the run of .string lines right after the label
            run = [strings[0]]
            for s in strings[1:]:
                if body[run[-1].end():s.start()].strip():
                    break
                run.append(s)
            yield (m.group(1), "".join(s.group(1) for s in run), m.end() + run[0].start(), m.end() + run[-1].end(),
                   text.count("\n", 0, m.start()) + 1)

    # ------------------------------------------------------------------ find
    def search(self, query: str, match_case: bool = False, whole_word: bool = False, places: bool = True,
               trainers: bool = True, limit: int = 1000) -> dict:
        rx = _pattern(query, match_case, whole_word, True)
        out = []
        for f in self._script_files():
            text = f.read_text(errors="replace")
            if not rx.search(re.sub(r'"[ \t]*\n[ \t]*\.string[ \t]+"', "", text)):
                continue
            rel = str(f.relative_to(self.root))
            for label, raw, _a, _b, line in self._labels(text):
                n = len(rx.findall(raw))
                if n:
                    out.append({"kind": "dialogue", "id": f"{rel}:{label}", "file": rel, "label": label, "line": line,
                                "map": f.parent.name if f.parent.parent.name == "maps" else None,
                                "text": readable(raw), "matches": n})
        plain = _pattern(query, match_case, whole_word, False)
        if places and (self.root / PLACES_FILE).exists():
            for sec in json.loads((self.root / PLACES_FILE).read_text()).get("map_sections", []):
                n = len(plain.findall(sec.get("name", "")))
                if n:
                    out.append({"kind": "place name", "id": f"{PLACES_FILE}:{sec['id']}", "file": PLACES_FILE,
                                "label": sec["id"], "line": None, "map": None, "text": sec["name"], "matches": n})
        if trainers:
            for rel in self._party_files():
                for i, ln in enumerate((self.root / rel).read_text(errors="replace").splitlines(), 1):
                    m = re.match(r"Name:\s*(.+)$", ln)
                    if m and plain.search(m.group(1)):
                        out.append({"kind": "trainer name", "id": f"{rel}:{i}", "file": rel, "label": m.group(1),
                                    "line": i, "map": None, "text": m.group(1), "matches": len(plain.findall(m.group(1)))})
        return {"query": query, "total": sum(r["matches"] for r in out), "results": out[:limit],
                "more": max(0, len(out) - limit)}

    def _party_files(self) -> list[str]:
        from .gamedata import built_party_files
        return built_party_files(self.root)

    # ------------------------------------------------------------------ replace
    def replace(self, query: str, replacement: str, match_case: bool = False, whole_word: bool = False,
                only: list[str] | None = None, keep_case: bool = True, dry_run: bool = False, places: bool = True,
                trainers: bool = True) -> dict:
        """Replace everywhere (or only in the result ids in ``only``). With dry_run, nothing is written: the result
        shows each text before and after."""
        rx = _pattern(query, match_case, whole_word, True)
        plain = _pattern(query, match_case, whole_word, False)
        want = set(only) if only else None
        changes, writes = [], {}

        def sub_plain(s: str) -> str:
            return plain.sub(lambda m: _same_case(m.group(0), replacement) if keep_case else replacement, s)

        for f in self._script_files():
            text = f.read_text(errors="replace")
            rel = str(f.relative_to(self.root))
            edits = []
            for label, raw, a, b, line in self._labels(text):
                if want is not None and f"{rel}:{label}" not in want:
                    continue
                if not rx.search(raw):
                    continue
                rewrap = False

                def one(m):
                    nonlocal rewrap
                    new, r = _fill(m.group(0), replacement, keep_case)
                    rewrap = rewrap or r
                    return new
                new_raw = rx.sub(one, raw)
                before = self.tb.layout(label, raw)
                after = self.tb.layout(label, new_raw)
                fits_before = {p for p in before.problems}
                if rewrap or any(p not in fits_before for p in after.problems):
                    new_raw, rewrap = self._rewrap(raw, new_raw, force=rewrap)
                    after = self.tb.layout(label, new_raw)
                indent = re.match(r"[ \t]*", text[a:b]).group(0) or "\t"
                block = "".join(f'{indent}.string "{ln}"\n' for ln in _split_lines(new_raw))
                edits.append((a, b, block))
                changes.append({"kind": "dialogue", "id": f"{rel}:{label}", "file": rel, "label": label, "line": line,
                                "before": readable(raw), "after": readable(new_raw), "rewrapped": rewrap,
                                "problems": [p for p in after.problems if p not in fits_before]})
            if edits:
                for a, b, block in sorted(edits, reverse=True):
                    text = text[:a] + block + text[b:]
                writes[rel] = text
        if places and (self.root / PLACES_FILE).exists():
            raw_json = (self.root / PLACES_FILE).read_text()
            data = json.loads(raw_json)
            hit = False
            for sec in data.get("map_sections", []):
                if want is not None and f"{PLACES_FILE}:{sec['id']}" not in want:
                    continue
                name = sec.get("name", "")
                if plain.search(name):
                    new = sub_plain(name)
                    changes.append({"kind": "place name", "id": f"{PLACES_FILE}:{sec['id']}", "file": PLACES_FILE,
                                    "label": sec["id"], "line": None, "before": name, "after": new, "rewrapped": False,
                                    "problems": [f"'{new}' is {len(new)} letters; place names fit about 16"]
                                    if len(new) > 16 else []})
                    sec["name"] = new
                    hit = True
            if hit:
                out = json.dumps(data, indent=2, ensure_ascii="\\u00" in raw_json)
                writes[PLACES_FILE] = out + ("\n" if raw_json.endswith("\n") else "")
        if trainers:
            for rel in self._party_files():
                lines = (self.root / rel).read_text(errors="replace").split("\n")
                hit = False
                for i, ln in enumerate(lines):
                    m = re.match(r"(Name:\s*)(.+)$", ln)
                    if not m or not plain.search(m.group(2)) or (want is not None and f"{rel}:{i + 1}" not in want):
                        continue
                    new = sub_plain(m.group(2))
                    limit = 10
                    changes.append({"kind": "trainer name", "id": f"{rel}:{i + 1}", "file": rel, "label": m.group(2),
                                    "line": i + 1, "before": m.group(2), "after": new, "rewrapped": False,
                                    "problems": [f"'{new}' is {len(new)} letters; trainer names can have {limit}"]
                                    if len(new) > limit else []})
                    lines[i] = m.group(1) + new
                    hit = True
                if hit:
                    writes[rel] = "\n".join(lines)
        if not dry_run:
            for rel, text in writes.items():
                self.dp._backup(rel)
                (self.root / rel).write_text(text)
            if writes:
                self.dp._changed = None
                if getattr(self.dp, "maps", None) is not None:
                    self.dp.maps.reload()
        return {"query": query, "replacement": replacement, "changes": changes, "files": sorted(writes),
                "dry_run": dry_run}

    def _rewrap(self, old: str, new: str, force: bool = False) -> tuple[str, bool]:
        """Re-wrap the boxes (\\p paragraphs) of ``new`` that changed and no longer fit (all changed ones with
        ``force``), keeping every other box exactly as written. -> (text, whether anything was re-wrapped)."""
        end = "$" if new.endswith("$") else ""
        olds, news = (old[:-1] if old.endswith("$") else old).split("\\p"), (new[:-1] if end else new).split("\\p")
        same_shape = len(olds) == len(news)
        out, did = [], False
        for i, para in enumerate(news):
            if same_shape and para == olds[i]:
                out.append(para)
                continue
            if not force and not self.tb.layout("", para + "$").problems:
                out.append(para)
                continue
            flat = re.sub(r"\s*\\[nl]\s*", " ", para).strip()
            out.append("".join(self.tb.wrap(flat))[:-1])  # wrap ends with $: this box's own end comes from the join
            did = True
        return "\\p".join(out) + end, did


def _split_lines(raw: str) -> list[str]:
    """Game text -> .string lines, one after each line break code (how the game's scripts are written)."""
    parts = re.split(r"(?<=\\[nlp])", raw)
    return [p for p in parts if p] or [""]

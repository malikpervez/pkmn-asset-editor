"""Add a brand-new Pokémon to a pokeemerald-expansion game, the way docs/tutorials/how_to_new_pokemon.md describes,
by copying an existing Pokémon (the *template*) and renaming everything that belongs to it.

What gets made, for a new Pokémon "Sparkit" copied from Pichu:

* ``SPECIES_SPARKIT`` in include/constants/species.h (between SPECIES_CUSTOM_START and SPECIES_CUSTOM_END, or before
  SPECIES_EGG in older versions) and ``NATIONAL_DEX_SPARKIT`` at the end of the National Pokédex, with
  NATIONAL_DEX_COUNT moved up and the Pokédex's name/height/weight orders updated.
* Its entry in src/data/pokemon/species_info/custom_families.h (included by species_info.h): the template's entry as
  your config compiles it, with its own name, Pokédex number and graphics. Form tables and evolutions are left out.
* Its own copies of everything the entry names: graphics (graphics/pokemon/sparkit/ and the INCGFX lines in
  src/data/graphics/pokemon.h), follower sprite tables, level-up learnsets (every gen_N.h), egg moves, and its
  TM/tutor moves in all_learnables.json.
* Then the stats, types, abilities and Pokédex text you give, an evolution from another Pokémon, and your pictures.

The cry is the template's (adding cries needs sound files converted with ffmpeg; see the tutorial).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .decomp import DecompError, strip_comments
from .gamedata import (LEARNABLES_FILE, TEACHABLE_FILE, _game_text, active_lines, const_of)

CUSTOM_FILE = "src/data/pokemon/species_info/custom_families.h"
SPECIES_INFO = "src/data/pokemon/species_info.h"
SPECIES_H = "include/constants/species.h"
POKEDEX_H = "include/constants/pokedex.h"
ORDERS_H = "src/data/pokemon/pokedex_orders.h"
DEF_DIRS = ("src/data/graphics", "src/data/object_events", "src/data/pokemon")


def names_for(name: str) -> dict:
    words = re.findall(r"[A-Za-z0-9]+", name.replace("é", "e").replace("♀", "F").replace("♂", "M"))
    if not words:
        raise DecompError("give the Pokémon a name with letters in it")
    camel = "".join(w[:1].upper() + w[1:].lower() for w in words)
    upper = "_".join(w.upper() for w in words)
    return {"name": name.strip(), "const": "SPECIES_" + upper, "dex": "NATIONAL_DEX_" + upper, "camel": camel,
            "dir": "_".join(w.lower() for w in words), "key": re.sub(r"(?!^)([A-Z]+)", r"_\1", camel).upper()}


class NewPokemon:
    def __init__(self, dp):
        self.dp = dp
        self.gd = dp.gamedata
        self.root = Path(dp.root)

    # ------------------------------------------------------------------ helpers
    def _read(self, rel: str) -> str:
        return (self.root / rel).read_text(errors="replace")

    def _write(self, rel: str, text: str) -> None:
        p = self.root / rel
        if p.exists():
            self.dp._backup(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        self._written.append(rel)

    def _template_entry(self, template: str) -> tuple[str, str, str]:
        """(file, compiled text of the template's entry, the template's CamelName in its symbols)."""
        idx = self.gd._species_index()
        if template not in idx:
            raise DecompError(f"unknown species {template}")
        info = idx[template]
        text, act = self.gd._parsed(info["file"])
        raw = self._read(info["file"])
        m = re.compile(rf"^[ \t]*\[{template}\]\s*=\s*\{{", re.M).search(text, max(0, info["start"] - 400))
        start = m.start() if m else text.rfind("\n", 0, info["start"]) + 1
        end = info["end"] + 1
        first = text.count("\n", 0, start)
        lines = raw[start:end].split("\n")
        keep = [ln for k, ln in enumerate(lines) if act[first + k] and not re.match(r"\s*#\s*(if|ifdef|ifndef|elif|else|"
                                                                                       r"endif)\b", ln)]
        body = "\n".join(keep)
        self.gd._entry_fields(info)  # fills _local_defines for this file
        for name, value in self.gd._local_defines.items():  # the template's file-local #defines won't be there
            body = re.sub(rf"\b{re.escape(name)}\b", value, body)
        camel = None
        for pat in (r"\.frontPic\s*=\s*gMonFrontPic_(\w+)", r"\.iconSprite\s*=\s*gMonIcon_(\w+)",
                    r"\.levelUpLearnset\s*=\s*s(\w+?)LevelUpLearnset"):
            hit = re.search(pat, body)
            if hit:
                camel = hit.group(1)
                break
        if not camel:
            raise DecompError(f"can't tell which graphics {template} uses; pick another template")
        return info["file"], body, camel

    def _definitions(self, symbol: str) -> list[tuple[str, str]]:
        """(file, text) of each compiled definition of ``symbol`` (``… symbol[] = …;``) in the game's data files."""
        out = []
        rx = re.compile(rf"^[^\n;{{}}]*\b{re.escape(symbol)}\s*\[[^\]]*\]\s*=", re.M)
        for d in DEF_DIRS:
            for f in sorted((self.root / d).rglob("*.h")):
                rel = str(f.relative_to(self.root))
                if rel == TEACHABLE_FILE and self.gd._helper_on():
                    continue  # generated by make_teachables.py from all_learnables.json
                raw = f.read_text(errors="replace")
                if symbol not in raw:
                    continue
                text = strip_comments(raw)
                act = None
                for m in rx.finditer(text):
                    if act is None:
                        act = active_lines(text, self.gd.defines)
                    if not act[text.count("\n", 0, m.start())]:
                        continue
                    depth, i = 0, m.end()
                    while i < len(text):
                        c = text[i]
                        if c == '"':
                            i = text.index('"', i + 1) + 1 if '"' in text[i + 1:] else len(text)
                            continue
                        depth += c in "({"
                        depth -= c in ")}"
                        if c == ";" and depth <= 0:
                            break
                        i += 1
                    out.append((rel, raw[m.start():i + 1]))
        return out

    # ------------------------------------------------------------------ plan / create
    def check(self, name: str, template: str) -> dict:
        n = names_for(name)
        self.gd._check_name("SPECIES_", n["name"])
        template = const_of("SPECIES", template)
        known = self.gd.known("SPECIES_") or set(self.gd._species_index())
        if n["const"] in known or n["const"] in self.gd._species_index():
            raise DecompError(f"{n['const']} already exists")
        if (self.root / "graphics" / "pokemon" / n["dir"]).exists():
            raise DecompError(f"graphics/pokemon/{n['dir']} already exists")
        _, body, camel = self._template_entry(template)
        warnings = []
        if re.search(r"\.formSpeciesIdTable|\.formChangeTable", body):
            warnings.append(f"{template} has other forms; the new Pokémon gets only this form")
        if re.search(r"\.is(Legendary|Mythical|UltraBeast|Paradox)\s*=\s*TRUE", body):
            warnings.append(f"{template} is a legendary/mythical Pokémon: those flags are copied too")
        return {"names": n, "template": template, "template_symbol": camel, "warnings": warnings}

    def create(self, name: str, template: str, changes: dict | None = None, evolves_from: dict | None = None,
               images: dict | None = None) -> dict:
        """Make the new Pokémon. ``changes`` are set_species fields (stats, types, abilities, category, height, weight,
        description, learnset…); ``evolves_from`` = {species, method, param}; ``images`` = {front|back|icon: image}."""
        plan = self.check(name, template)
        n, template, old = plan["names"], plan["template"], plan["template_symbol"]
        self._written: list[str] = []
        src_file, body, _ = self._template_entry(template)

        # every symbol the entry names that belongs to the template, and what they name in turn
        def own(sym):
            return sym.endswith("_" + old) or re.fullmatch(rf"s{old}[A-Z]\w*", sym)
        rename: dict[str, str] = {}
        todo = [s for s in re.findall(r"\b[gs]\w+", body) if own(s)]
        for macro in re.findall(rf"\b([A-Z_]+)\(\s*{old}\s*\)", body):
            if macro == "FOOTPRINT":
                todo.append(f"gMonFootprint_{old}")
        defs: dict[str, list[tuple[str, str]]] = {}
        while todo:
            sym = todo.pop()
            if sym in rename:
                continue
            rename[sym] = sym[:-len(old)] + n["camel"] if sym.endswith("_" + old) else "s" + n["camel"] + sym[1 + len(old):]
            found = self._definitions(sym)
            defs[sym] = found
            for _, text in found:
                todo += [s for s in re.findall(r"\b[gs]\w+", text) if own(s) and s not in rename]
        sym_rx = re.compile(r"\b(" + "|".join(map(re.escape, sorted(rename, key=len, reverse=True))) + r")\b") \
            if rename else None

        def renamed(text: str) -> str:
            text = sym_rx.sub(lambda m: rename[m.group(1)], text) if sym_rx else text
            return re.sub(r"graphics/pokemon/([\w/]+?)/(?=[\w.]+\")", lambda m: self._gfx_dir(m.group(1), n["dir"]), text)

        # graphics files the copied definitions point at
        self._gfx_dirs: dict[str, str] = {}
        additions: dict[str, list[str]] = {}
        for sym, found in defs.items():
            for rel, text in found:
                additions.setdefault(rel, []).append(renamed(text))
        copied = []
        for old_dir, new_dir in self._gfx_dirs.items():
            for rel_list in additions.values():
                for text in rel_list:
                    for f in re.findall(rf"graphics/pokemon/{re.escape(new_dir)}/([\w.]+)\"", text):
                        src = self.root / "graphics" / "pokemon" / old_dir / f
                        dst = self.root / "graphics" / "pokemon" / new_dir / f
                        if src.exists() and not dst.exists():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(src, dst)
                            copied.append(str(dst.relative_to(self.root)))
        for rel, texts in additions.items():
            cur = self._read(rel)
            self._write(rel, cur.rstrip("\n") + f"\n\n// {n['name']} (made from {template})\n" + "\n\n".join(texts) + "\n")

        # the species and Pokédex constants
        self._add_species_constant(n["const"])
        has_dex = self._add_dex_constant(n["dex"])

        # its entry
        entry = renamed(body)
        entry = re.sub(rf"\[{template}\]", f"[{n['const']}]", entry, count=1)
        entry = re.sub(rf"\b{old}\b(?=\s*\))", n["camel"], entry)  # FOOTPRINT(Pichu) and the like
        if has_dex:
            entry = re.sub(r"\.natDexNum\s*=\s*\w+", f".natDexNum = {n['dex']}", entry)
        entry = _drop_fields(entry, ("formSpeciesIdTable", "formChangeTable", "evolutions"))
        if not entry.rstrip().endswith(","):
            entry = entry.rstrip() + ","
        cur = self._read(CUSTOM_FILE) if (self.root / CUSTOM_FILE).exists() else \
            "// Your own Pokémon (made with pae new-pokemon). Included at the end of gSpeciesInfo in species_info.h.\n"
        self._write(CUSTOM_FILE, cur.rstrip("\n") + f"\n\n    // {n['name']}, made from {template}\n{entry}\n")
        self._include_custom_file()
        self._copy_learnables(template, n["key"])

        # its data, its place in the Pokédex orders, an evolution into it, its pictures
        self.gd._tables, self.gd._species, self.gd._names, self.gd._files = {}, None, None, {}
        if getattr(self.dp, "maps", None) is not None:
            self.dp.maps.reload()
            self.dp.maps._constants = None
        ch = dict(changes or {})
        ch.setdefault("name", n["name"])
        self.gd.set_species(n["const"], {k: v for k, v in ch.items() if v not in (None, "")})
        if has_dex:
            self._add_to_dex_orders(n)
        if evolves_from and evolves_from.get("species"):
            pre = const_of("SPECIES", evolves_from["species"])
            cur_evos = self.gd.get_species(pre)["evolutions"]
            self.gd.set_species(pre, {"evolutions": [*cur_evos, {"method": evolves_from.get("method") or "EVO_LEVEL",
                                                                 "param": str(evolves_from.get("param") or 16),
                                                                 "target": n["const"]}]})
        if hasattr(self.dp, "reindex"):
            self.dp.reindex()
        pictures = []
        for kind, img in (images or {}).items():
            pictures += self._set_picture(n["dir"], kind, img)
        self.dp._changed = None
        return {"species": n["const"], "name": n["name"], "template": template, "dex": n["dex"] if has_dex else None,
                "files": sorted(set(self._written + copied + pictures)), "graphics": f"graphics/pokemon/{n['dir']}",
                "warnings": plan["warnings"]}

    def _gfx_dir(self, old_dir: str, new_dir: str) -> str:
        """graphics/pokemon/pichu/ -> graphics/pokemon/sparkit/ (forms' sub-folders get their own sub-folder)."""
        if old_dir not in self._gfx_dirs:
            base = old_dir.split("/")[0]
            self._gfx_dirs[old_dir] = new_dir + old_dir[len(base):]
        return f"graphics/pokemon/{self._gfx_dirs[old_dir]}/"

    def _set_picture(self, folder: str, kind: str, img) -> list[str]:
        files = {"front": ("anim_front.png", "front.png"), "back": ("back.png",), "icon": ("icon.png",)}.get(kind, ())
        for f in files:
            rel = f"graphics/pokemon/{folder}/{f}"
            if rel in self.dp.assets:
                import io
                src = io.BytesIO(img) if isinstance(img, (bytes, bytearray)) else img
                self.dp.import_image(self.dp.assets[rel], src, new_palette=kind != "icon", resize=True)
                return [rel]
        return []

    # ------------------------------------------------------------------ constants
    def _add_species_constant(self, const: str) -> None:
        text = self._read(SPECIES_H)
        if re.search(r"^\s*SPECIES_CUSTOM_END\s*,", text, re.M):
            text = re.sub(r"^(\s*)(SPECIES_CUSTOM_END\s*,)", rf"\1{const},\n\1\2", text, count=1, flags=re.M)
        else:
            m = re.search(r"^#define\s+SPECIES_EGG\s+\((\w+)\s*\+\s*1\)", text, re.M)
            if not m:
                raise DecompError(f"can't find where custom species go in {SPECIES_H}")
            last = m.group(1)
            text = text[:m.start()] + f"#define {const} ({last} + 1)\n\n#define SPECIES_EGG ({const} + 1)" + text[m.end():]
        self._write(SPECIES_H, text)

    def _add_dex_constant(self, dex: str) -> bool:
        if not (self.root / POKEDEX_H).exists():
            return False
        text = self._read(POKEDEX_H)
        m = re.search(r"enum\s+NationalDexOrder\s*\{(.*?)\n\};", text, re.S)
        if not m:
            return False
        entries = re.findall(r"^\s*(NATIONAL_DEX_\w+)\s*,", m.group(1), re.M)
        if not entries:
            return False
        last = entries[-1]
        insert_at = m.end(1)
        text = text[:insert_at] + f"\n    {dex}," + text[insert_at:]
        # NATIONAL_DEX_COUNT: the compiled #define that names the old last entry now names the new one
        lines = text.split("\n")
        act = active_lines(strip_comments(text), self.gd.defines)
        for i, ln in enumerate(lines):
            if act[i] and re.match(rf"\s*#\s*define\s+NATIONAL_DEX_COUNT\s+{last}\b", ln):
                lines[i] = ln.replace(last, dex)
        self._write(POKEDEX_H, "\n".join(lines))
        return True

    def _include_custom_file(self) -> None:
        text = self._read(SPECIES_INFO)
        if "species_info/custom_families.h" in text:
            return
        incs = list(re.finditer(r'^([ \t]*)#include "species_info/gen_\d+_families\.h"[ \t]*$', text, re.M))
        if not incs:
            raise DecompError(f"can't find where the species files are included in {SPECIES_INFO}")
        m = incs[-1]
        text = text[:m.end()] + f'\n{m.group(1)}#include "species_info/custom_families.h"' + text[m.end():]
        self._write(SPECIES_INFO, text)

    def _copy_learnables(self, template: str, key: str) -> None:
        if not (self.root / LEARNABLES_FILE).exists():
            return
        old_key = self.gd._teachable_key(template)
        data = json.loads(self._read(LEARNABLES_FILE))
        if old_key and old_key in data and key not in data:
            data[key] = list(data[old_key])
            self.gd._write_json(LEARNABLES_FILE, data)
            self._written.append(LEARNABLES_FILE)

    def _add_to_dex_orders(self, n: dict) -> None:
        """Put the new number in the Pokédex's alphabetical, weight and height lists, in its place."""
        if not (self.root / ORDERS_H).exists():
            return
        info = {}
        for sid, ent in self.gd._species_index().items():
            values, _ = self.gd._entry_fields(ent)
            dex = values.get("natDexNum", "").strip()
            if dex and dex not in info:
                info[dex] = {"name": _game_text(values.get("speciesName", "")).lower(),
                             "weight": self.gd._value(values.get("weight", "")) or 0,
                             "height": self.gd._value(values.get("height", "")) or 0}
        me = info.get(n["dex"]) or {"name": n["name"].lower(), "weight": 0, "height": 0}
        text = self._read(ORDERS_H)
        for array, key in (("gPokedexOrder_Alphabetical", "name"), ("gPokedexOrder_Weight", "weight"),
                           ("gPokedexOrder_Height", "height")):
            m = re.search(rf"{array}\[\]\s*=\s*\{{(.*?)\n\}};", text, re.S)
            if not m:
                continue
            body = m.group(1)
            place = None
            for e in re.finditer(r"^([ \t]*)(NATIONAL_DEX_\w+)\s*,", body, re.M):
                other = info.get(e.group(2))
                if other and other[key] > me[key]:
                    place = e
                    break
            if place:
                new_body = body[:place.start()] + f"{place.group(1)}{n['dex']},\n" + body[place.start():]
            else:
                new_body = body.rstrip() + f"\n    {n['dex']},"
            text = text[:m.start(1)] + new_body + text[m.end(1):]
        self._write(ORDERS_H, text)


def _drop_fields(entry: str, fields: tuple[str, ...]) -> str:
    """Remove ``.field = …,`` (values may span lines) from an entry's text."""
    for f in fields:
        m = re.search(rf"^[ \t]*\.{f}\s*=\s*", entry, re.M)
        if not m:
            continue
        depth, i = 0, m.end()
        while i < len(entry):
            c = entry[i]
            depth += c in "({"
            depth -= c in ")}"
            if c == "," and depth == 0:
                break
            if depth < 0:
                i -= 1
                break
            i += 1
        end = entry.find("\n", i)
        entry = entry[:m.start()] + entry[(end + 1) if end >= 0 else len(entry):]
    return entry

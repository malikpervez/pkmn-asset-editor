"""Dialogue as it looks in the game: the real font, the message box, and mistakes that only show up in game.

Text in scripts is ``Label:`` followed by ``.string "…"`` lines. The game prints it in a box two lines high:
``\\n`` starts the second line, ``\\l`` scrolls up a line, ``\\p`` starts a new box and ``$`` ends the text.
Characters come from ``charmap.txt``, glyphs from ``graphics/fonts/latin_normal.png`` (16x16 cells, one per byte)
and their widths from ``gFontNormalLatinGlyphWidths`` in ``src/fonts.c``.

Checked: lines wider than the box (they get cut off), a third line in a box without ``\\l``/``\\p`` (it runs off the
box), characters the game can't print, unknown ``{PLACEHOLDERS}`` and a missing ``$``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

BOX_WIDTH = 216  # 27 tiles: the standard field message window
TEXT_COLORS = {1: (96, 96, 96), 2: (208, 208, 200)}  # DARK_GRAY text, LIGHT_GRAY shadow
# what placeholders print as in the preview (the longest likely text, so widths are checked fairly)
PLACEHOLDERS = {"PLAYER": "BRENDAN", "RIVAL": "BRENDAN", "STR_VAR_1": "VAR1", "STR_VAR_2": "VAR2",
                "STR_VAR_3": "VAR3", "KUN": "", "VERSION": "EMERALD", "AQUA": "AQUA", "MAGMA": "MAGMA",
                "ARCHIE": "ARCHIE", "MAXIE": "MAXIE", "KYOGRE": "KYOGRE", "GROUDON": "GROUDON", "REGION": "HOENN"}
STRING_RE = re.compile(r'^\s*\.string\s+"((?:[^"\\]|\\.)*)"', re.M)
LABEL_RE = re.compile(r"^(\w+):{1,2}[ \t]*$", re.M)


@dataclass
class Box:
    lines: list[str]  # what the box shows when the game waits for A (one or two lines)
    widths: list[int]


@dataclass
class TextPreview:
    label: str
    text: str
    boxes: list[Box] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    line: int = 0

    def as_dict(self) -> dict:
        return {"label": self.label, "text": self.text, "line": self.line, "problems": self.problems,
                "boxes": [{"lines": b.lines, "widths": b.widths} for b in self.boxes]}


class TextBox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._font: np.ndarray | None = None
        self._widths: list[int] | None = None
        self._charmap: dict[str, int] | None = None
        self._codes: set[str] | None = None

    # ------------------------------------------------------------------ game data
    def charmap(self) -> dict[str, int]:
        if self._charmap is None:
            cm, codes = {}, set()
            p = self.root / "charmap.txt"
            if p.exists():
                for line in p.read_text(errors="replace").splitlines():
                    m = re.match(r"^'(.+?)'\s*=\s*([0-9A-Fa-f]{2})\b", line)
                    if m:
                        ch = m.group(1).encode().decode("unicode_escape") if m.group(1).startswith("\\") else m.group(1)
                        cm.setdefault(ch, int(m.group(2), 16))
                        continue
                    m = re.match(r"^(\w+)\s*=", line)
                    if m:
                        codes.add(m.group(1))
            if not cm:  # no charmap.txt: plain ASCII letters in Gen 3 order
                for i, c in enumerate("0123456789"):
                    cm[c] = 0xA1 + i
                for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
                    cm[c] = 0xBB + i
                for i, c in enumerate("abcdefghijklmnopqrstuvwxyz"):
                    cm[c] = 0xD5 + i
                cm.update({" ": 0, "!": 0xAB, "?": 0xAC, ".": 0xAD, "-": 0xAE, ",": 0xB8, "'": 0xB4})
            self._charmap, self._codes = cm, codes
        return self._charmap

    def widths(self) -> list[int]:
        if self._widths is None:
            out = [6] * 256
            p = self.root / "src" / "fonts.c"
            if p.exists():
                m = re.search(r"gFontNormalLatinGlyphWidths\[\]\s*=\s*\{(.*?)\}", p.read_text(errors="replace"), re.S)
                if m:
                    vals = [int(v) for v in re.findall(r"\d+", m.group(1))]
                    out = (vals + out)[:256] if vals else out
            self._widths = out
        return self._widths

    def font(self) -> np.ndarray | None:
        if self._font is None:
            p = self.root / "graphics" / "fonts" / "latin_normal.png"
            if p.exists():
                with Image.open(p) as im:
                    self._font = np.array(im.convert("P") if im.mode != "P" else im)
        return self._font

    # ------------------------------------------------------------------ text -> boxes
    def _tokens(self, text: str) -> tuple[list[tuple[str, object]], list[str]]:
        """('char', code) / ('nl'|'scroll'|'para'|'end', None) tokens and problems."""
        cm, codes = self.charmap(), self._codes or set()
        out, problems = [], []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                e = text[i + 1]
                i += 2
                if e == "n":
                    out.append(("nl", None))
                elif e == "l":
                    out.append(("scroll", None))
                elif e == "p":
                    out.append(("para", None))
                elif e in cm:
                    out.append(("char", cm[e]))
                else:
                    problems.append(f"\\{e} is not something the game understands")
                continue
            if c == "{":
                j = text.find("}", i)
                if j < 0:
                    problems.append("a { has no matching }")
                    i += 1
                    continue
                name = text[i + 1:j].split()[0] if text[i + 1:j].strip() else ""
                i = j + 1
                if name in PLACEHOLDERS:
                    out += [("char", cm.get(ch, 0)) for ch in PLACEHOLDERS[name]]
                elif name in codes or not codes:
                    continue  # colours, pauses, sounds, fonts: nothing to print
                else:
                    problems.append(f"{{{name}}} is not a known code (see charmap.txt)")
                continue
            if c == "$":
                out.append(("end", None))
                i += 1
                continue
            if c in cm:
                out.append(("char", cm[c]))
            else:
                problems.append(f"'{c}' can't be shown by the game's font")
            i += 1
        return out, problems

    def layout(self, label: str, text: str, line: int = 0) -> TextPreview:
        tokens, problems = self._tokens(text)
        widths = self.widths()
        prev = TextPreview(label, text, line=line, problems=list(problems))
        lines, cur, box_lines = [""], [], []
        box: list[str] = []
        shown_lines: list[str] = []

        def width_of(codes):
            return sum(widths[c] for c in codes)

        def flush_line():
            nonlocal cur
            s = "".join(_decode(self.charmap(), c) for c in cur)
            w = width_of(cur)
            cur = []
            return s, w

        def check_width(s, w):
            if w > BOX_WIDTH:
                prev.problems.append(f"\"{s}\" is {w} pixels wide; the box fits {BOX_WIDTH}, so it gets cut off "
                                     "(add a \\n or \\l earlier)")

        boxes: list[Box] = []
        current: list[tuple[str, int]] = []  # lines in the box right now
        ended = False
        for kind, val in tokens:
            if ended:
                if kind == "char":
                    prev.problems.append("there is text after the $ (it is never shown)")
                    break
                continue
            if kind == "char":
                cur.append(val)
                continue
            s, w = flush_line()
            current.append((s, w))
            check_width(s, w)
            if kind == "nl":
                if len(current) >= 2:
                    prev.problems.append(f"a 3rd line starts after \"{current[-1][0]}\", but the box only has 2: use "
                                         "\\l (scroll) or \\p (new box) there instead of \\n")
                continue
            boxes.append(Box([x for x, _ in current], [y for _, y in current]))
            if kind == "scroll":
                current = current[-1:]
            elif kind == "para":
                current = []
            elif kind == "end":
                ended = True
                current = []
        if not ended:
            s, w = flush_line()
            check_width(s, w)
            if s or current:
                current.append((s, w))
                boxes.append(Box([x for x, _ in current], [y for _, y in current]))
            prev.problems.append("the text doesn't end with $ (the game would keep reading past it)")
        prev.problems = list(dict.fromkeys(prev.problems))
        prev.boxes = boxes
        del lines, box_lines, box, shown_lines
        return prev

    def wrap(self, text: str, width: int = BOX_WIDTH - 8) -> list[str]:
        """Plain text -> game text lines (each ending in \\n, \\l, \\p or $) that fit the box.
        A blank line (or \\p) in the input starts a new box."""
        widths, cm = self.widths(), self.charmap()

        def w(s):  # {PLAYER} and the like are measured as the name they print
            s = re.sub(r"\{(\w+)\}", lambda m: PLACEHOLDERS.get(m.group(1), m.group(0)), s)
            return sum(widths[cm.get(c, 0)] for c in s)
        out: list[str] = []
        paras = [p.split() for p in re.split(r"\n\s*\n|\\p", text.strip()) if p.split()]
        for pi, words in enumerate(paras):
            lines, cur = [], ""
            for word in words:
                cand = f"{cur} {word}" if cur else word
                if cur and w(cand) > width:
                    lines.append(cur)
                    cur = word
                else:
                    cur = cand
            if cur:
                lines.append(cur)
            for li, line in enumerate(lines):
                last = li == len(lines) - 1
                if last:
                    end = "$" if pi == len(paras) - 1 else "\\p"
                else:
                    end = "\\n" if li == 0 else "\\l"  # 2nd line of the box, then scroll for the rest
                out.append(line + end)
        return out or ["$"]

    # ------------------------------------------------------------------ scripts
    def texts(self, script: str) -> list[TextPreview]:
        """Every text label in a script file's source."""
        out = []
        labels = list(LABEL_RE.finditer(script))
        for i, m in enumerate(labels):
            end = labels[i + 1].start() if i + 1 < len(labels) else len(script)
            body = script[m.end():end]
            parts = STRING_RE.findall(body)
            if not parts:
                continue
            first = re.search(r"\S", body)
            if first and not body[first.start():].startswith(".string"):
                continue  # a script with code that happens to contain .string later
            out.append(self.layout(m.group(1), "".join(parts), script.count("\n", 0, m.start()) + 1))
        return out

    def check_changed(self, dp, files) -> list[tuple[str, str, str]]:
        """Problems in texts that are new or changed since the last commit: (level, file:line, message)."""
        out = []
        for rel in files:
            if not rel.endswith((".inc", ".pory")) or not (self.root / rel).exists():
                continue
            before = {t.label: t.text for t in self.texts((dp.original_bytes(rel) or b"").decode(errors="replace"))}
            for t in self.texts((self.root / rel).read_text(errors="replace")):
                if before.get(t.label) == t.text:
                    continue
                for msg in t.problems:
                    out.append(("warning", f"{rel}:{t.line}", f"{t.label}: {msg}"))
        return out

    # ------------------------------------------------------------------ rendering
    def render(self, prev: TextPreview, scale: int = 2) -> Image.Image:
        """The boxes one under the other, drawn with the game's font in a message window."""
        font = self.font()
        widths = self.widths()
        bw, bh, pad = BOX_WIDTH + 16, 40, 6
        boxes = prev.boxes or [Box([""], [0])]
        tops, y = [], pad
        for b in boxes:
            tops.append(y)
            y += bh + 16 * max(0, len(b.lines) - 2) + pad
        img = Image.new("RGB", (bw, y), (40, 40, 48))
        d = ImageDraw.Draw(img)
        arr = np.array(img)
        for bi, b in enumerate(boxes):
            y0 = tops[bi]
            d.rounded_rectangle([0, y0, bw - 1, y0 + bh - 1], 4, fill=(255, 255, 255), outline=(90, 160, 200), width=2)
            if len(b.lines) > 2:  # lines that run off the bottom of the box
                d.rectangle([0, y0 + bh, bw - 1, y0 + bh + 16 * (len(b.lines) - 2)], fill=(255, 225, 225))
            arr = np.array(img)
            for li, s in enumerate(b.lines):
                x, y = 8, y0 + 4 + li * 16
                for ch in s:
                    code = self.charmap().get(ch, 0)
                    w = widths[code]
                    if font is not None:
                        gy, gx = (code // 16) * 16, (code % 16) * 16
                        glyph = font[gy:gy + 16, gx:gx + w] if gy + 16 <= font.shape[0] else None
                        if glyph is not None:
                            region = arr[y:y + 16, x:x + glyph.shape[1]]
                            for v, col in TEXT_COLORS.items():
                                mask = glyph[: region.shape[0], : region.shape[1]] == v
                                region[mask] = col
                    x += w
                if b.widths[li] > BOX_WIDTH:
                    arr[y:y + 14, 8 + BOX_WIDTH:bw - 2] = (230, 60, 60)
            img = Image.fromarray(arr)
            d = ImageDraw.Draw(img)
        if scale > 1:
            img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
        return img


def _decode(cm: dict[str, int], code: int) -> str:
    for ch, c in cm.items():
        if c == code and len(ch) == 1:
            return ch
    return "?"

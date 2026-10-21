"""Build event scripts from blocks ("say this", "give that", "if this flag is on…") instead of writing them.

A script is a list of blocks; a block is a dict with a ``type``:

=============  ====================================================  ==========================================
type           fields                                                becomes
=============  ====================================================  ==========================================
say            text                                                  msgbox (text wrapped to fit the box)
ask            text, yes: [blocks], no: [blocks]                     msgbox …, MSGBOX_YESNO + branches
give_item      item, amount                                          giveitem
give_mon       species, level                                        givemon + a fanfare and message
trainer        trainer, intro, defeat                                trainerbattle_single (put it first)
wild           species, level                                        setwildbattle + dowildbattle
flag           flag, on                                              setflag / clearflag
var            var, value                                            setvar
if_flag        flag, on, then: [blocks], else: [blocks]              goto_if_set / goto_if_unset
if_var         var, value, then: [blocks], else: [blocks]            goto_if_ne
move           who (LOCALID_X or "player"), steps: [up, down, …]     applymovement + waitmovement + movement data
sound / music  se / bgm                                              playse / playbgm
wait           frames                                                delay
heal                                                                 special HealPlayerParty
hide / show    who                                                   removeobject / addobject
warp           map, x, y                                             warp + waitstate
fade           out                                                   fadescreen
custom         line                                                  the line as written
=============  ====================================================  ==========================================

Movement steps: up/down/left/right (walk), run_up…, face_up…, face_player, jump_up… (jump in place), !, ?, wait.

The blocks are saved as a ``@ pae-blocks`` comment line above the script, so the builder can open what it made;
scripts written by hand are read back as blocks where the commands are simple, and as ``custom`` lines elsewhere.
"""

from __future__ import annotations

import json
import re

from .decomp import DecompError

STEP = {**{d: f"walk_{d}" for d in ("up", "down", "left", "right")},
        **{f"run_{d}": f"walk_fast_{d}" for d in ("up", "down", "left", "right")},
        **{f"face_{d}": f"face_{d}" for d in ("up", "down", "left", "right")},
        **{f"jump_{d}": f"jump_in_place_{d}" for d in ("up", "down", "left", "right")},
        "face_player": "face_player", "!": "emote_exclamation_mark", "?": "emote_question_mark", "wait": "delay_16"}
STEP_BACK = {v: k for k, v in STEP.items()}
LOCK = {"npc": ("lock", "faceplayer"), "trigger": ("lockall",), "sign": ("lockall",)}
UNLOCK = {"npc": ("release",), "trigger": ("releaseall",), "sign": ("releaseall",)}
META = "@ pae-blocks "


class _Gen:
    def __init__(self, base: str, wrap, taken: set[str] | None = None):
        self.base = base
        self.wrap = wrap
        self.taken = taken or set()
        self.data: list[str] = []
        self.n = {"text": 0, "move": 0, "branch": 0}

    def _label(self, kind: str) -> str:
        """Town_EventScript_Greeter -> Town_Text_Greeter1, Town_Movement_Greeter1, Town_EventScript_Greeter_Branch1."""
        while True:  # never a name the game already uses
            self.n[kind] += 1
            if kind == "branch" or "_EventScript_" not in self.base:
                part = {"text": "Text", "move": "Movement", "branch": "Branch"}[kind]
                lbl = f"{self.base}_{part}{self.n[kind]}"
            else:
                part = {"text": "_Text_", "move": "_Movement_"}[kind]
                lbl = self.base.replace("_EventScript_", part, 1) + str(self.n[kind])
            if lbl not in self.taken:
                return lbl

    def text(self, text: str) -> str:
        lbl = self._label("text")
        text = (text or "").replace('"', "'")
        # written with its own line breaks (\n, \l): kept as it is; otherwise wrapped to fit (\p = a new box)
        lines = [text.replace("\n", "\\n").rstrip("$") + "$"] if re.search(r"\\[nl]", text) else self.wrap(text)
        self.data.append(f"{lbl}:\n" + "".join(f'\t.string "{x}"\n' for x in lines))
        return lbl

    def blocks(self, blocks: list[dict]) -> list[str]:
        out = []
        for b in blocks or []:
            out += self.block(b)
        return out

    def block(self, b: dict) -> list[str]:
        t = b.get("type")
        if t == "say":
            return [f"\tmsgbox {self.text(b.get('text', ''))}, MSGBOX_DEFAULT"]
        if t == "ask":
            no, done = self._label("branch"), None
            done = no + "_Done"
            return ([f"\tmsgbox {self.text(b.get('text', ''))}, MSGBOX_YESNO",
                     f"\tgoto_if_eq VAR_RESULT, NO, {no}"] + self.blocks(b.get("yes")) +
                    [f"\tgoto {done}", f"{no}:"] + self.blocks(b.get("no")) + [f"{done}:"])
        if t in ("if_flag", "if_var"):
            other = self._label("branch")
            done = other + "_Done"
            if t == "if_flag":
                test = f"\tgoto_if_{'unset' if b.get('on', True) else 'set'} {b['flag']}, {other}"
            else:
                test = f"\tgoto_if_ne {b['var']}, {int(b.get('value') or 0)}, {other}"
            return [test] + self.blocks(b.get("then")) + [f"\tgoto {done}", f"{other}:"] + \
                self.blocks(b.get("else")) + [f"{done}:"]
        if t == "give_item":
            return [f"\tgiveitem {b['item']}, {int(b.get('amount') or 1)}"]
        if t == "give_mon":
            msg = self.text(f"{{PLAYER}} received {b.get('name') or _pretty(b['species'])}!")
            return [f"\tgivemon {b['species']}, {int(b.get('level') or 5)}", "\tplayfanfare MUS_OBTAIN_ITEM",
                    f"\tmsgbox {msg}, MSGBOX_DEFAULT", "\twaitfanfare"]
        if t == "trainer":
            return [f"\ttrainerbattle_single {b['trainer']}, {self.text(b.get('intro', 'Let us battle!'))}, "
                    f"{self.text(b.get('defeat', 'You win!'))}"]
        if t == "wild":
            return [f"\tsetwildbattle {b['species']}, {int(b.get('level') or 5)}", "\tdowildbattle"]
        if t == "flag":
            return [f"\t{'setflag' if b.get('on', True) else 'clearflag'} {b['flag']}"]
        if t == "var":
            return [f"\tsetvar {b['var']}, {int(b.get('value') or 0)}"]
        if t == "move":
            who = _who(b.get("who"))
            lbl = self._label("move")
            steps = [STEP.get(s, s) for s in b.get("steps") or [] if s]
            bad = [s for s in steps if not re.fullmatch(r"[a-z_0-9]+", s)]
            if bad:
                raise DecompError(f"unknown movement step {bad[0]}")
            self.data.append(f"{lbl}:\n" + "".join(f"\t{s}\n" for s in steps) + "\tstep_end\n")
            return [f"\tapplymovement {who}, {lbl}", f"\twaitmovement 0"]
        if t == "sound":
            return [f"\tplayse {b['se']}", "\twaitse"]
        if t == "music":
            return [f"\tplaybgm {b['bgm']}, FALSE"]
        if t == "wait":
            return [f"\tdelay {int(b.get('frames') or 30)}"]
        if t == "heal":
            return ["\tspecial HealPlayerParty"]
        if t == "hide":
            return [f"\tremoveobject {_who(b.get('who'))}"]
        if t == "show":
            return [f"\taddobject {_who(b.get('who'))}"]
        if t == "warp":
            return [f"\twarp {b['map']}, {int(b.get('x') or 0)}, {int(b.get('y') or 0)}", "\twaitstate"]
        if t == "fade":
            return [f"\tfadescreen {'FADE_TO_BLACK' if b.get('out', True) else 'FADE_FROM_BLACK'}"]
        if t == "custom":
            line = (b.get("line") or "").strip()
            return [f"\t{line}"] if line else []
        raise DecompError(f"unknown block {t}")


def _who(who) -> str:
    return "LOCALID_PLAYER" if who in (None, "", "player", "LOCALID_PLAYER") else str(who)


def _pretty(const: str) -> str:
    return re.sub(r"^[A-Z]+_", "", const or "").replace("_", " ").title()


def generate(label: str, kind: str, blocks: list[dict], wrap, taken: set[str] | None = None) -> str:
    """The script text for ``blocks``: the meta comment, the script and its texts/movements (named so that they
    don't clash with ``taken`` labels)."""
    g = _Gen(label, wrap, taken)
    body = g.blocks(blocks)
    trainer_first = bool(blocks) and blocks[0].get("type") == "trainer"
    if kind == "sign" and len(blocks) == 1 and blocks[0].get("type") == "say":
        code = [body[0].replace("MSGBOX_DEFAULT", "MSGBOX_SIGN"), "\tend"]
    else:
        lock = () if trainer_first else LOCK.get(kind, LOCK["npc"])
        code = [f"\t{x}" for x in lock] + body + [f"\t{x}" for x in UNLOCK.get(kind, UNLOCK["npc"])] + ["\tend"]
    meta = META + json.dumps({"label": label, "kind": kind, "blocks": blocks}, separators=(",", ":"))
    return meta + f"\n{label}::\n" + "\n".join(code) + "\n\n" + "\n".join(g.data) + f"@ pae-blocks-end {label}\n"


def find(script: str, label: str):
    """(start, end, blocks, kind) of a builder-made script in a map's scripts, or None."""
    m = re.search(rf"^{re.escape(META)}(.*)\n{re.escape(label)}::", script, re.M)
    if not m:
        return None
    try:
        meta = json.loads(m.group(1))
    except ValueError:
        return None
    end = re.search(rf"^@ pae-blocks-end {re.escape(label)}[ \t]*\n?", script[m.start():], re.M)
    stop = m.start() + end.end() if end else len(script)
    return m.start(), stop, meta.get("blocks", []), meta.get("kind", "npc")


def parse(script: str, label: str) -> dict:
    """Blocks for an existing script: the builder's own, or read back from a hand-written one."""
    hit = find(script, label)
    if hit:
        return {"blocks": hit[2], "kind": hit[3], "built": True}
    m = re.search(rf"^{re.escape(label)}::?[ \t]*\n", script, re.M)
    if not m:
        raise DecompError(f"no script {label}")
    nxt = re.search(r"^\w+::?[ \t]*$", script[m.end():], re.M)
    code = script[m.end(): m.end() + nxt.start()] if nxt else script[m.end():]
    texts = _texts(script)
    moves = _movements(script)
    blocks, kind = [], "npc"
    for raw in code.splitlines():
        line = raw.split("@")[0].strip()
        if not line:
            continue
        w = re.split(r"[\s,]+", line)
        op, args = w[0], [a for a in w[1:] if a]
        if op in ("lock", "faceplayer", "release", "end", "waitmovement", "waitstate", "waitse", "waitfanfare",
                  "dowildbattle", "return"):
            continue
        if op == "lockall":
            kind = "trigger"
            continue
        if op == "releaseall":
            continue
        if op == "msgbox" and args:
            if len(args) > 1 and args[1] == "MSGBOX_SIGN":
                kind = "sign"
            blocks.append({"type": "say", "text": texts.get(args[0], args[0])})
        elif op == "giveitem" and args:
            blocks.append({"type": "give_item", "item": args[0], "amount": int(args[1]) if len(args) > 1 and args[1].isdigit() else 1})
        elif op == "givemon" and len(args) >= 2:
            blocks.append({"type": "give_mon", "species": args[0], "level": int(args[1]) if args[1].isdigit() else 5})
        elif op == "trainerbattle_single" and len(args) >= 3:
            blocks.append({"type": "trainer", "trainer": args[0], "intro": texts.get(args[1], args[1]),
                           "defeat": texts.get(args[2], args[2])})
        elif op == "setwildbattle" and len(args) >= 2:
            blocks.append({"type": "wild", "species": args[0], "level": int(args[1]) if args[1].isdigit() else 5})
        elif op in ("setflag", "clearflag") and args:
            blocks.append({"type": "flag", "flag": args[0], "on": op == "setflag"})
        elif op == "setvar" and len(args) >= 2 and args[1].lstrip("-").isdigit():
            blocks.append({"type": "var", "var": args[0], "value": int(args[1])})
        elif op == "applymovement" and len(args) >= 2 and args[1] in moves:
            who = "player" if args[0] in ("LOCALID_PLAYER", "OBJ_EVENT_ID_PLAYER") else args[0]
            blocks.append({"type": "move", "who": who, "steps": moves[args[1]]})
        elif op == "playse" and args:
            blocks.append({"type": "sound", "se": args[0]})
        elif op == "playbgm" and args:
            blocks.append({"type": "music", "bgm": args[0]})
        elif op == "delay" and args and args[0].isdigit():
            blocks.append({"type": "wait", "frames": int(args[0])})
        elif op == "special" and args == ["HealPlayerParty"]:
            blocks.append({"type": "heal"})
        elif op == "removeobject" and args:
            blocks.append({"type": "hide", "who": args[0]})
        elif op == "addobject" and args:
            blocks.append({"type": "show", "who": args[0]})
        elif op == "warp" and len(args) >= 3 and args[-1].isdigit():
            blocks.append({"type": "warp", "map": args[0], "x": int(args[-2]), "y": int(args[-1])})
        elif op == "fadescreen" and args:
            blocks.append({"type": "fade", "out": args[0] in ("FADE_TO_BLACK", "FADE_TO_WHITE")})
        else:
            blocks.append({"type": "custom", "line": line})
    return {"blocks": blocks, "kind": kind, "built": False}


def _texts(script: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r'^(\w+):{1,2}[ \t]*\n((?:[ \t]*\.string[ \t]+"(?:[^"\\]|\\.)*"[ \t]*\n?)+)', script, re.M):
        raw = "".join(re.findall(r'\.string\s+"((?:[^"\\]|\\.)*)"', m.group(2)))
        out[m.group(1)] = raw.replace("\\p", "\n\n").replace("\\n", "\n").replace("\\l", "\n").rstrip("$")
    return out


def _movements(script: str) -> dict[str, list[str]]:
    out = {}
    for m in re.finditer(r"^(\w+):{1,2}[ \t]*\n((?:[ \t]*[a-z_0-9]+[ \t]*\n)+?)[ \t]*step_end", script, re.M):
        steps = [s.strip() for s in m.group(2).splitlines() if s.strip()]
        if all(re.fullmatch(r"[a-z_0-9]+", s) for s in steps):
            out[m.group(1)] = [STEP_BACK.get(s, s) for s in steps]
    return out


def save(maps, map_name: str, label: str, kind: str, blocks: list[dict]) -> dict:
    """Write a builder script into a map's scripts (replacing the one with the same label) and return it."""
    from .textbox import TextBox
    name = maps.resolve(map_name)
    if not re.fullmatch(r"[A-Za-z_]\w*", label or ""):
        raise DecompError("the script needs a name made of letters, digits and _")
    if maps.script_path(name).suffix == ".pory":
        raise DecompError("this map uses Poryscript; the block builder writes scripts.inc")
    tb = TextBox(maps.root)
    script = maps.read_script(name)
    hit = find(script, label)
    outside = script[:hit[0]] + script[hit[1]:] if hit else script
    own_file = str(maps.script_path(name).relative_to(maps.root))
    taken = set(re.findall(r"^(\w+):", outside, re.M)) | {k for k, f in maps.labels().items() if f != own_file}
    taken.discard(label)
    text = generate(label, kind, blocks, tb.wrap, taken)
    if hit:
        script = script[:hit[0]] + text + script[hit[1]:]
    else:
        m = re.search(rf"^{re.escape(label)}::?[ \t]*\n", script, re.M)
        if m:  # a hand-written script: its code is replaced, its texts stay where they are
            nxt = re.search(r"^\w+::?[ \t]*$", script[m.end():], re.M)
            end = m.end() + nxt.start() if nxt else len(script)
            script = script[:m.start()] + text + "\n" + script[end:]
        else:
            others = {k for k, f in maps.labels().items()}
            if label in others:
                raise DecompError(f"{label} is already used in another file")
            script = script.rstrip("\n") + "\n\n" + text
    maps.write_script(name, script)
    own = set(re.findall(r"^(\w+):", text, re.M))
    texts = [t.as_dict() for t in tb.texts(maps.read_script(name)) if t.label in own]
    return {"label": label, "script": text, "texts": texts,
            "problems": [f"{t['label']}: {p}" for t in texts for p in t["problems"]]}

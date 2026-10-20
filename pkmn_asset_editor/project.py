"""Projects: extract a ROM into an organised folder, track edits, rebuild the ROM."""

from __future__ import annotations

import bisect
import hashlib
import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import formats, lz77, patch, previews
from .catalog import Asset, Blob, build_catalog
from .discovery import discover
from .gfx import (RGB, LoadedImage, decode_palette, encode_palette, load_png, quantize_new_palette, read_jasc,
                  same_color, save_indexed_png, write_jasc)
from .rom import MAX_ROM_SIZE, ROM_BASE, Rom, sha1

PROJECT_FILE = "pae-project.json"
MANIFEST_FILE = "manifest.json"
FORMAT = 1


class ProjectError(RuntimeError):
    pass


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class BuildReport:
    output: str = ""
    changed_assets: list[str] = field(default_factory=list)
    in_place: list[dict] = field(default_factory=list)
    relocated: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    patches: list[str] = field(default_factory=list)
    rom_size: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {"ok": self.ok, **self.__dict__}


class FreeSpace:
    """First-fit allocator over runs of 0xFF that nothing in the ROM points into."""

    def __init__(self, rom: Rom, start: int = 0, min_run: int = 0x400, allow_expand: bool = True):
        self.rom = rom
        self.allow_expand = allow_expand
        data = np.frombuffer(bytes(rom.data), dtype=np.uint8)
        ff = np.concatenate([[False], data == 0xFF, [False]])
        edges = np.flatnonzero(np.diff(ff.astype(np.int8)))
        runs = []
        targets = rom.pointers.unique_targets
        for s, e in zip(edges[0::2], edges[1::2]):
            s, e = int(s), int(e)
            if e <= start:
                continue
            s = max(s, start)
            # anything pointed at inside the run might be real (blank) data: carve a gap around it
            lo, hi = np.searchsorted(targets, s - 16), np.searchsorted(targets, e)
            cuts = [int(t) for t in targets[lo:hi]]
            pieces, cur = [], s
            for t in cuts:
                pieces.append((cur, t - 16))
                cur = max(cur, t + 0x200)
            pieces.append((cur, e))
            for ps, pe in pieces:
                ps = (ps + 16 + 3) & ~3  # keep a little distance from whatever precedes the run
                if pe - ps >= min_run:
                    runs.append([ps, pe])
        # biggest regions first: the end-of-ROM padding is the safest place to put new data
        self.runs = sorted(runs, key=lambda r: r[0] - r[1])
        self.expanded_to: int | None = None

    def alloc(self, size: int) -> int:
        size = (size + 3) & ~3
        for r in self.runs:
            if r[1] - r[0] >= size:
                off = r[0]
                r[0] += size
                return off
        if self.allow_expand and len(self.rom.data) < MAX_ROM_SIZE:
            old = len(self.rom.data)
            self.expanded_to = MAX_ROM_SIZE
            self.runs.append([old, MAX_ROM_SIZE])
            return self.alloc(size)
        raise ProjectError(f"no free space left for 0x{size:X} bytes")


class Project:
    def __init__(self, root: Path):
        self.root = Path(root)
        cfg_path = self.root / PROJECT_FILE
        if not cfg_path.exists():
            raise ProjectError(f"{self.root} is not a project (missing {PROJECT_FILE}); run `pae extract` first")
        self.config = json.loads(cfg_path.read_text())
        manifest = json.loads((self.root / MANIFEST_FILE).read_text())
        if manifest.get("format") != FORMAT:
            raise ProjectError("unsupported manifest format")
        self.manifest = manifest
        self.assets: dict[str, Asset] = {a["id"]: Asset.from_dict(a) for a in manifest["assets"]}
        self.blobs: dict[int, Blob] = {int(k, 16): Blob(**v) for k, v in manifest["blobs"].items()}
        self.alias_of: dict[str, str] = {al: a.id for a in self.assets.values() for al in a.aliases}
        self._rom: Rom | None = None
        self._raw: dict[int, bytes] = {}

    # ------------------------------------------------------------------ creation
    @classmethod
    def create(cls, rom_path, root, force: bool = False, log=print) -> "Project":
        root = Path(root)
        if (root / PROJECT_FILE).exists() and not force:
            raise ProjectError(f"{root} already contains a project (use --force to overwrite)")
        if root.exists() and force:
            for sub in ("graphics", "previews"):
                shutil.rmtree(root / sub, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        rom = Rom.load(rom_path)
        log(f"ROM: {rom.info.name} [{rom.info.code}] '{rom.info.title}' rev {rom.info.version}, "
            f"{len(rom.data) // 1024} KiB")
        disc = discover(rom)
        assets, blobs, raw, warnings = build_catalog(rom, disc)
        shutil.copyfile(rom_path, root / "original.gba")
        config = {
            "format": FORMAT,
            "game": {"code": rom.info.code, "title": rom.info.title, "name": rom.info.name,
                     "version": rom.info.version, "family": rom.info.family},
            "rom": {"file": "original.gba", "sha1": sha1(rom.data), "size": len(rom.data)},
            "build": {"output": "build/rom.gba", "free_space_start": None, "allow_expand": True,
                      "patches": ["bps", "ups", "ips"]},
            "notes": disc.notes + warnings,
        }
        (root / PROJECT_FILE).write_text(json.dumps(config, indent=2))
        cls._write_manifest(root, assets, blobs)
        proj = cls(root)
        proj._rom = rom
        proj._raw = dict(raw)
        palettes = proj.original_palettes()
        for a in proj.assets.values():
            proj.write_asset_file(a, palettes)
        proj.save_manifest()
        previews.write_previews(proj)
        (root / "AGENTS.md").write_text(previews.agents_guide(proj))
        (root / ".gitignore").write_text("original.gba\nbuild/\n")
        counts = proj.summary()
        log(f"Extracted {sum(counts.values())} assets in {time.time() - t0:.1f}s: " +
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return proj

    @staticmethod
    def _write_manifest(root: Path, assets: dict[str, Asset], blobs: dict[int, Blob]) -> None:
        data = {
            "format": FORMAT,
            "assets": [a.to_dict() for a in sorted(assets.values(), key=lambda a: a.id)],
            "blobs": {f"{k:06X}": b.to_dict() for k, b in sorted(blobs.items())},
        }
        (root / MANIFEST_FILE).write_text(json.dumps(data, indent=1))

    def save_manifest(self) -> None:
        self._write_manifest(self.root, self.assets, self.blobs)

    # ------------------------------------------------------------------ basics
    @property
    def rom(self) -> Rom:
        if self._rom is None:
            path = self.root / self.config["rom"]["file"]
            rom = Rom.load(path)
            if sha1(rom.data) != self.config["rom"]["sha1"]:
                raise ProjectError(f"{path} does not match the ROM this project was extracted from")
            self._rom = rom
        return self._rom

    def raw(self, off: int) -> bytes:
        r = self._raw.get(off)
        if r is None:
            b = self.blobs[off]
            if b.compression == "lz77":
                r = lz77.decompress(self.rom.data, off)[0]
            else:
                r = bytes(self.rom.data[off:off + b.raw_size])
            self._raw[off] = r
        return r

    class _RawView(dict):
        def __init__(self, proj):
            super().__init__()
            self.proj = proj

        def __missing__(self, key):
            v = self.proj.raw(key)
            self[key] = v
            return v

    def raw_view(self) -> dict[int, bytes]:
        return Project._RawView(self)

    def path(self, asset: Asset) -> Path:
        return self.root / asset.file

    def get(self, ident: str) -> Asset:
        ident = ident.strip().removeprefix("graphics/")
        for suffix in (".png", ".pal"):
            ident = ident.removesuffix(suffix)
        ident = self.alias_of.get(ident, ident)
        if ident not in self.assets:
            raise ProjectError(f"unknown asset '{ident}'")
        return self.assets[ident]

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.assets.values():
            out[a.category] = out.get(a.category, 0) + 1
        return out

    def original_palettes(self) -> dict[str, list[RGB]]:
        return {a.id: decode_palette(self.raw(a.blobs["data"])) for a in self.assets.values() if a.kind == "palette"}

    def current_palettes(self) -> dict[str, list[RGB]]:
        """Palettes as currently saved in the project's .pal files (falls back to the ROM)."""
        pals = self.original_palettes()
        for a in self.assets.values():
            if a.kind == "palette" and self.is_touched(a):
                try:
                    cols = read_jasc(self.path(a))
                    if len(cols) == len(pals[a.id]):
                        pals[a.id] = cols
                except (OSError, ValueError):
                    pass
        return pals

    # ------------------------------------------------------------------ interface shared with DecompProject
    kind = "rom"

    @property
    def title(self) -> str:
        return self.config["game"]["name"]

    def palette_colors(self, pid: str) -> list[RGB]:
        a = self.get(pid)
        if self.is_touched(a):
            try:
                cols = read_jasc(self.path(a))
                if len(cols) == a.layout["colors"]:
                    return cols
            except (OSError, ValueError):
                pass
        return decode_palette(self.raw(a.blobs["data"]))

    def set_palette(self, pid: str, colors: list[RGB]) -> list[str]:
        a = self.get(pid)
        if a.kind != "palette":
            raise ProjectError(f"{a.id} is not a palette")
        if len(colors) != a.layout["colors"]:
            raise ProjectError(f"{a.id} needs exactly {a.layout['colors']} colours")
        write_jasc(self.path(a), colors)
        return self.sync_palette_views(a.id)

    def view(self, asset: Asset, palette_override: str | None = None):
        """(indices, PLTE, index-0-is-transparent) of the asset's editable PNG."""
        p = self.path(asset)
        with Image.open(p) as im:
            mode = im.mode
        if mode != "P":
            self.import_image(asset, p)
        img = load_png(p)
        return img.indices, list(img.palette or []), asset.kind == "sprite" and bool(asset.palettes)

    def write_view(self, asset: Asset, indices: np.ndarray) -> list[str]:
        img = load_png(self.path(asset))
        save_indexed_png(self.path(asset), indices, [tuple(c) for c in img.palette],
                         transparent0=asset.kind == "sprite" and bool(asset.palettes))
        return []

    def output_rom(self) -> Path:
        return self.root / (self.config["build"].get("output") or "build/rom.gba")

    def describe(self, asset: Asset) -> dict:
        from .cli import describe
        return describe(self, asset)

    def users_of_palette(self, pid: str) -> list[str]:
        return [a.id for a in self.assets.values() if any(l["palette"] == pid for l in a.palettes)]

    def is_touched(self, asset: Asset) -> bool:
        p = self.path(asset)
        return p.exists() and file_sha1(p) != asset.sha1

    # ------------------------------------------------------------------ rendering
    def write_asset_file(self, asset: Asset, palettes: dict[str, list[RGB]], raw: dict[int, bytes] | None = None) -> None:
        path = self.path(asset)
        path.parent.mkdir(parents=True, exist_ok=True)
        if asset.kind == "palette":
            write_jasc(path, palettes[asset.id])
        else:
            pixels, plte = formats.render(asset, raw if raw is not None else self.raw_view(), palettes)
            save_indexed_png(path, pixels, plte, transparent0=asset.kind == "sprite" and bool(asset.palettes))
        asset.sha1 = file_sha1(path)

    def render_image(self, asset: Asset, palette_override: str | None = None,
                     palettes: dict[str, list[RGB]] | None = None) -> Image.Image:
        """Render the asset the way the built ROM will show it (current pixels + current palettes)."""
        palettes = dict(palettes or self.current_palettes())
        if asset.kind == "palette":
            cols = palettes[asset.id]
            n = len(cols)
            img = np.array(cols, dtype=np.uint8).reshape(-1, min(16, n), 3)
            return Image.fromarray(img, "RGB").resize((min(16, n) * 16, -(-n // 16) * 16), Image.NEAREST)
        work = asset
        if palette_override is not None and asset.kind == "sprite":
            work = Asset(**{**asset.__dict__, "palettes": [{"palette": palette_override, "png_bank": 0, "pal_bank": 0}]})
        raw = self.raw_view()
        p = self.path(asset)
        if p.exists() and self.is_touched(asset):
            try:
                img = load_png(p)
                for pid, _bank, changes in formats.plte_proposals(asset, img, self.original_palettes()):
                    if not self.is_touched(self.assets[pid]):
                        cols = list(palettes[pid])
                        for ci, rgb in changes.items():
                            cols[ci] = rgb
                        palettes[pid] = cols
                parsed = formats.parse(asset, img, raw, palettes)
                raw = _Overlay(self.raw_view(), parsed.blobs)
            except (formats.AssetError, OSError, ValueError):
                pass
        pixels, plte = formats.render(work, raw, palettes)
        img = Image.frombytes("P", (pixels.shape[1], pixels.shape[0]), pixels.tobytes())
        img.putpalette([v for c in plte for v in c])
        if work.kind == "sprite" and work.palettes:
            img.info["transparency"] = 0
        return img.convert("RGBA")

    def sync_palette_views(self, pid: str) -> list[str]:
        """Re-colour every PNG that uses palette ``pid`` with its current colours (pixels untouched)."""
        palettes = self.current_palettes()
        done = []
        for aid in self.users_of_palette(pid):
            a = self.assets[aid]
            p = self.path(a)
            if not p.exists():
                continue
            if not self.is_touched(a):
                self.write_asset_file(a, palettes)
                done.append(aid)
                continue
            with Image.open(p) as im:
                im.load()
                if im.mode != "P":
                    continue
                plte = formats.image_palette(a, palettes)
                old = im.getpalette() or []
                new = [v for c in plte for v in c]
                im.putpalette(new + old[len(new):])
                kwargs = {"transparency": im.info["transparency"]} if "transparency" in im.info else {}
                im.save(p, **kwargs)
            done.append(aid)
        self.save_manifest()
        return done

    # ------------------------------------------------------------------ status
    def status(self) -> list[dict]:
        out = []
        raw = self.raw_view()
        palettes = self.current_palettes()
        for a in sorted(self.assets.values(), key=lambda a: a.id):
            p = self.path(a)
            if not p.exists():
                out.append({"id": a.id, "state": "missing", "file": a.file})
                continue
            if file_sha1(p) == a.sha1:
                continue
            state = "modified"
            try:
                if a.kind == "palette":
                    cols = read_jasc(p)
                    orig = decode_palette(raw[a.blobs["data"]])
                    if len(cols) == len(orig) and all(same_color(x, y) for x, y in zip(cols, orig)):
                        state = "touched"
                else:
                    img = load_png(p)
                    parsed = formats.parse(a, img, raw, palettes)
                    if all(raw[o] == d for o, d in parsed.blobs.items()) and not formats.plte_proposals(
                            a, img, self.original_palettes()):
                        state = "touched"
            except (formats.AssetError, ValueError, OSError) as e:
                state = "invalid"
                out.append({"id": a.id, "state": state, "file": a.file, "error": str(e)})
                continue
            out.append({"id": a.id, "state": state, "file": a.file})
        return out

    # ------------------------------------------------------------------ build
    def collect_changes(self, report: BuildReport) -> dict[int, bytes]:
        raw = self.raw_view()
        orig_pals = self.original_palettes()
        proposals: dict[int, list[tuple[str, bytes]]] = {}

        # Phase A: palettes (.pal files win over PNG palettes)
        from_file: dict[str, list[RGB]] = {}
        for a in self.assets.values():
            if a.kind != "palette" or not self.is_touched(a):
                continue
            try:
                cols = read_jasc(self.path(a))
            except (ValueError, OSError) as e:
                report.errors.append(f"{a.id}: {e}")
                continue
            orig = orig_pals[a.id]
            if len(cols) != len(orig):
                report.errors.append(f"{a.id}: palette must have exactly {len(orig)} colours (has {len(cols)})")
                continue
            if not all(same_color(x, y) for x, y in zip(cols, orig)):
                from_file[a.id] = cols
        images: dict[str, LoadedImage] = {}
        png_changes: dict[str, dict[int, tuple[RGB, str]]] = {}
        for a in self.assets.values():
            if a.kind == "palette" or not self.is_touched(a):
                continue
            try:
                img = load_png(self.path(a))
            except (OSError, ValueError) as e:
                report.errors.append(f"{a.id}: cannot read image: {e}")
                continue
            images[a.id] = img
            for pid, _bank, changes in formats.plte_proposals(a, img, orig_pals):
                if pid in from_file:
                    if any(not same_color(rgb, from_file[pid][ci]) for ci, rgb in changes.items()):
                        report.warnings.append(f"{a.id}: its PNG palette differs from {pid}.pal; the .pal file wins")
                    continue
                slot = png_changes.setdefault(pid, {})
                for ci, rgb in changes.items():
                    prev = slot.get(ci)
                    if prev and not same_color(prev[0], rgb):
                        report.errors.append(f"palette {pid} colour {ci}: {prev[1]} sets {prev[0]} but {a.id} sets "
                                             f"{rgb}; edit {pid}.pal to settle it")
                    else:
                        slot[ci] = (rgb, a.id)
        effective = dict(orig_pals)
        effective.update(from_file)
        for pid, slot in png_changes.items():
            cols = list(orig_pals[pid])
            for ci, (rgb, _src) in slot.items():
                cols[ci] = rgb
            effective[pid] = cols
            report.warnings.append(f"palette {pid} updated from the colours in "
                                   f"{', '.join(sorted({s for _, s in slot.values()}))}")
        for pid in set(from_file) | set(png_changes):
            a = self.assets[pid]
            proposals.setdefault(a.blobs["data"], []).append((pid, encode_palette(effective[pid])))
            report.changed_assets.append(pid)

        # Phase B: pixels
        for aid, img in sorted(images.items()):
            a = self.assets[aid]
            try:
                parsed = formats.parse(a, img, raw, effective)
            except formats.AssetError as e:
                report.errors.append(str(e))
                continue
            report.warnings.extend(parsed.warnings)
            changed = False
            for off, data in parsed.blobs.items():
                if data != raw[off]:
                    proposals.setdefault(off, []).append((aid, data))
                    changed = True
            if changed:
                report.changed_assets.append(aid)

        # Merge proposals that target the same blob
        final: dict[int, bytes] = {}
        for off, props in proposals.items():
            orig = raw[off]
            if len(props) == 1:
                final[off] = props[0][1]
                continue
            if any(len(d) != len(orig) for _, d in props):
                if len({d for _, d in props}) == 1:
                    final[off] = props[0][1]
                else:
                    report.errors.append(f"blob 0x{off:06X} was resized by {props[0][0]} and also edited by "
                                         f"{', '.join(p for p, _ in props[1:])}")
                continue
            unit = self.blobs[off].unit
            merged = bytearray(orig)
            owner: dict[int, str] = {}
            for aid, data in props:
                for u in range(0, len(orig), unit):
                    chunk = data[u:u + unit]
                    if chunk == orig[u:u + unit]:
                        continue
                    if u in owner and merged[u:u + unit] != chunk:
                        report.errors.append(f"{aid} and {owner[u]} edit the same data at 0x{off:06X}+0x{u:X} "
                                             f"differently")
                        continue
                    merged[u:u + unit] = chunk
                    owner[u] = aid
            final[off] = bytes(merged)
        return final

    def build(self, output: str | None = None, patches: list[str] | None = None, dry_run: bool = False) -> BuildReport:
        report = BuildReport()
        final = self.collect_changes(report)
        if report.errors:
            return report
        rom = self.rom
        out = bytearray(rom.data)
        cfg = self.config["build"]
        space = FreeSpace(rom, cfg.get("free_space_start") or 0, allow_expand=cfg.get("allow_expand", True))
        pointers = rom.pointers
        blob_ranges = sorted((b.offset, b.offset + b.stored_size) for b in self.blobs.values())
        starts = [s for s, _ in blob_ranges]

        def inside_blob(loc):
            i = bisect.bisect_right(starts, loc) - 1
            return i >= 0 and blob_ranges[i][0] <= loc < blob_ranges[i][1]

        writes: list[tuple[int, bytes, bytes, str]] = []
        repoints: list[tuple[int, int]] = []
        for off in sorted(final):
            data = final[off]
            blob = self.blobs[off]
            if blob.compression == "lz77":
                enc = lz77.compress(data)
            else:
                if len(data) != blob.raw_size:
                    report.errors.append(f"uncompressed data at 0x{off:06X} cannot change size")
                    continue
                enc = data
            if len(enc) <= blob.stored_size:
                writes.append((off, enc, data, blob.compression))
                report.in_place.append({"offset": f"0x{off:06X}", "size": len(enc), "slot": blob.stored_size})
                continue
            refs = [r for r in pointers.refs_to(off) if not inside_blob(r)]
            if not refs:
                report.errors.append(f"0x{off:06X} grew to {len(enc)} bytes (slot {blob.stored_size}) and no "
                                     f"pointer to it was found, so it cannot be moved")
                continue
            new = space.alloc(len(enc))
            writes.append((new, enc, data, blob.compression))
            repoints.extend((r, new) for r in refs)
            report.relocated.append({"from": f"0x{off:06X}", "to": f"0x{new:06X}", "size": len(enc),
                                     "slot": blob.stored_size, "pointers_updated": len(refs)})
        if report.errors:
            return report
        if space.expanded_to:
            out.extend(b"\xFF" * (space.expanded_to - len(out)))
            report.warnings.append(f"ROM expanded to {space.expanded_to // (1 << 20)} MiB to make room")
        for off, enc, _data, _comp in writes:
            out[off:off + len(enc)] = enc
        for loc, new in repoints:
            out[loc:loc + 4] = (ROM_BASE + new).to_bytes(4, "little")
        # verify every written blob decodes to what we intended
        for off, _enc, data, comp in writes:
            if comp == "none":
                ok = bytes(out[off:off + len(data)]) == data
            else:
                ok = lz77.decompress(out, off)[0] == data
            if not ok:
                report.errors.append(f"internal error: verification failed at 0x{off:06X}")
        if report.errors:
            return report
        report.rom_size = len(out)
        report.changed_assets = sorted(set(report.changed_assets))
        if dry_run:
            return report
        target = self.root / (output or cfg.get("output") or "build/rom.gba")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(out)
        report.output = str(target)
        for kind in patches if patches is not None else cfg.get("patches", []):
            try:
                if kind == "bps":
                    data = patch.make_bps(bytes(rom.data), bytes(out))
                elif kind == "ups":
                    data = patch.make_ups(bytes(rom.data), bytes(out))
                elif kind == "ips":
                    data = patch.make_ips(bytes(rom.data), bytes(out))
                else:
                    report.warnings.append(f"unknown patch format {kind}")
                    continue
            except ValueError as e:
                report.warnings.append(str(e))
                continue
            p = target.with_suffix("." + kind)
            p.write_bytes(data)
            report.patches.append(str(p))
        (target.parent / "build-report.json").write_text(json.dumps(report.to_dict(), indent=2))
        return report

    # ------------------------------------------------------------------ editing helpers
    def revert(self, asset: Asset) -> None:
        # pixels/colours come from the ROM; images are coloured with the palettes as currently edited
        palettes = self.original_palettes() if asset.kind == "palette" else self.current_palettes()
        self.write_asset_file(asset, palettes)
        self.save_manifest()

    def import_image(self, asset: Asset, src, new_palette: bool = False, resize: bool = False) -> list[str]:
        """Convert any image (RGBA, wrong size, too many colours) into the asset's format and save it."""
        if asset.kind == "palette":
            cols = _read_palette_source(src)
            n = asset.layout["colors"]
            if len(cols) < n:
                cols = cols + self.current_palettes()[asset.id][len(cols):]
            write_jasc(self.path(asset), cols[:n])
            return []
        notes: list[str] = []
        with Image.open(src) as im:
            im.load()
            target = self._dims(asset)
            if im.size != target:
                if not resize:
                    raise ProjectError(f"{src} is {im.size[0]}x{im.size[1]}, {asset.id} needs "
                                       f"{target[0]}x{target[1]} (pass --resize to scale it)")
                lay = asset.layout
                frame = (lay["frame_w"] * 8, lay["frame_h"] * 8) if asset.kind == "sprite" else None
                im = fit_to_asset(im, target, frame)
                notes.append(f"resized to {target[0]}x{target[1]}")
            rgba = np.array(im.convert("RGBA"), dtype=np.uint8)
            src_indexed = load_png_from(im) if im.mode == "P" else None
        palettes = self.current_palettes()
        # Indexed sources are trusted only if they already use this asset's palette; anything else is
        # matched by colour so a foreign palette can never silently overwrite the game's palette.
        loaded = LoadedImage(rgba.shape[1], rgba.shape[0], None, None, rgba)
        if src_indexed is not None and not new_palette:
            plte = formats.image_palette(asset, palettes)
            n = min(len(plte), len(src_indexed.palette))
            if n and all(same_color(src_indexed.palette[i], plte[i]) for i in range(n)) and \
                    int(src_indexed.indices.max(initial=0)) < n:
                loaded = src_indexed
        if new_palette:
            if asset.kind == "tileset" or (asset.kind == "bg" and len({l["palette"] for l in asset.palettes}) != 1):
                raise ProjectError("--new-palette is only supported for sprites and single-palette backgrounds")
            if not asset.palettes:
                raise ProjectError(f"{asset.id} has no palette of its own")
            link = asset.palettes[0]
            if asset.kind == "bg" and len(asset.palettes) > 1:
                raise ProjectError("--new-palette needs a background that uses a single palette bank")
            pid = link["palette"]
            idx, pal = quantize_new_palette(rgba, 16)
            cols = list(palettes[pid])
            cols[link["pal_bank"] * 16:link["pal_bank"] * 16 + 16] = pal
            palettes[pid] = cols
            write_jasc(self.path(self.assets[pid]), cols)
            others = [u for u in self.users_of_palette(pid) if u != asset.id]
            if others:
                notes.append(f"palette {pid} is shared with {', '.join(others)}; they will use the new colours too")
            if asset.kind == "sprite":
                loaded = LoadedImage(rgba.shape[1], rgba.shape[0], idx, pal, rgba)
            else:
                loaded = LoadedImage(rgba.shape[1], rgba.shape[0], idx | (link["png_bank"] << 4), None, rgba)
        raw = self.raw_view()
        parsed = formats.parse(asset, loaded, raw, palettes)
        notes.extend(parsed.warnings)
        overlay = _Overlay(self.raw_view(), parsed.blobs)
        pixels, plte = formats.render(asset, overlay, palettes)
        save_indexed_png(self.path(asset), pixels, plte, transparent0=asset.kind == "sprite" and bool(asset.palettes))
        if new_palette:
            self.sync_palette_views(asset.palettes[0]["palette"])
        return notes

    def _dims(self, asset: Asset) -> tuple[int, int]:
        if asset.kind == "sprite":
            from .gfx import Layout
            lay = Layout.from_dict(asset.layout)
            return lay.width, lay.height
        if asset.kind == "tileset":
            lay = formats.tileset_layout(asset)
            return lay.width, lay.height
        return asset.layout["map_w"] * 8, asset.layout["map_h"] * 8

    def normalize(self) -> list[str]:
        """Convert edited non-indexed PNGs into proper indexed images in place."""
        done = []
        for a in self.assets.values():
            if a.kind == "palette" or not self.is_touched(a):
                continue
            with Image.open(self.path(a)) as im:
                if im.mode == "P":
                    continue
            self.import_image(a, self.path(a))
            done.append(a.id)
        return done

    def relayout(self, asset: Asset, frame_w: int | None = None, frame_h: int | None = None,
                 cols: int | None = None, tiles_wide: int | None = None, palette: str | None = None,
                 label: str | None = None, force: bool = False) -> None:
        from .gfx import Layout
        if self.is_touched(asset) and (frame_w or frame_h or cols or tiles_wide or palette is not None) and not force:
            raise ProjectError(f"{asset.id} has unsaved edits; build or revert first (or pass --force to discard)")
        if label is not None:
            asset.label = label
        if asset.kind == "sprite":
            lay = Layout.from_dict(asset.layout)
            if tiles_wide:
                lay = Layout.sheet(lay.count, tiles_wide)
            if frame_w or frame_h:
                fw, fh = frame_w or lay.frame_w, frame_h or lay.frame_h
                if lay.count % (fw * fh):
                    raise ProjectError(f"{lay.count} tiles do not split into {fw}x{fh}-tile frames")
                lay = Layout.frames_of(lay.count, fw, fh, cols or 1)
            elif cols:
                lay = Layout.frames_of(lay.count, lay.frame_w, lay.frame_h, cols)
            asset.layout = {**asset.layout, **lay.to_dict()}
            if palette is not None:
                if palette in ("", "none"):
                    asset.palettes = []
                else:
                    pid = self.get(palette).id
                    if self.assets[pid].kind != "palette":
                        raise ProjectError(f"{pid} is not a palette")
                    asset.palettes = [{"palette": pid, "png_bank": 0, "pal_bank": 0}]
        elif asset.kind == "tileset" and tiles_wide:
            asset.layout["tiles_wide"] = tiles_wide
        self.write_asset_file(asset, self.current_palettes())
        self.save_manifest()


class _Overlay(dict):
    def __init__(self, base, top):
        super().__init__(top)
        self.base = base

    def __missing__(self, key):
        return self.base[key]


def load_png_from(im: Image.Image) -> LoadedImage:
    idx = np.array(im, dtype=np.uint8)
    raw_pal = im.getpalette() or []
    pal = [tuple(raw_pal[i:i + 3]) for i in range(0, len(raw_pal) - 2, 3)]
    return LoadedImage(im.size[0], im.size[1], idx, pal, np.array(im.convert("RGBA"), dtype=np.uint8))


def fit_to_asset(im: Image.Image, size: tuple[int, int], frame: tuple[int, int] | None = None) -> Image.Image:
    """Scale artwork for an asset. If the art is shaped like one animation frame rather than the whole
    sheet, it fills one frame and is repeated in every frame (e.g. Emerald's 2-frame front sprites)."""
    import math
    im = im.convert("RGBA")
    if frame and frame != size and size[0] % frame[0] == 0 and size[1] % frame[1] == 0:
        aspect = im.width / im.height
        if abs(math.log(aspect / (frame[0] / frame[1]))) < abs(math.log(aspect / (size[0] / size[1]))):
            one = _fit(im, frame)
            sheet = Image.new("RGBA", size, (0, 0, 0, 0))
            for y in range(0, size[1], frame[1]):
                for x in range(0, size[0], frame[0]):
                    sheet.paste(one, (x, y))
            return sheet
    return _fit(im, size)


def _fit(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Scale artwork into ``size`` keeping aspect ratio, centred on a transparent canvas."""
    w, h = im.size
    s = min(size[0] / w, size[1] / h)
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    method = Image.NEAREST if s >= 1 and float(s).is_integer() else Image.LANCZOS
    scaled = im.resize((nw, nh), method)
    canvas = Image.new("RGBA", size, (0, 0, 0, 0))
    canvas.paste(scaled, ((size[0] - nw) // 2, (size[1] - nh) // 2))
    return canvas


def _read_palette_source(src) -> list[RGB]:
    src = Path(src)
    if src.suffix.lower() == ".pal":
        return read_jasc(src)
    if src.suffix.lower() == ".json":
        data = json.loads(src.read_text())
        return [tuple(int(c.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4)) if isinstance(c, str) else tuple(c)
                for c in data]
    with Image.open(src) as im:
        if im.mode == "P":
            raw = im.getpalette()
            return [tuple(raw[i:i + 3]) for i in range(0, len(raw), 3)]
        cols = []
        for c in im.convert("RGB").getdata():
            if c not in cols:
                cols.append(c)
        return cols

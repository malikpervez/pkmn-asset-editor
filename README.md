# pkmn-asset-editor

Extract **every graphic** from a Gen 3 Pokémon ROM into an organised folder of
ordinary PNG and palette files, edit them by hand, in the built-in web editor,
or with an AI assistant, then **rebuild the ROM** with all edits inserted.
Data that grows is relocated and its pointers are updated automatically.

Supported games: **FireRed, LeafGreen, Ruby, Sapphire, Emerald**, in any
language and revision, including ROM hacks whose tables were moved. Graphics
tables are found by their structure, not by hardcoded offsets.

| What | Where it ends up |
|---|---|
| Pokémon front/back battle sprites, normal + shiny palettes, party icons | `graphics/pokemon/NNN_name/` |
| Trainer battle pictures + palettes | `graphics/trainers/NNN/` |
| Item icons + palettes | `graphics/items/NNN_name/` |
| Battle-animation sprites (laid out as animation frames) + palettes | `graphics/battle_anims/sprites/TAG/` |
| Battle-animation backgrounds (full tilemap images) | `graphics/battle_anims/backgrounds/NN/` |
| Battle backgrounds (terrain scenes and entry graphics) | `graphics/battle_backgrounds/NN/` |
| Overworld sprites (all walking/animation frames) + palettes | `graphics/overworld/NNN/` |
| Map tilesets (tiles in their real colours + 16 palettes) and metatile previews | `graphics/tilesets/`, `previews/tilesets/` |
| Any other LZ77-compressed graphics referenced by the game (title screens, menus, …) | `graphics/unknown/OFFSET.png` |

## Install

```bash
pip install -e .          # Python 3.10+, installs numpy + Pillow and the `pae` command
```

You must supply your own legally obtained ROM. ROMs and built files are
git-ignored.

## Quick start

```bash
pae scan  firered.gba                 # see what is found (no files written)
pae extract firered.gba -o fr-project # writes graphics/, manifest.json, AGENTS.md
cd fr-project

pae serve                             # web editor at http://127.0.0.1:8765
#   or edit graphics/**/*.png and *.pal with any editor (Aseprite, GIMP, Usenti, …)

pae status                            # what you changed
pae validate                          # check sizes, colours, conflicts, tile limits
pae build                             # -> build/rom.gba + build/rom.ups + build/rom.ips
```

`original.gba` inside the project is never modified. Every build starts again
from it, so builds are repeatable and you can never corrupt your base ROM.

### The web editor

`pae serve` opens a local gallery with categories and search. Clicking an asset
opens:

* a zoomable canvas with pencil, flood fill, eyedropper and undo
* the palette(s) that colour it: click a swatch to draw with it, shift-click to
  change the colour (changes are written to the `.pal` file and every image
  sharing that palette is recoloured)
* a shiny/alternate palette preview
* *Replace with image…*: upload any PNG; it is resized and mapped to the
  palette, or a new palette is built from it if you ask
* revert, download, and the asset's size and colour constraints

**Build ROM** in the header writes the ROM and lets you download it or the
patches.

## Editing rules

The tool enforces these rules and converts input for you wherever it can:

1. **Image sizes are fixed.** `pae import <id> art.png --resize` scales new art.
2. **Sprites use 16 colours, and index 0 is transparent.** Sprite PNGs are
   4-bit indexed. You can keep them indexed (same palette order) or save any
   RGB/RGBA PNG; colours are then mapped to the nearest palette entry.
   `--new-palette` builds a fresh 16-colour palette from the art.
3. **Colours are 15-bit**, so each channel is rounded to a multiple of 8.
4. **Palettes** can be edited in the `.pal` file (JASC-PAL text) *or* in the
   PNG's own palette. If both are edited, the `.pal` file wins. If two PNGs
   change the same palette colour differently, the build stops and tells you
   which ones conflict.
5. **Tilesets and backgrounds** are 8-bit indexed PNGs with `pixel = bank*16 + colour`,
   so they display in their real in-game colours.
   * Tilesets store only the colour part. The palette a tile uses in-game is
     chosen by the metatile.
   * Backgrounds are re-cut into 8x8 tiles automatically, with de-duplication
     including flipped tiles. They may not need more unique tiles than the
     original, because extra tiles would overflow the VRAM slot. Backgrounds
     that share tiles with another background are edited in place instead.
6. Overworld frames, icons and tileset palettes are stored uncompressed in the
   game, so they are always written in place.

## How reinsertion works

* Each asset points at one or more **blobs**: pieces of ROM data, either
  LZ77-compressed or raw. The build decodes every edited file and compares it
  with the original data. Only data that really changed is written; re-saving
  an image with identical pixels is a no-op.
* When several assets share a blob (one palette used by a front and back
  sprite, one tile sheet used by two backgrounds), their edits are merged per
  tile or per colour, and contradictory edits are reported.
* Compressed data is re-compressed with an optimal-parse, VRAM-safe LZ77
  encoder. If it still fits in its original slot it is written **in place**.
  Otherwise it is **relocated** into free space (runs of `0xFF` that nothing
  points into, biggest region first) and **every pointer** to the old location
  is updated. If no space is left, the ROM is expanded to 32 MiB.
* Every written block is decompressed again and verified before the ROM is
  saved. `build/build-report.json` lists everything that was written, moved and
  repointed.
* **UPS** and **IPS** patches are generated next to the ROM so you can share
  your work without distributing the game. `pae apply-patch` applies them.

## Using it with AI

The project is designed so an AI agent can work on it directly:

* **`AGENTS.md`** is written into every project. It explains the layout, the
  rules above and the commands.
* **`manifest.json`** describes every asset: id, file, label, frame layout,
  linked palettes, aliases and ROM offsets. `pae describe <id>` prints one
  asset with its constraints as JSON. `pae list --json` lists them all.
* **Contact sheets** let a multimodal model look at many sprites at once:
  `pae sheet pokemon -o sheet.png`, and `pae render <id> --palette <id>` for
  a single asset (e.g. its shiny palette).
* **`pae import`** and **`pae import-dir`** accept whatever an image generator
  produces (RGBA, any size, any number of colours) and convert it into a valid
  asset. `import-dir` mirrors asset ids, so `drop/pokemon/025_pikachu/front.png`
  replaces that sprite. This makes bulk updates of whole parts of the game a
  single command.
* **MCP server**: `pae mcp --project <dir>` exposes tools for listing,
  viewing (returns images), reading and painting pixels as text, importing
  images, setting palettes, validating and building.

  ```bash
  claude mcp add pae -- pae mcp --project /path/to/fr-project       # Claude Code
  ```
  ```json
  { "mcpServers": { "pae": { "command": "pae", "args": ["mcp", "--project", "/path/to/fr-project"] } } }
  ```

Example prompt for an agent: *"Read AGENTS.md. Give every Fire-type Pokémon's
shiny palette a blue tint, show me a contact sheet of the results, then validate
and build."*

## Command reference

| Command | Purpose |
|---|---|
| `pae scan ROM` | report which tables are found in a ROM |
| `pae extract ROM -o DIR [--force]` | create a project |
| `pae list [-c CATEGORY] [-k KIND] [-q TEXT] [--json]` | list assets |
| `pae describe ID` | one asset plus its constraints (JSON) |
| `pae import ID FILE [--resize] [--new-palette]` | convert and insert an image (or `.pal`/JSON palette) |
| `pae import-dir DIR [--resize] [--new-palette]` | bulk import by path |
| `pae set ID [--frame-w N --frame-h N --cols N --tiles-wide N --palette ID --label TEXT]` | re-layout or re-colour an asset's view (useful for `unknown/` assets) |
| `pae sync-palettes [ID…]` | recolour PNGs after editing `.pal` files |
| `pae normalize` | convert edited RGB PNGs to indexed PNGs in place |
| `pae sheet PREFIX -o FILE` / `pae render ID -o FILE` | previews |
| `pae status` / `pae validate` / `pae build [-o FILE] [--no-patch]` | check and build |
| `pae revert ID… / --all` | restore originals |
| `pae serve` / `pae mcp` | web editor / MCP server |
| `pae apply-patch ROM PATCH -o OUT` | apply UPS/IPS |

All project commands take `-p/--project DIR` (default `.`, or `$PAE_PROJECT`).

## How tables are found

`pae` reads the ROM as an array of 32-bit words and recognises each table type
by the shape of its entries:

* Pokémon/trainer pictures: `{pointer to LZ77, size 0x800, tag == index}` runs.
* Palettes: `{pointer, tag == index, 0}`. Shiny palettes start at tag 500.
* Battle-animation sprites and palettes: tags `10000 + index`. Frame sizes are
  read from the `SpriteTemplate`/`OamData` structures that use each tag.
* Icons: the icon palette table (tags 56000+), then the palette-index array
  and the icon table that precede it.
* Item icons: `{LZ77 of 0x120 bytes, LZ77 palette}` pairs.
* Battle and animation backgrounds: runs of tiles/palette/tilemap LZ77 pointers.
* Overworld sprites: `ObjectEventGraphicsInfo` structs (`tileTag 0xFFFF`,
  size = width × height / 2) and their frame lists, plus the pointer table that
  gives their graphics IDs and the palette table matching their tags.
* Tilesets: `Tileset` structs, paired primary↔secondary through the map
  layouts that use them. Per-tile palettes are inferred from the metatiles.
* Names come from the species and item name tables in the ROM, so renamed
  Pokémon in hacks get the right folder names.
* Anything else compressed that the game points to is collected under
  `unknown/`. Use `pae set` to give it the right width or palette once you know
  what it is.

## Limitations

* Gen 3 GBA games only. The project model is generic, but discovery is Gen 3
  specific.
* Uncompressed graphics outside the tables above are not found
  automatically.
* Map layouts, metatile arrangements, animation scripts and text are not
  edited. Only graphics and palettes are.
* Relocation updates every aligned word in the ROM that equals the old
  address. This is the standard approach in ROM-hacking tools. A random data
  word that happens to equal the address would also be changed, but this is
  extremely unlikely.
* Trainer pictures and animation tags are named by number, because the ROM
  has no names for them. Use `pae set ID --label …` to add your own.
* Discovery has only been tested against a synthetic ROM with the retail table
  layouts (see `tests/synthrom.py`), not against retail ROMs. If something is
  missing on a real ROM, `pae scan` shows what was and wasn't found.

## Development

```bash
pip install -e ".[test]"
pytest -q
```

The tests build a synthetic ROM (`tests/synthrom.py`) containing every table
type. They check extraction, no-op builds (byte-identical output), in-place and
relocated edits with pointer updates, palette edits and conflicts, RGBA import,
tilesets, background re-tiling, overworld frames, patches, the MCP server and
the web API.

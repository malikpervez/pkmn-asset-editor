# pkmn-asset-editor

Organise, view and edit **every graphic** of a Gen 3 Pokémon game — battle
sprites, icons, trainers, items, battle animations, overworld sprites, tilesets,
backgrounds, UI — by hand, in a built-in web editor, or with an AI assistant,
and get it back into the game safely.

It works in two modes:

| Mode | For | "Inserting" edits |
|---|---|---|
| **Source tree** | **pokeemerald-expansion**, pokeemerald, pokefirered (decomp projects) | edits are written straight into the repo's PNG/.pal/.bin files, validated, and `make` builds the ROM |
| **ROM** | unmodified retail FireRed/LeafGreen/Ruby/Sapphire/Emerald ROMs (or binary hacks) | edits are recompressed and written into a copy of the ROM, relocating data and repointing as needed |

The same commands, web editor and MCP server work in both modes: pass the repo
folder or an extracted ROM project as `--project`.

## Source-tree mode (pokeemerald-expansion)

In a decomp every graphic already is a file and `make` builds the ROM, so there
is nothing to extract. `pae` adds the layer that makes editing them safe:

* **An organised index of all ~21,000 graphics**, built from the `INCGFX_*` /
  `INCBIN_*` references in `src/` (symbol names, output format, gbagfx
  arguments such as frame sizes), grouped into categories (pokemon, trainers,
  items, battle_anims, overworld, tilesets, …). Unreferenced files are flagged.
* **The palette each image really uses in game**, which is often not the PNG's
  own palette:
  * Pokémon sprites → `normal.pal` / `shiny.pal`. Forms inherit from the parent
    folder. Overworld followers → `overworld_normal.pal`.
  * Icons → `icon_palettes/palN.pal` via `.iconPalIndex`, including ternaries
    on config flags.
  * Overworld sprites → `.paletteTag` through the graphics info and palette
    tag tables.
  * Trainers and items → paired symbols like `gTrainerFrontPic_X` ↔
    `gTrainerPalette_X`, or `{ .iconPic, .iconPalette }` initializers.
    Alternative palettes (shiny, a shared icon used by Super Potion) can be
    previewed.
* **Coloured, editable views of tilesets** (`…/general@tileset`). Each tile is
  shown with the palette `metatiles.bin` gives it, and secondary tilesets
  borrow their primary's palettes via `layouts.json`. **Tilemap backgrounds**
  (`…/dark@bg`, battle environments, title screens) can be edited as one image
  and are re-cut into tiles and a `.bin` tilemap.
* **Validation (`pae check`)** that catches what breaks `make` or looks wrong:
  * RGB/RGBA PNGs (gbagfx rejects them)
  * sizes that are not multiples of 8, changed sizes, sheets that don't split
    into frames
  * colour indices above 15, which gbagfx silently wraps
  * PNG palettes that grew, which overwrites neighbouring palettes in game
  * colours edited in a PNG whose in-game palette is a separate `.pal`
  * deleted files still referenced by the source
* **Artwork conversion (`pae import`)**: any PNG (RGBA, any size, any number of
  colours) is turned into a valid indexed PNG mapped to the in-game palette.
  `--new-palette` builds a fresh palette, writes `normal.pal`, and remaps the
  sibling sprites that share it. Art shaped like one frame fills every
  animation frame.
* **git-based** status and revert, and `pae build` = check + `make`.

```bash
pip install -e /path/to/pkmn-asset-editor
cd ~/my-expansion-game

pae check --all                       # sanity-check the whole tree (fast, read-only)
pae serve                             # web editor at http://127.0.0.1:8765
pae list -c pokemon -q charizard
pae describe graphics/pokemon/charizard/anim_front.png     # size, frames, in-game palette, symbols
pae import graphics/pokemon/charizard/back.png new_art.png --resize
pae check                             # validates only what you changed
pae build                             # check, then `make -jN` (pae build --make "make -j8 DEBUG=1")
pae revert --all                      # undo (git checkout of the edited files)
pae guide > GRAPHICS_AGENTS.md        # instructions for AI agents working in your repo
```

### Maps, events and story scripts

`pae serve` also has a **map editor** at `/maps` ("Maps & events" in the
header). It edits the same files as Porymap (`map.json`, `layouts.json`,
`map_groups.json`, `map.bin`, `scripts.inc`), so `make` picks the changes up
like any other edit.

* **View any map** (Emerald and FRLG layouts) rendered from its real tilesets,
  with NPCs drawn using their overworld sprites, plus warps, triggers and signs.
  There are optional grid and collision/elevation overlays.
* **Paint the map.** Pick a metatile, or drag out a multi-block stamp (a whole
  house, a fence, a patch of grass) from the metatile panel. Use *Copy* to
  grab blocks already on any map and paste them elsewhere. Flood fill,
  collision/elevation painting, and undo/redo are available.
* **Events.** Add, drag, duplicate and delete NPCs, warps, triggers and signs.
  Their fields autocomplete from your constants (`OBJ_EVENT_GFX_*`,
  `MOVEMENT_TYPE_*`, `FLAG_*`, `VAR_*`, `MAP_*`, …) and from every script
  label in the project. *Make two-way* links a warp and its destination.
  *New script…* writes a ready-to-edit message script and hooks it up.
* **Story scripts.** Edit the map's `scripts.inc` (or `scripts.pory`) with a
  label jump list. "Open script" on an event goes straight to its code.
* **Map header.** Music, weather, map type, flags, connections, and layout
  resize.
* **New maps (e.g. a new house).** *New map…* copies an existing map, such as
  a house interior, under a new name. Its scripts and `LOCALID_*`s are renamed,
  it gets its own copy of the layout, and it is registered in
  `map_groups.json`/`layouts.json`. Then add a warp on a door and click
  *Make two-way*.
* **Check** validates the edited maps:
  * warps to missing maps or warp ids
  * scripts that aren't defined anywhere
  * unknown graphics ids
  * duplicate local ids
  * events outside the map
  * block data that doesn't match the layout size
  * metatile ids the tilesets don't have

The same operations exist on the command line and as MCP tools, so an AI can
build maps and story too:

```bash
pae map list petalburg
pae map render PetalburgCity -o petalburg.png --scale 2
pae map metatiles PetalburgCity -o metatiles.png           # ids to paint with
pae map events PetalburgCity
pae map new PetalburgCity_House1 PetalburgCity_House3       # new house interior
pae map link PetalburgCity 6 PetalburgCity_House3 0         # door <-> house, both ways
```

MCP map tools: `list_maps`, `view_map`, `get_map`, `view_metatiles`,
`get_blocks`/`set_blocks` (blocks as text), `copy_blocks`, `edit_map`
(header + add/update/delete events), `get_script`/`write_script`/`new_script`,
`duplicate_map`, `link_warps`, `resize_map`, `find_constants`.

### Build and play-test

`pae` can build your game and **play it headlessly**, so you (or an AI) can see
an edit running in the game itself, not just in the files.

```bash
scripts/setup-toolchain.sh             # once: arm-none-eabi-gcc, libpng, libmgba (apt / brew / pacman)
pae doctor                              # checks toolchain, emulator, Quickstart, built ROM
pae play PetalburgCity --x 17 --y 11 --inputs "UP, wait 10, A, wait 60" -o sign.png --gif sign.gif
```

How it works:

1. `pae play` runs `make`. A no-op build takes about 1 s; a changed map rebuilds
   in under a minute.
2. It boots the ROM in a headless libmgba driver (`native/pae_mgba.c`, compiled
   on first use) and starts a fresh game with expansion's **Quickstart**.
3. It **warps to the map** by writing the destination into the saveblock and
   loading it through `CB2_LoadMap`, the path a door warp takes, using symbol
   addresses from the build's `.elf`. NPCs, map scripts and story triggers run
   as they do for a new save.
4. Inputs are a small script: `KEY`, `KEY*frames` (16 frames = one step),
   `A+B 4`, `wait 60`. Results come back as a PNG, or as a GIF with `--gif`.

* In the map editor, **▶ Play-test** opens the game at the selected event or the
  clicked tile. You can then play it with on-screen buttons or the keyboard,
  and rebuild/restart after edits.
* AI assistants get `playtest_start` and `playtest_press` MCP tools that return
  screenshots.
* **Changes** (graphics page) shows before/after renders of every edited sprite,
  palette, tileset and map, plus text diffs of scripts and JSON. This makes it
  easy to review what an AI changed before committing.

`pae` never commits, and writes nothing outside the files you edit and its own
`.pae/` folder. `.pae/` is excluded via `.git/info/exclude`, so your `.gitignore`
is not touched.

## ROM mode

Extract every graphic from a retail ROM into an organised folder of ordinary PNG
and palette files, edit them, then **rebuild the ROM** with all edits inserted.
Data that grows is relocated and its pointers are updated automatically.
Graphics tables are found by their structure, not by hardcoded offsets, so
every language and revision works, including binary hacks whose tables were
moved. pokeemerald-expansion games use different data structures and their own
compression, so use source-tree mode for those.

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

### Install

```bash
pip install -e .          # Python 3.10+, installs numpy + Pillow and the `pae` command
```

You must supply your own legally obtained ROM. ROMs and built files are
git-ignored.

### Quick start

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

### The web editor (both modes)

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

### Editing rules

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

### How reinsertion works

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

* **`AGENTS.md`** is written into every ROM project. In a source tree,
  `pae guide` prints the equivalent. Both explain the layout, the rules and the
  commands.
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
  claude mcp add pae -- pae mcp --project ~/my-expansion-game       # Claude Code, source tree
  claude mcp add pae -- pae mcp --project /path/to/fr-project       # or an extracted ROM project
  ```
  ```json
  { "mcpServers": { "pae": { "command": "pae", "args": ["mcp", "--project", "/path/to/fr-project"] } } }
  ```

Example prompts for an agent:
*"Run `pae guide` and follow it. Give every Fire-type Pokémon's shiny palette
a blue tint, show me a contact sheet of the results, then run `pae check`."*
*"Replace the back sprites in drop/ with `pae import-dir drop --resize`, fix
anything `pae check` reports, then build."*

## Command reference

| Command | Purpose |
|---|---|
| `pae scan ROM` | report which tables are found in a ROM |
| `pae check [--all]` | source tree: validate edited files (or every graphic) |
| `pae fix [FILE…]` | convert RGB/RGBA PNGs into valid indexed PNGs (source tree and ROM project) |
| `pae palette-from-png FILE…` | source tree: copy a PNG's colours into the `.pal` the game uses for it |
| `pae guide` | print AI-agent instructions for the project |
| `pae extract ROM -o DIR [--force]` | create a project |
| `pae list [-c CATEGORY] [-k KIND] [-q TEXT] [--json]` | list assets |
| `pae describe ID` | one asset plus its constraints (JSON) |
| `pae import ID FILE [--resize] [--new-palette]` | convert and insert an image (or `.pal`/JSON palette) |
| `pae import-dir DIR [--resize] [--new-palette]` | bulk import by path |
| `pae set ID [--frame-w N --frame-h N --cols N --tiles-wide N --palette ID --label TEXT]` | re-layout or re-colour an asset's view (useful for `unknown/` assets) |
| `pae sync-palettes [ID…]` | recolour PNGs after editing `.pal` files |
| `pae sheet PREFIX -o FILE` / `pae render ID -o FILE` | previews |
| `pae status` / `pae validate` / `pae build [-o FILE] [--no-patch]` | check and build |
| `pae revert ID… / --all` | restore originals |
| `pae doctor` | check toolchain, emulator, Quickstart and built ROM |
| `pae play MAP [--x --y --inputs … -o PNG --gif GIF]` | build, then play-test a map headlessly |
| `pae map list / render / metatiles / events / new / link` | maps, events and warps |
| `pae serve` / `pae mcp` | web editor / MCP server |
| `pae apply-patch ROM PATCH -o OUT` | apply UPS/IPS |

All project commands take `-p/--project DIR` (default `.`, or `$PAE_PROJECT`).

## How ROM-mode tables are found

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

* Gen 3 GBA games only.
* Source-tree mode has been run against the latest upstream
  pokeemerald-expansion (it indexes all ~21.5k graphics and `pae check --all`
  is clean), but not against your fork. Palette linking relies on expansion's
  naming and data conventions, so heavily customised code may need
  `pae describe` to confirm which palette an image uses. `pae build` only
  calls your `make`, so your normal toolchain must be installed.
* Uncompressed graphics outside the tables above are not found
  automatically.
* ROM mode edits graphics and palettes only. Maps, events and scripts are
  edited in source-tree mode.
* The map editor edits maps, events, headers and scripts, but not metatile
  definitions (which 8x8 tiles make up a metatile) or tileset animations.
  Use Porymap's tileset editor for those.
* Play-testing starts from a new save (Quickstart), so flags and vars are
  unset. It needs a non-release build with `ENABLE_QUICKSTART`, and libmgba.
* Relocation updates every aligned word in the ROM that equals the old
  address. This is the standard approach in ROM-hacking tools. A random data
  word that happens to equal the address would also be changed, but this is
  extremely unlikely.
* Trainer pictures and animation tags are named by number, because the ROM
  has no names for them. Use `pae set ID --label …` to add your own.
* ROM-mode discovery has only been tested against a synthetic ROM with the
  retail table layouts (see `tests/synthrom.py`), not against retail ROMs. If
  something is missing on a real ROM, `pae scan` shows what was and wasn't
  found.

## Development

```bash
pip install -e ".[test]"
pytest -q
```

The tests build a synthetic ROM (`tests/synthrom.py`) containing every table
type, and a miniature expansion-style git repository (`tests/decomp_fixture.py`). They check extraction, no-op builds (byte-identical output), in-place and
relocated edits with pointer updates, palette edits and conflicts, RGBA import,
tilesets, background re-tiling, overworld frames, patches, the MCP server and
the web API. In source-tree mode they also check palette linking, validation,
`fix`, imports with new palettes, tileset/background views, git revert and
`make`.

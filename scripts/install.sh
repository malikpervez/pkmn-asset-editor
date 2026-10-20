#!/usr/bin/env bash
# One-command install for Linux, macOS and Windows (inside WSL):
#   bash scripts/install.sh            (from a copy of this repository)
# It installs the game toolchain (compiler, libpng, libmgba), then pae itself into ~/.pae-venv, and puts a `pae`
# command in ~/.local/bin. Re-running is safe: it updates everything.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

say "1/3  Installing the tools that build and play your game…"
bash "$HERE/pkmn_asset_editor/tools/setup-toolchain.sh"

say "2/3  Installing Python and pae…"
if ! command -v python3 >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get install -y -q python3 python3-venv python3-pip
  elif command -v brew >/dev/null 2>&1; then brew install python
  else echo "Please install Python 3.10 or newer, then run this again." >&2; exit 1; fi
fi
if ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get install -y -q python3-venv
fi
python3 -m venv "$HOME/.pae-venv"
"$HOME/.pae-venv/bin/pip" install -q --upgrade pip
"$HOME/.pae-venv/bin/pip" install -q "$HERE"

say "3/3  Adding the pae command…"
mkdir -p "$HOME/.local/bin"
ln -sf "$HOME/.pae-venv/bin/pae" "$HOME/.local/bin/pae"
case ":$PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
       [ -f "$rc" ] && ! grep -q '.local/bin' "$rc" && echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$rc"
     done
     export PATH="$HOME/.local/bin:$PATH" ;;
esac

say "All done!"
cat <<'MSG'
Next:
  cd path/to/your/game        (your pokeemerald-expansion folder)
  pae doctor --fix            checks everything and fixes what it can
  pae serve                   then open http://localhost:8765 in your browser

(Open a new terminal first if `pae` isn't found.)
MSG

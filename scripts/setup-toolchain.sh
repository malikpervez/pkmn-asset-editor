#!/usr/bin/env bash
# Install what `pae build` / `pae play` need to build pokeemerald-expansion and play-test it headlessly:
#   * arm-none-eabi GCC + newlib   (compiles the game; expansion's INSTALL.md lists alternatives such as devkitARM)
#   * a host C compiler, make, libpng (builds the repo's tools: gbagfx, mapjson, …)
#   * libmgba                       (pae's headless emulator driver for play-testing)
# Usage: scripts/setup-toolchain.sh        (re-running is safe)
set -euo pipefail

if command -v apt-get >/dev/null 2>&1; then
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  $SUDO apt-get update -q
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -q \
    build-essential git libpng-dev pkg-config \
    gcc-arm-none-eabi binutils-arm-none-eabi libnewlib-arm-none-eabi \
    libmgba-dev
elif command -v brew >/dev/null 2>&1; then
  brew install libpng mgba
  brew install --cask gcc-arm-embedded || echo "Install the Arm GNU toolchain or devkitARM manually (see INSTALL.md)"
elif command -v pacman >/dev/null 2>&1; then
  sudo pacman -S --needed base-devel libpng arm-none-eabi-gcc arm-none-eabi-newlib mgba-qt
else
  echo "Unsupported package manager: install arm-none-eabi-gcc + newlib, libpng, make and libmgba manually." >&2
  exit 1
fi
echo
echo "Done. In your game repository run:  pae doctor"

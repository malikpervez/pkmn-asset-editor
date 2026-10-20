#!/usr/bin/env bash
# Kept for old instructions: the script now ships inside the package (so `pae doctor --fix` can run it).
exec bash "$(dirname "$0")/../pkmn_asset_editor/tools/setup-toolchain.sh" "$@"

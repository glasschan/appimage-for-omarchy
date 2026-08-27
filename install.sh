#!/bin/bash
# Install (or cleanly re-sync) "AppImage for Omarchy" into the Omarchy user
# plugin directory. Only runtime files are copied — repo/dev artifacts
# (PRD.md, tests, __pycache__, ...) never end up in ~/.config.
set -euo pipefail

PLUGIN_ID="io.github.glasschan.appimage"
SRC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PLUGINS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins"
DEST="$PLUGINS_DIR/$PLUGIN_ID"

fail() {
  echo "install.sh: $*" >&2
  exit 1
}

# --- Safety checks: never write outside our own plugin directory. ----------
[[ -f "$SRC_DIR/manifest.json" ]] ||
  fail "no manifest.json in $SRC_DIR; run this script from the repo root"

manifest_id="$(jq -r '.id // empty' "$SRC_DIR/manifest.json" 2>/dev/null ||
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("id",""))' \
    "$SRC_DIR/manifest.json")"
[[ "$manifest_id" == "$PLUGIN_ID" ]] ||
  fail "manifest id '$manifest_id' does not match expected '$PLUGIN_ID'"

[[ -n "$HOME" && "$PLUGINS_DIR" == "$HOME"/* ]] ||
  fail "plugins dir '$PLUGINS_DIR' is not under \$HOME; refusing to touch it"
[[ "$(basename -- "$DEST")" == "$PLUGIN_ID" && "$DEST" == "$PLUGINS_DIR/$PLUGIN_ID" ]] ||
  fail "internal error: unexpected dest '$DEST'"

# --- Sync runtime files only. -----------------------------------------------
# Runtime set: manifest, root QML, lib/, icons/, backend/main.py,
# backend/omarchy_appimage/ (source only), README, LICENSE.
mkdir -p -- "$DEST"

# Empty the destination directory first so a previous full-repo copy
# (PRD.md, tests, ...) is cleanly overwritten. The directory itself is kept
# (same inode) to stay friendly to the shell's file watcher.
find "$DEST" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +

if command -v rsync >/dev/null 2>&1; then
  rsync -a \
    --include='/manifest.json' \
    --include='/*.qml' \
    --include='/README.md' \
    --include='/LICENSE' \
    --include='/lib/' --include='/lib/**' \
    --include='/icons/' --include='/icons/**' \
    --include='/backend/' \
    --include='/backend/main.py' \
    --include='/backend/omarchy_appimage/' \
    --include='/backend/omarchy_appimage/*.py' \
    --exclude='*' \
    "$SRC_DIR/" "$DEST/"
else
  # No rsync: plain cp with the same include set.
  cp -a -- "$SRC_DIR/manifest.json" "$SRC_DIR"/*.qml \
    "$SRC_DIR/README.md" "$SRC_DIR/LICENSE" "$DEST/"
  mkdir -p -- "$DEST/lib" "$DEST/icons" "$DEST/backend/omarchy_appimage"
  cp -a -- "$SRC_DIR/lib/." "$DEST/lib/"
  cp -a -- "$SRC_DIR/icons/." "$DEST/icons/"
  cp -a -- "$SRC_DIR/backend/main.py" "$DEST/backend/"
  cp -a -- "$SRC_DIR/backend/omarchy_appimage/." "$DEST/backend/omarchy_appimage/"
fi

# Defense in depth: strip bytecode and OS cruft from the installed copy.
find "$DEST" -type d -name '__pycache__' -prune -exec rm -rf -- {} +
find "$DEST" -type f \( -name '*.pyc' -o -name '.DS_*' \) -delete

# --- Validate and tell the shell about the new files. ----------------------
if command -v omarchy >/dev/null 2>&1; then
  omarchy plugin validate "$DEST" || fail "installed copy failed validation"
fi
if command -v omarchy-shell >/dev/null 2>&1; then
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
fi

echo "Installed runtime files to: $DEST"

enabled="unknown"
if command -v omarchy >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
  enabled="$(omarchy plugin list --json 2>/dev/null |
    jq -r --arg id "$PLUGIN_ID" '.[] | select(.id == $id) | .enabled // empty' ||
    echo unknown)"
fi
if [[ "$enabled" == "true" ]]; then
  echo "Plugin is enabled; files hot-reload (rescan already requested)."
else
  echo "Plugin is not enabled yet. Enable it with:"
  echo "  omarchy plugin enable $PLUGIN_ID"
  echo "Saving files under ~/.config/omarchy/plugins/ hot-reloads; if needed:"
  echo "  omarchy-shell shell rescanPlugins"
fi

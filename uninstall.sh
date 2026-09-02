#!/bin/bash
# Uninstall "AppImage for Omarchy": disable the plugin in the shell, remove
# its directory from ~/.config/omarchy/plugins, and suggest a shell restart.
set -euo pipefail

PLUGIN_ID="io.github.glasschan.appimage"
PLUGINS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins"
DEST="$PLUGINS_DIR/$PLUGIN_ID"
SHELL_JSON="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/shell.json"

fail() {
  echo "uninstall.sh: $*" >&2
  exit 1
}

# --- Safety checks before any rm -rf. ---------------------------------------
[[ -n "$HOME" && "$PLUGINS_DIR" == "$HOME"/* ]] ||
  fail "plugins dir '$PLUGINS_DIR' is not under \$HOME; refusing to touch it"
[[ "$(basename -- "$DEST")" == "$PLUGIN_ID" && "$DEST" == "$PLUGINS_DIR/$PLUGIN_ID" ]] ||
  fail "internal error: unexpected dest '$DEST'"
[[ ! -L "$DEST" || ! -e "$DEST" ]] ||
  fail "'$DEST' is a symlink; refusing — remove it manually"

# --- 1) Disable the plugin. -------------------------------------------------
# Note: `omarchy plugin --help` may exit non-zero even when it prints usage,
# so match on its output rather than its exit code (we run under pipefail).
plugin_help=""
if command -v omarchy >/dev/null 2>&1; then
  plugin_help="$(omarchy plugin --help 2>&1 || true)"
fi
if command -v omarchy >/dev/null 2>&1 &&
  grep -q -- 'plugin disable' <<<"$plugin_help"; then
  omarchy plugin disable "$PLUGIN_ID" || true
else
  # Fallback: drop the widget from the bar layout in shell.json.
  # The shell hot-reloads shell.json on save.
  if [[ -f "$SHELL_JSON" ]]; then
    backup="${SHELL_JSON}.bak.$(date -u +%Y%m%d%H%M%S)"
    cp -a -- "$SHELL_JSON" "$backup"
    echo "Backup of shell.json at: $backup"
    if command -v jq >/dev/null 2>&1; then
      tmp="$(mktemp)"
      jq --arg id "$PLUGIN_ID" '
        .bar.layout |= with_entries(.value |= map(select(.id != $id)))
      ' "$SHELL_JSON" >"$tmp" && mv -- "$tmp" "$SHELL_JSON"
    elif command -v python3 >/dev/null 2>&1; then
      python3 - "$SHELL_JSON" "$PLUGIN_ID" <<'PY'
import json
import sys

path, plugin_id = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
layout = cfg.get("bar", {}).get("layout", {})
for section, entries in layout.items():
    if isinstance(entries, list):
        layout[section] = [
            e for e in entries
            if not (isinstance(e, dict) and e.get("id") == plugin_id)
        ]
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY
    else
      fail "neither jq nor python3 available to edit $SHELL_JSON"
    fi
    echo "Removed $PLUGIN_ID from the bar layout in $SHELL_JSON"
  fi
fi

# --- 2) Remove the plugin directory (only ever our own). --------------------
if [[ -e "$DEST" ]]; then
  rm -rf -- "$DEST"
  echo "Removed: $DEST"
else
  echo "Nothing installed at: $DEST"
fi

# --- 3) Let the shell drop it without a full restart, then hint one. --------
if command -v omarchy-shell >/dev/null 2>&1; then
  omarchy-shell shell rescanPlugins >/dev/null 2>&1 || true
fi

echo "Done. If the widget is still on the bar, run:  omarchy restart shell"

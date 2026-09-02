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
#
# quickshell watches the plugin dir and hot-reloads on every file event, but
# this plugin must NEVER be hot-reloaded in place: per quickshell issue #972
# (open; unfixed in the 0.3.1 on this machine), when a backend Process exits
# during a plugin hot-reload, IpcHandler::onPostReload segfaults on a
# dynamic_cast of a stale object — and this panel runs short-lived backend
# processes (including a 10s running-state poll), so ANY watcher-triggered
# reload risks crashing the shell. The deploy rule is therefore: swap while
# the shell is stopped (see below); the fresh instance then loads the new
# files from disk — no in-place reload, no rescan.
#
# Primary protection: the shell is stopped around the swap, so a watcher can
# never see it. Defensive leftovers: BOTH swap-side directories (staging and
# the moved-aside old tree) stay dot-prefixed anyway. The shell's watcher
# ignores dot-entries (the dotted staging dir produced zero watcher events),
# while a visible duplicate plugin dir (full manifest + tree, duplicate
# manifest id) fired dozens of "Local plugin changed, reloading" events and
# made quickshell 0.3.1 segfault (Signal 11) during hot-reload incubation.
mkdir -p -- "$PLUGINS_DIR"

STAGING="$PLUGINS_DIR/.${PLUGIN_ID}.staging.$$"
OLD_DIR="$PLUGINS_DIR/.${PLUGIN_ID}.old.$$"
cleanup_staging() {
  if [[ -n "${STAGING:-}" && -d "${STAGING:-}" ]]; then
    rm -rf -- "$STAGING"
  fi
}
trap cleanup_staging EXIT

rm -rf -- "$STAGING"
mkdir -p -- "$STAGING"

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
    "$SRC_DIR/" "$STAGING/"
else
  # No rsync: plain cp with the same include set.
  cp -a -- "$SRC_DIR/manifest.json" "$SRC_DIR"/*.qml \
    "$SRC_DIR/README.md" "$SRC_DIR/LICENSE" "$STAGING/"
  mkdir -p -- "$STAGING/lib" "$STAGING/icons" "$STAGING/backend/omarchy_appimage"
  cp -a -- "$SRC_DIR/lib/." "$STAGING/lib/"
  cp -a -- "$SRC_DIR/icons/." "$STAGING/icons/"
  cp -a -- "$SRC_DIR/backend/main.py" "$STAGING/backend/"
  cp -a -- "$SRC_DIR/backend/omarchy_appimage/." "$STAGING/backend/omarchy_appimage/"
fi

# Defense in depth: strip bytecode and OS cruft from the staged copy.
find "$STAGING" -type d -name '__pycache__' -prune -exec rm -rf -- {} +
find "$STAGING" -type f \( -name '*.pyc' -o -name '.DS_*' \) -delete

# --- Validate the staging copy BEFORE touching anything live. ----------------
# `omarchy plugin validate` is a pure file/schema check that needs no
# running shell, so a bad staging tree aborts here: old installation
# untouched, shell never stopped.
if command -v omarchy >/dev/null 2>&1; then
  omarchy plugin validate "$STAGING" ||
    fail "staged copy failed validation; nothing was swapped (old installation intact, shell not stopped)"
fi

# --- Stop the shell so the swap can never trigger a reload. ------------------
# Per quickshell issue #972 (see note above) ANY in-place reload of this
# plugin can segfault quickshell 0.3.1, so deploy while the shell is down.
# `omarchy restart shell` is not enough (it restarts immediately), so stop
# quickshell ourselves and wait for it to fully exit before swapping.
QS_PAT='quickshell -n -p /usr/share/omarchy/shell'
if pgrep -f "$QS_PAT" >/dev/null 2>&1; then
  echo "Stopping the Omarchy shell for a no-hot-reload swap (quickshell #972)..."
  pkill -f "$QS_PAT" || true
  for _ in $(seq 1 30); do
    pgrep -f "$QS_PAT" >/dev/null 2>&1 || break
    sleep 0.1
  done
  if pgrep -f "$QS_PAT" >/dev/null 2>&1; then
    fail "quickshell did not exit within ~3s; nothing was swapped (staging cleaned up)"
  fi
fi

# --- Failure paths after the swap: keep a bootable tree installed. -----------
# The previous installation is retained in OLD_DIR until validation AND
# restart handling have fully succeeded, so a failed post-swap validation
# or restart rolls back to the known-good tree and brings the shell back
# up. Fresh installs (no prior DEST) have nothing to roll back to.
shell_up_attempt() {
  if command -v omarchy >/dev/null 2>&1; then
    if ! omarchy restart shell; then
      echo "install.sh: WARNING: 'omarchy restart shell' failed; the shell is still down." >&2
      echo "install.sh: start it manually with: omarchy restart shell" >&2
    fi
  else
    echo "install.sh: NOTE: 'omarchy' CLI not found; the shell is still stopped. Start it with:" >&2
    echo "  omarchy restart shell" >&2
  fi
}

restore_old_and_die() {
  echo "install.sh: $*" >&2
  if [[ -d "$OLD_DIR" ]]; then
    rm -rf -- "$DEST"
    if mv -- "$OLD_DIR" "$DEST"; then
      echo "install.sh: previous installation restored." >&2
    else
      echo "install.sh: CRITICAL: rollback failed; the previous installation is left at $OLD_DIR" >&2
    fi
  else
    echo "install.sh: no previous tree to roll back to (fresh install; the new tree is left in place)." >&2
  fi
  shell_up_attempt
  exit 1
}

# --- Swap the finished tree into place. --------------------------------------
# If any rename fails, fail loudly: the old copy is restored where
# possible and the shell is never left down without a restart attempt.
# OLD_DIR is dot-prefixed + pid-tagged so the watcher never sees it (see
# note above); do not make it visible again.
rm -rf -- "$OLD_DIR"
if [[ -e "$DEST" ]]; then
  if ! mv -- "$DEST" "$OLD_DIR"; then
    shell_up_attempt
    fail "could not move the current installation aside"
  fi
  if ! mv -- "$STAGING" "$DEST"; then
    if ! mv -- "$OLD_DIR" "$DEST"; then
      shell_up_attempt
      fail "restore failed; previous installation left at $OLD_DIR"
    fi
    shell_up_attempt
    fail "could not move staged files into place; previous installation restored"
  fi
else
  if ! mv -- "$STAGING" "$DEST"; then
    shell_up_attempt
    fail "could not move staged files into place"
  fi
fi

# --- Validate the live DEST post-swap, then bring the shell back up. --------
# `omarchy plugin validate` is pure file/schema checking and works while
# the shell is down; `omarchy plugin list` talks to the shell over IPC, so
# it is only usable after the restart below. On ANY failure the retained
# old tree is restored before giving up.
if command -v omarchy >/dev/null 2>&1; then
  omarchy plugin validate "$DEST" ||
    restore_old_and_die "installed copy failed validation"
  # The shell was stopped for the swap; bring it back. The fresh instance
  # loads the new files from disk, so there is deliberately no rescan
  # call — any in-place reload of this plugin must never be triggered,
  # since it can segfault quickshell 0.3.1 (issue #972). A failed restart
  # rolls back too: the shell stays down either way, so at least leave
  # the known-good tree installed.
  if ! omarchy restart shell; then
    restore_old_and_die "'omarchy restart shell' failed"
  fi
else
  echo "NOTE: 'omarchy' CLI not found; the shell is still stopped. Start it with:"
  echo "  omarchy restart shell"
fi

# Success: validate + restart both made it past — the retained old tree
# can finally go.
rm -rf -- "$OLD_DIR"

echo "Installed runtime files to: $DEST"

enabled="unknown"
if command -v omarchy >/dev/null 2>&1 && command -v jq >/dev/null 2>&1; then
  enabled="$(omarchy plugin list --json 2>/dev/null |
    jq -r --arg id "$PLUGIN_ID" '.[] | select(.id == $id) | .enabled // empty' ||
    echo unknown)"
fi
if [[ "$enabled" == "true" ]]; then
  echo "Plugin is enabled; the shell was stopped for the swap and restarted, so it"
  echo "loaded the new files cleanly — no in-place hot-reload, which can segfault"
  echo "quickshell 0.3.1 (https://github.com/quickshell-mirror/quickshell/issues/972)."
else
  echo "Plugin is not enabled yet. Enable it with:"
  echo "  omarchy plugin enable $PLUGIN_ID"
  echo "Note: deploys stop and restart the shell instead of hot-reloading, because"
  echo "ANY in-place reload can segfault quickshell 0.3.1 (issue #972)."
fi

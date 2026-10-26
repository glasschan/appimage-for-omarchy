#!/bin/bash
# qml-compile-check.sh — compile the plugin's QML with the real engine.
#
# The static gate (scripts/ci/qml-property-gate.sh) catches assignments to
# properties our own components do not declare. This is the runtime half: it
# asks Quickshell to compile every entry point the way the shell does and
# fails when a component does not reach Component.Ready.
#
# Why it matters: a QML component that fails to compile takes the whole
# panel down, and the omarchy shell swallows the load error (its Loader
# error handler calls `errorString()` as a function), so the only symptom
# is a bar button that does nothing when pressed. The panel was dead from
# v0.6.3 to v0.6.8 for exactly this reason.
#
# Quickshell is not part of the CI runner image, so this script SKIPS (exit
# 0) when quickshell is unavailable — it is a local/developer check, run on
# the machine that has the plugin installed.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$REPO_ROOT"

ENTRY_POINTS=(Panel.qml BarWidget.qml ThemeIcon.qml SafeTextField.qml Service.qml PythonDependencyCard.qml)

if ! command -v quickshell >/dev/null 2>&1; then
  echo "SKIP: quickshell is not installed — QML compile check not run"
  echo "      (install quickshell, or rely on scripts/ci/qml-property-gate.sh)"
  exit 0
fi

OMARCHY_SHELL_DIR="${OMARCHY_PATH:-/usr/share/omarchy}/shell"
if [ ! -f "$OMARCHY_SHELL_DIR/Commons/qmldir" ] \
    || [ ! -f "$OMARCHY_SHELL_DIR/Ui/qmldir" ]; then
  echo "SKIP: omarchy shell modules not found in $OMARCHY_SHELL_DIR"
  echo "      (QML compile check not run)"
  exit 0
fi

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/appimage-qml-check.XXXXXX")"
trap 'rm -rf -- "$WORK_DIR"' EXIT

# The plugin QML imports `qs.Commons` and `qs.Ui`; Quickshell resolves those
# from the module directories next to the config it loads.
ln -sfn "$OMARCHY_SHELL_DIR/Commons" "$WORK_DIR/Commons"
ln -sfn "$OMARCHY_SHELL_DIR/Ui" "$WORK_DIR/Ui"

{
  echo 'import QtQuick'
  echo 'import Quickshell'
  echo
  echo 'ShellRoot {'
  echo '  id: root'
  echo '  property var targets: ['
  for entry in "${ENTRY_POINTS[@]}"; do
    echo "    \"file://$REPO_ROOT/$entry\","
  done
  echo '  ]'
  echo '  Component.onCompleted: {'
  echo '    for (var i = 0; i < root.targets.length; i++) {'
  echo '      var comp = Qt.createComponent(root.targets[i], Component.PreferSynchronous)'
  echo '      if (comp.status === Component.Ready) {'
  echo '        console.log("COMPILE-OK " + root.targets[i])'
  echo '      } else {'
  echo '        console.log("COMPILE-FAIL " + root.targets[i] + " :: " + comp.errorString())'
  echo '      }'
  echo '    }'
  echo '    Qt.quit()'
  echo '  }'
  echo '}'
} > "$WORK_DIR/shell.qml"

OUTPUT="$(timeout 120 quickshell -p "$WORK_DIR" 2>&1 || true)"

# Strip the ANSI colouring quickshell writes to its log lines.
CLEAN="$(printf '%s\n' "$OUTPUT" | sed -e 's/\x1b\[[0-9;]*m//g')"

if ! printf '%s\n' "$CLEAN" | grep -q 'COMPILE-'; then
  echo "SKIP: quickshell produced no compile result (no display session?)"
  printf '%s\n' "$CLEAN" | tail -5
  exit 0
fi

STATUS=0
for entry in "${ENTRY_POINTS[@]}"; do
  if printf '%s\n' "$CLEAN" | grep -q "COMPILE-OK file://$REPO_ROOT/$entry"; then
    echo "OK: $entry"
  else
    detail="$(printf '%s\n' "$CLEAN" | grep "COMPILE-FAIL file://$REPO_ROOT/$entry" | head -1)"
    detail="${detail#*:: }"
    echo "::error::[qml-compile-check] $entry: ${detail:-component did not reach Component.Ready}"
    STATUS=1
  fi
done

if [ "$STATUS" -ne 0 ]; then
  echo "FAIL: qml-compile-check — a QML entry point does not compile; the shell cannot load it"
  exit 1
fi

echo "PASS: qml-compile-check (every QML entry point compiles)"

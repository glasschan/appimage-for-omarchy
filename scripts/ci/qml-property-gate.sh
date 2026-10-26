#!/bin/bash
# qml-property-gate.sh — permanent CI gate for QML property assignments.
#
# A QML assignment to a property the target type does not declare is a
# COMPILE error, not a warning: the whole component fails to load. For a
# plugin panel that is silent and fatal at once — the omarchy shell's Loader
# error handler calls `errorString()` as a function (shell.qml), so the real
# load error is swallowed and the only symptom is a bar button that does
# nothing when pressed.
#
# That is exactly how the panel stayed dead from v0.6.3 to v0.6.8: Panel.qml
# assigned `foreground:` / `accent:` to SafeTextField, which never declared
# them, so Panel.qml never compiled.
#
# This gate is the static half of the fix: every property the plugin assigns
# to one of its OWN components (SafeTextField.qml, ThemeIcon.qml) must be
# declared by that component (or inherited from its base type). The runtime
# half is scripts/ci/qml-compile-check.sh, which actually compiles the QML
# where Quickshell is available.
#
# Dependency-free by design (PRD §2): bash + python3 stdlib only.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$REPO_ROOT"

FAILED=""

if python3 - <<'PY'
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
QML = sorted(ROOT.glob('*.qml'))

errors = []


def err(rule, path, lineno, msg):
    rel = path.relative_to(ROOT)
    where = f'{rel}:{lineno}' if lineno else str(rel)
    errors.append(where)
    print(f'::error::[{rule}] {where}: {msg}')


def strip_comments_and_strings(text):
    """Blank out // and /* */ comments and string literal contents while
    preserving newlines, so line numbers stay true and a brace inside a
    string cannot confuse the block matcher."""
    out = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                out.append(' ')
                i += 1
            continue
        if c == '/' and i + 1 < n and text[i + 1] == '*':
            while i < n and not (text[i] == '*' and i + 1 < n and text[i + 1] == '/'):
                out.append('\n' if text[i] == '\n' else ' ')
                i += 1
            out.append('  ')
            i += 2
            continue
        if c in '"\'':
            quote = c
            out.append(' ')
            i += 1
            while i < n and text[i] != quote:
                if text[i] == '\\':
                    out.append(' ')
                    i += 1
                    if i < n:
                        out.append('\n' if text[i] == '\n' else ' ')
                        i += 1
                    continue
                out.append('\n' if text[i] == '\n' else ' ')
                i += 1
            out.append(' ')
            i += 1
            continue
        out.append(c)
        i += 1
    return ''.join(out)


# Members every QML object has (Item + QObject basics), plus the ones a
# TextField inherits from TextInput / Qt Quick Controls.
ITEM_MEMBERS = {
    'id', 'objectName', 'parent', 'children', 'visible', 'opacity', 'enabled',
    'width', 'height', 'implicitWidth', 'implicitHeight', 'anchors', 'margins',
    'anchors.fill', 'anchors.centerIn', 'transform', 'scale', 'rotation',
    'opacity', 'clip', 'antialiasing', 'layer', 'z', 'focus', 'activeFocus',
    'activeFocusOnTab', 'focusPolicy', 'Component', 'state', 'states',
    'transitions', 'font', 'padding', 'leftPadding', 'rightPadding',
    'topPadding', 'bottomPadding', 'background', 'contentItem', 'Keys',
    'property', 'signal', 'readonly', 'default', 'required', 'function',
    'Connections', 'Binding', 'Number', 'Qt', 'Math', 'JSON', 'Date', 'Color',
}

TEXTFIELD_MEMBERS = ITEM_MEMBERS | {
    'text', 'placeholderText', 'placeholderTextColor', 'color',
    'selectionColor', 'selectedTextColor', 'selection', 'selectedText',
    'cursorPosition', 'cursorRectangle', 'readOnly', 'echoMode', 'validator',
    'inputMask', 'inputMethodHints', 'maximumLength', 'selectByMouse',
    'selectByKeyboard', 'tabStopBehavior', 'renderType', 'horizontalAlignment',
    'verticalAlignment', 'acceptableInput', 'inputMethodComposing',
    'preeditText', 'preeditTextFormat', 'inputMethodQuery', 'passwordCharacter',
    'insertBehavior', 'selectedTextFormat', 'horizontalAlignment',
    'verticalAlignment', 'wrapMode', 'maskFormat', 'maskData',
    'accepted', 'editingFinished', 'textChanged', 'textEdited',
    'activeFocusChanged', 'hoveredChanged', 'cursorPositionChanged',
    'selectionChanged', 'acceptedChanged', 'preeditTextChanged',
    'onAccepted', 'onEditingFinished', 'onTextChanged', 'onTextEdited',
    'onActiveFocusChanged', 'onHoveredChanged', 'onCursorPositionChanged',
    'onSelectionChanged', 'onPressed', 'onReleased', 'onVisibleChanged',
    'onEnabledChanged', 'onWidthChanged', 'onHeightChanged',
}

# Members the shell's BorderSurface inherits on top of the Item basics —
# the base type of the panel's card components (ticket #55 extraction).
BORDER_MEMBERS = ITEM_MEMBERS | {
    'color', 'radius', 'borderSpec', 'padding',
}

# Column members the AppRow delegate relies on.
COLUMN_MEMBERS = ITEM_MEMBERS | {
    'spacing',
}

# The plugin's own component types: name -> (file, base type members).
LOCAL_TYPES = {
    'SafeTextField': ('SafeTextField.qml', TEXTFIELD_MEMBERS),
    'ThemeIcon': ('ThemeIcon.qml', ITEM_MEMBERS),
    'PythonDependencyCard': ('PythonDependencyCard.qml', BORDER_MEMBERS),
    'ForceQuitCard': ('ForceQuitCard.qml', BORDER_MEMBERS),
    'UpdateAllCard': ('UpdateAllCard.qml', BORDER_MEMBERS),
}

DECL_RE = re.compile(r'^\s*(?:required\s+|default\s+)*(?:readonly\s+)?'
                      r'(property|signal)\b')
PROP_RE = re.compile(r'^\s*(?:readonly\s+)?property\s+\w+\s+(\w+)')
SIGNAL_RE = re.compile(r'^\s*signal\s+(\w+)')
ASSIGN_RE = re.compile(r'^(\s*)([A-Za-z_]\w*)\s*:')
HANDLER_RE = re.compile(r'^(\s*)on([A-Z]\w*)\s*:')


def declared_members(path):
    text = path.read_text(encoding='utf-8')
    props, signals = set(), set()
    for line in text.splitlines():
        m = PROP_RE.match(line)
        if m:
            props.add(m.group(1))
        m = SIGNAL_RE.match(line)
        if m:
            signals.add(m.group(1))
    return props, signals


def block_ranges(text, type_name):
    """Yield (start, end) offsets of every `TypeName { ... }` block."""
    for m in re.finditer(r'\b' + re.escape(type_name) + r'\s*\{', text):
        depth = 0
        i = m.end() - 1
        while i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    yield m.start(), i
                    break
            i += 1


def depth_of(text, offset):
    """Brace depth at `offset`, counting from the start of the file."""
    depth = 0
    for c in text[:offset]:
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
    return depth


for type_name, (file_name, inherited) in LOCAL_TYPES.items():
    component = ROOT / file_name
    if not component.is_file():
        err('qml-property-gate', ROOT, None,
            f'local component {file_name} is missing — the gate cannot '
            'verify assignments to it')
        continue
    own_props, own_signals = declared_members(component)
    known = own_props | own_signals | inherited

    for path in QML:
        if path.name == file_name:
            continue  # the component's own body may assign to itself
        text = strip_comments_and_strings(path.read_text(encoding='utf-8'))
        for start, end in block_ranges(text, type_name):
            base_depth = depth_of(text, start)
            body = text[start:end]
            offset = start
            for raw_line in body.split('\n'):
                line_no = text.count('\n', 0, offset) + 1
                offset += len(raw_line) + 1
                if not raw_line.strip():
                    continue
                # only assignments at the block's own level
                if depth_of(text, offset - len(raw_line) - 1) != base_depth + 1:
                    continue
                if DECL_RE.match(raw_line):
                    continue  # a declaration, not an assignment
                m = HANDLER_RE.match(raw_line)
                if m:
                    # `onFoo` binds to signal `foo` or to the change
                    # handler of property `foo` (signal `fooChanged`).
                    raw_name = m.group(2)
                    name = raw_name[0].lower() + raw_name[1:]
                    if name not in known and f'{name}Changed' not in known:
                        err('qml-property-gate', path, line_no,
                            f'{type_name} has no signal/property "{name}" — '
                            'QML rejects the assignment at compile time and '
                            'the whole panel fails to load')
                    continue
                m = ASSIGN_RE.match(raw_line)
                if not m:
                    continue
                name = m.group(2)
                if name in ('property', 'signal', 'readonly', 'default',
                            'required', 'function', 'id'):
                    continue
                if name not in known:
                    err('qml-property-gate', path, line_no,
                        f'{type_name} has no property "{name}" — QML rejects '
                        'the assignment at compile time and the whole panel '
                        'fails to load (declare it in '
                        f'{file_name} or stop assigning it)')

if not QML:
    err('qml-property-gate', ROOT, None, 'no root QML files found')

if errors:
    print(f'\nqml-property-gate: FAIL ({len(errors)} error(s))')
    sys.exit(1)
print('qml-property-gate: PASS (local QML components expose every '
      'property the plugin assigns to them)')
PY
then
  :
else
  FAILED="qml-property-gate"
fi

if [ -n "$FAILED" ]; then
  printf 'FAIL: %s\n' "$FAILED"
  exit 1
fi
echo "PASS: qml-property-gate"

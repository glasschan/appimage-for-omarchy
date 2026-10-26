#!/bin/bash
# security-gates.sh — permanent CI gates for the security-review findings.
#
# The marketplace reviewer found the same classes of issues twice; every
# class below is now an automated invariant. Dependency-free by design
# (PRD §2): pure bash + python3 stdlib — no ruff/bandit/external linters.
#
# Gates in this script:
#   1  backend invariant greps (os.environ / urllib egress / atomic copy /
#      bounded subprocess / QML collector wiring / shell-source constancy,
#      python side: launch-argv constancy on the launch path /
#      no editable native text input — round-3 finding, marketplace #11415)
# The former gate 2 (shellcheck on the bundled install/uninstall scripts)
# and gate 3 (install.sh deploy smoke test) died with those scripts: the
# marketplace requires plugins to install via `omarchy plugin add`, which
# leaves nothing repo-side to shellcheck or smoke-test. Gate 4 (security-
# regression coverage manifest) is backend/tests/test_security_coverage.py
# and therefore runs inside the normal `unittest discover` suite.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$REPO_ROOT"

FAILED_GATES=""

fail() {
  printf 'FAIL: %s\n' "$*"
  FAILED_GATES="${FAILED_GATES:+$FAILED_GATES, }$*"
}

# --- Gate 1: backend invariant greps. ---------------------------------------
# The scans are RECURSIVE (rglob): the CLI verbs live in
# backend/omarchy_appimage/commands/ since ticket #56, and a subpackage
# must never be a scan blind spot.
# One scan per review finding; each violation prints a ::error:: line so the
# GitHub run annotation points at the guilty rule.
if python3 - <<'PY'
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
PKG = ROOT / 'backend' / 'omarchy_appimage'
QML = sorted(ROOT.glob('*.qml'))

errors = []


def err(rule, path, lineno, msg):
    rel = path.relative_to(ROOT)
    where = f'{rel}:{lineno}' if lineno else str(rel)
    errors.append(where)
    print(f'::error::[{rule}] {where}: {msg}')


def read_lines(path):
    return path.read_text(encoding='utf-8').splitlines()


# Rule 1: os.environ / os.getenv only in constants.py. Environment
# variables must never gate a trust boundary (round-2 finding #1: the
# ALLOW_LOCAL kill switch). Matches any `.environ` access AND os.getenv,
# so an aliased import or the idiomatic getenv cannot evade the scan.
for path in sorted(PKG.rglob('*.py')):
    if path.name == 'constants.py':
        continue
    for i, line in enumerate(read_lines(path), 1):
        if re.search(r'\benviron(?:b)?\b|\bgetenv\s*\(', line):
            err('env-boundary', path, i,
                'os.environ/os.getenv outside constants.py — env must '
                'never gate a trust boundary')

# Rule 2: no direct urllib egress outside net.py. All requests must flow
# through the hardened opener (redirect/SSRF/deadline enforcement).
for path in sorted(PKG.rglob('*.py')):
    if path.name == 'net.py':
        continue
    for i, line in enumerate(read_lines(path), 1):
        if re.search(r'\burlopen\s*\(', line) or \
                'urllib.request.urlopen' in line or \
                re.search(r'\bfrom\s+urllib\.request\s+import\b', line):
            err('egress', path, i,
                'direct urllib egress outside net.py — go through the '
                'hardened net.py opener')

# Rule 3: no non-atomic copies in provider.py (round-1 finding #4): only
# _atomic_install may write payloads (fd write-through + fsync).
provider = PKG / 'provider.py'
for i, line in enumerate(read_lines(provider), 1):
    if re.search(r'\bshutil\.copy(?:file|2|tree)?\s*\(', line):
        err('atomic-install', provider, i,
            'non-atomic shutil copy in provider.py — use _atomic_install')

# Rule 4: no raw subprocess in extractor.py (round-1 finding #5): all
# external tools go through the bounded utils.run_command (pipe-draining,
# timeout-reaped).
extractor = PKG / 'extractor.py'
for i, line in enumerate(read_lines(extractor), 1):
    if 'subprocess.' in line:
        err('bounded-subprocess', extractor, i,
            'raw subprocess use in extractor.py — use utils.run_command')

# Rule 5: every raw subprocess.run elsewhere carries timeout= (utils.py owns
# the bounded runner and is exempt). squashfs.py's zstd probe is the case
# this pins: an unbounded wait must never hang the backend. `from subprocess
# import` and aliased imports are flagged too — a bare `run(...)` alias must not
# escape the scan.
for path in sorted(PKG.rglob('*.py')):
    if path.name == 'utils.py':
        continue
    lines = read_lines(path)
    for i, line in enumerate(lines):
        if re.search(r'\bfrom\s+subprocess\s+import\b', line):
            err('bounded-subprocess', path, i + 1,
                'from subprocess import ... — call subprocess.run (with '
                'timeout=) or use utils.run_command')
        if re.search(r'\bimport\s+subprocess\s+as\b', line):
            err('bounded-subprocess', path, i + 1,
                'aliased subprocess import — call subprocess.run (with '
                'timeout=) or use utils.run_command')
        if not re.search(r'\bsubprocess\.run\s*\(', line):
            continue
        window = '\n'.join(lines[i:i + 7])  # call line + following 6
        if not re.search(r'\btimeout\s*=', window):
            err('bounded-subprocess', path, i + 1,
                'subprocess.run without timeout= within 6 lines')

# Rule 6: QML collector wiring (round-2 finding #3). Every StdioCollector
# block must keep its onRead: producer-side bound — brace-aware scan, enough
# for these flat blocks. Comments (// and /* */) and string literals are
# stripped first so braces or onRead: inside them cannot fool the scan;
# newlines are preserved so reported line numbers stay the original ones.
def strip_comments_and_strings(text, keep_strings=False):
    out = []
    i, n = 0, len(text)
    state = 'code'
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ''
        if state == 'code':
            if c == '/' and nxt == '/':
                state, i = 'line', i + 2
            elif c == '/' and nxt == '*':
                state, i = 'block', i + 2
                out.append('  ')
            elif c in '"\'`':
                state, i = {'"': 'dq', "'": 'sq', '`': 'bt'}[c], i + 1
                out.append(' ' if not keep_strings else c)
            else:
                out.append(c)
                i += 1
        elif state == 'line':
            if c == '\n':
                state = 'code'
            out.append('\n' if c == '\n' else ' ')
            i += 1
        elif state == 'block':
            if c == '*' and nxt == '/':
                state, i = 'code', i + 2
                out.append('  ')
            else:
                out.append('\n' if c == '\n' else ' ')
                i += 1
        else:  # dq / sq / bt string literal
            if c == '\\' and i + 1 < n:
                out.append('  ' if not keep_strings else c + nxt)
                i += 2
            elif c == {'dq': '"', 'sq': "'", 'bt': '`'}[state]:
                state = 'code'
                out.append(' ' if not keep_strings else c)
                i += 1
            else:
                out.append('\n' if c == '\n' else (' ' if not keep_strings else c))
                i += 1
    return ''.join(out)


for path in QML:
    text = strip_comments_and_strings(path.read_text(encoding='utf-8'))
    for m in re.finditer(r'\bStdioCollector\b', text):
        open_brace = text.find('{', m.end())
        if open_brace == -1:
            err('qml-collector', path, text.count('\n', 0, m.start()) + 1,
                'StdioCollector without a brace block')
            continue
        depth, i = 0, open_brace
        while i < len(text):
            if text[i] == '{':
                depth += 1
            elif text[i] == '}':
                depth -= 1
                if depth == 0:
                    break
            i += 1
        lineno = text.count('\n', 0, m.start()) + 1
        if i >= len(text):
            err('qml-collector', path, lineno,
                'unbalanced StdioCollector block')
        elif 'onRead:' not in text[open_brace:i + 1]:
            err('qml-collector', path, lineno,
                'StdioCollector block without onRead: — the producer-side '
                'stream bound must stay wired')

# Rule 7: shell-source constancy (round-3 finding). The functions below
# build the shell SOURCE STRING handed to `omarchy launch floating terminal
# with presentation`, and that wrapper turns the string into bash source
# (presentation_script="…; $cmd; …" → bash -c). The source must therefore
# be CONSTANT: environment-derived values may only ever appear as shell
# expansions inside double quotes ("${XDG_RUNTIME_DIR:?}…") or as argv/env
# data (sh -c script sh "$1" …), never interpolated into the literal. Any
# of these tokens inside a builder body means runtime values are being
# spliced into shell source: runtimeDir / Quickshell.env (environment
# reads), pythonMarkerPaths / marker. (paths derived from a runtime dir).
SHELL_SOURCE_BUILDERS = ['pythonInstallCommand', 'pythonInstallPrepScript']
BUILDER_TOKENS = ['runtimeDir', 'Quickshell.env', 'pythonMarkerPaths', 'marker.']
builder_res = [re.compile(r'\bfunction\s+' + re.escape(name) + r'\s*\(')
               for name in SHELL_SOURCE_BUILDERS]
for path in sorted(ROOT.glob('lib/*.js')) + QML:
    # strip_comments_and_strings() (defined under rule 6) blanks // and
    # /* */ comments AND the contents of string literals while preserving
    # newlines — so line numbers stay true, a "//" inside a string literal
    # cannot truncate the scan, and builder doc comments that merely
    # discuss runtimeDir stay clean. Only live code can trip the rule.
    text = strip_comments_and_strings(path.read_text(encoding='utf-8'))
    for name, bres in zip(SHELL_SOURCE_BUILDERS, builder_res):
        for m in bres.finditer(text):
            lineno = text.count('\n', 0, m.start()) + 1
            open_brace = text.find('{', m.end())
            if open_brace == -1:
                err('shell-source-constancy', path, lineno,
                    f'{name}() without a brace block')
                continue
            depth, i = 0, open_brace
            while i < len(text):
                if text[i] == '{':
                    depth += 1
                elif text[i] == '}':
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            if i >= len(text):
                err('shell-source-constancy', path, lineno,
                    f'{name}() has no closing brace — unbalanced builder '
                    'body, the rule cannot bound its scan')
                continue
            # Model.js top-level builders close at column 0; anything else
            # is a layout change worth a loud look before trusting the rule
            # (the token scan below stays correctly bounded regardless).
            line_start = text.rfind('\n', 0, i) + 1
            line_end = text.find('\n', i)
            if line_end == -1:
                line_end = len(text)
            if text[line_start:line_end] != '}':
                err('shell-source-constancy', path,
                    text.count('\n', 0, i) + 1,
                    f'{name}() closes at a non-column-0 brace — top-level '
                    'builders close at column 0; re-check the body bounds')
            for token in BUILDER_TOKENS:
                for tm in re.finditer(re.escape(token), text[m.start():i + 1]):
                    err('shell-source-constancy', path,
                        text.count('\n', 0, m.start() + tm.start()) + 1,
                        f'{name}() interpolates "{token}" into shell '
                        'source — the presented string must be constant; '
                        'environment values only as expansion/argv data')

# Rule 7b: launch-argv constancy (tickets #32-#35; the python side of
# rule 7's shell-source-constancy). The launch path — launch.py (the ONE
# launch-command builder), exec_scale.py (the mechanism) and
# commands/exec_scale.py (the verb) — builds its commands as ARGV lists
# end to end and hands the image to `os.execve`; nothing may ever pass
# through a shell again. Forbidden on that path:
#   - shell execution vectors: `shell=True`, `os.system`/`os.popen`,
#     a `bash -c` / `sh -c` argv pair (any bang-shell spelling, with or
#     without a /bin prefix) — each is how a shell-interpolated launch
#     command would be reintroduced;
#   - any `os.exec*` other than `os.execve(` — only the env-map handover
#     is the contract; a `execv`/`execl`/`execvp` silently drops the
#     computed environment (GDK_SCALE, the DESKTOPINTEGRATION stamp);
#   - a `DESKTOPINTEGRATION` string literal outside launch.py — the
#     marker is launch.py's `DESKTOPINTEGRATION_ENV` constant and every
#     other module must import it, so the entry writer, the panel argv
#     and the verb's stamp cannot drift into different spellings.
LAUNCH_PATH_FILES = [PKG / 'launch.py', PKG / 'exec_scale.py',
                     PKG / 'commands' / 'exec_scale.py']
LAUNCH_SHELL_RES = [
    (re.compile(r'\bshell\s*=\s*True\b'),
     'shell=True on the launch path — commands are argv lists, never '
     'shell source'),
    (re.compile(r'\bos\.(?:system|popen)\s*\('),
     'os.system/os.popen on the launch path — commands are argv lists, '
     'never shell source'),
    (re.compile(r'["\'](?:/(?:usr/)?bin/)?(?:ba|z|da|k)?sh["\']'
                r'\s*,\s*["\']-c["\']'),
     'a `sh -c` argv pair on the launch path — commands are argv lists, '
     'never shell source'),
]
for path in LAUNCH_PATH_FILES:
    for i, line in enumerate(read_lines(path), 1):
        for res, msg in LAUNCH_SHELL_RES:
            if res.search(line):
                err('launch-argv-constancy', path, i, msg)
        if re.search(r'\bos\.exec\w*\s*\(', line) \
                and not re.search(r'\bos\.execve\s*\(', line):
            err('launch-argv-constancy', path, i,
                'os.exec* other than os.execve — only execve hands over '
                'the computed environment (GDK_SCALE, the '
                'DESKTOPINTEGRATION stamp)')
        if path.name != 'launch.py' \
                and re.search(r'["\']DESKTOPINTEGRATION', line):
            err('launch-argv-constancy', path, i,
                'DESKTOPINTEGRATION string literal outside launch.py — '
                'import launch.DESKTOPINTEGRATION_ENV; the marker must '
                'not drift between the entry writer, the panel argv and '
                'the verb stamp')

# Rule 8: no editable native text input (marketplace #11415, round 3).
# Qt 6.12 automatically retrieves foreign clipboard text on every clipboard
# change for any QQuickTextInput with readOnly: false — no paste action, no
# aggregate byte limit, no overall deadline — so a clipboard provider can
# stall or exhaust the shared shell while an editable input is alive, even
# inside an open card. readOnly is the only switch for that hook
# (omarchy#15083). Plugin-wide contract:
#   - `TextField {` / `TextInput {` / `TextEdit {` instantiations exist
#     ONLY as SafeTextField.qml's own readOnly root (text entry goes
#     through SafeTextField everywhere else);
#   - that root stays permanently `readOnly: true`;
#   - its clipboard helper stays bounded (timeout + text/plain only);
#   - every NumberField instance flips the shell SpinBox's inner TextInput
#     readOnly via `field.editable = false`.
safe_root_seen = False
for path in QML:
    text = strip_comments_and_strings(path.read_text(encoding='utf-8'))
    is_safe_field = path.name == 'SafeTextField.qml'
    for m in re.finditer(r'\b(TextField|TextInput|TextEdit)\s*\{', text):
        if is_safe_field and m.group(1) == 'TextField' and not safe_root_seen:
            safe_root_seen = True
            continue  # SafeTextField's own readOnly root declaration
        err('qml-no-editable-input', path, text.count('\n', 0, m.start()) + 1,
            f'native {m.group(1)} instantiation — text entry must go '
            'through SafeTextField (Qt 6.12 clipboard hook arms on every '
            'readOnly: false QQuickTextInput, marketplace #11415 round 3)')
    if is_safe_field:
        if not re.search(r'^\s*readOnly:\s*true\s*$', text, re.M):
            err('qml-no-editable-input', path, None,
                'SafeTextField root lost its permanent `readOnly: true` — '
                'that flag is the only switch for the Qt 6.12 clipboard '
                'hook and must never be bound or cleared')
        # argv literals live inside strings, so this check needs the
        # comments-stripped, strings-preserved text.
        live = strip_comments_and_strings(
            path.read_text(encoding='utf-8'), keep_strings=True)
        if not re.search(r'"/usr/bin/timeout"', live) \
                or not re.search(r'"/usr/bin/wl-paste", "--no-newline", '
                                 '"--type", "text/plain"', live):
            err('qml-no-editable-input', path, None,
                'SafeTextField clipboard helper must stay bounded and '
                'fail-closed: /usr/bin/timeout around /usr/bin/wl-paste '
                '--no-newline --type text/plain')
if not QML:
    err('qml-no-editable-input', ROOT, None, 'no root QML files found')
number_fields = len(re.findall(r'\bNumberField\s*\{', '\n'.join(
    strip_comments_and_strings(p.read_text(encoding='utf-8')) for p in QML)))
editable_offs = sum(len(re.findall(r'field\.editable\s*=\s*false',
                                   strip_comments_and_strings(
                                       p.read_text(encoding='utf-8'))))
                    for p in QML)
if number_fields != editable_offs:
    err('qml-no-editable-input', ROOT, None,
        f'{number_fields} NumberField instance(s) but {editable_offs} '
        '`field.editable = false` wire(s) — every NumberField must flip '
        'the shell SpinBox contentItem to readOnly')

if errors:
    print(f'gate 1: {len(errors)} backend invariant violation(s)')
    sys.exit(1)
print('gate 1: backend invariants OK '
      '(env boundary, egress, atomic install, bounded subprocess, '
      'QML collectors, shell-source constancy, launch-argv constancy, '
      'no editable native input)')
PY
then
  printf 'PASS: gate 1 — backend invariant greps\n'
else
  fail 'gate 1 (backend invariant greps)'
fi

# --- Summary. ----------------------------------------------------------------
if [[ -n "$FAILED_GATES" ]]; then
  printf 'security-gates: FAILED gates: %s\n' "$FAILED_GATES" >&2
  exit 1
fi
printf 'security-gates: PASS (backend invariant greps)\n'

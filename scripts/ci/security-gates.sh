#!/bin/bash
# security-gates.sh — permanent CI gates for the security-review findings.
#
# The marketplace reviewer found the same classes of issues twice; every
# class below is now an automated invariant. Dependency-free by design
# (PRD §2): pure bash + python3 stdlib — no ruff/bandit/external linters.
#
# Gates in this script:
#   1  backend invariant greps (os.environ / urllib egress / atomic copy /
#      bounded subprocess / QML collector wiring)
#   2  shellcheck on install.sh + uninstall.sh (hard fail under CI, which
#      always has shellcheck; warn-only on a local machine without it)
# Gate 3 (install.sh deploy smoke test) lives in install-sh-smoke.sh and is
# run right after this script; gate 4 (security-regression coverage
# manifest) is backend/tests/test_security_coverage.py and therefore runs
# inside the normal `unittest discover` suite.
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd -- "$REPO_ROOT"

FAILED_GATES=""

fail() {
  printf 'FAIL: %s\n' "$*"
  FAILED_GATES="${FAILED_GATES:+$FAILED_GATES, }$*"
}

# --- Gate 1: backend invariant greps. ---------------------------------------
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


# Rule 1: os.environ only in constants.py. Environment variables must never
# gate a trust boundary (round-2 finding #1: the ALLOW_LOCAL kill switch).
# Matches any `.environ` access, so an aliased `import os as x` cannot
# evade the scan.
for path in sorted(PKG.glob('*.py')):
    if path.name == 'constants.py':
        continue
    for i, line in enumerate(read_lines(path), 1):
        if re.search(r'\benviron(?:b)?\b', line):
            err('env-boundary', path, i,
                'os.environ outside constants.py — env must never gate a '
                'trust boundary')

# Rule 2: no direct urllib egress outside net.py. All requests must flow
# through the hardened opener (redirect/SSRF/deadline enforcement).
for path in sorted(PKG.glob('*.py')):
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
    if re.search(r'\bshutil\.copy(?:file|2)?\s*\(', line):
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
# this pins: an unbounded wait must never hang the backend.
for path in sorted(PKG.glob('*.py')):
    if path.name == 'utils.py':
        continue
    lines = read_lines(path)
    for i, line in enumerate(lines):
        if not re.search(r'\bsubprocess\.run\s*\(', line):
            continue
        window = '\n'.join(lines[i:i + 5])  # call line + following 4
        if not re.search(r'\btimeout\s*=', window):
            err('bounded-subprocess', path, i + 1,
                'subprocess.run without timeout= within 5 lines')

# Rule 6: QML collector wiring (round-2 finding #3). Every StdioCollector
# block must keep its onRead: producer-side bound — brace-aware scan, enough
# for these flat blocks.
for path in QML:
    text = path.read_text(encoding='utf-8')
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

if errors:
    print(f'gate 1: {len(errors)} backend invariant violation(s)')
    sys.exit(1)
print('gate 1: backend invariants OK '
      '(env boundary, egress, atomic install, bounded subprocess, QML collectors)')
PY
then
  printf 'PASS: gate 1 — backend invariant greps\n'
else
  fail 'gate 1 (backend invariant greps)'
fi

# --- Gate 2: shellcheck. -----------------------------------------------------
# Preinstalled on GitHub ubuntu runners, so a missing binary there is a
# broken runner, not a reason to skip; locally it may genuinely be absent.
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck install.sh uninstall.sh; then
    printf 'PASS: gate 2 — shellcheck install.sh uninstall.sh\n'
  else
    fail 'gate 2 (shellcheck)'
  fi
elif [[ -n "${CI:-}" ]]; then
  fail "gate 2 (shellcheck): shellcheck is missing under CI — it must be " \
    "preinstalled on ubuntu runners; refusing to skip silently"
  exit 1
else
  printf 'WARN: gate 2 — shellcheck not installed locally; gate SKIPPED (CI enforces it on ubuntu-latest)\n'
fi

# --- Summary. ----------------------------------------------------------------
if [[ -n "$FAILED_GATES" ]]; then
  printf 'security-gates: FAILED gates: %s\n' "$FAILED_GATES" >&2
  exit 1
fi
printf 'security-gates: PASS (invariant greps + shellcheck)\n'

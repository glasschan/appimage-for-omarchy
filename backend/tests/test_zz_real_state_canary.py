# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
# test_zz_real_state_canary.py — suite-wide real-state isolation canary.
#
# FakeXDGTestCase sandboxes each test's environment, and
# SandboxIsolationPinTests pins the two path resolvers — but a test (or a
# straggler thread it spawned) that reaches the REAL user state dir through
# any other route still escapes both. The name sorts last so discovery
# imports it after every other test module: the snapshot below is taken
# before the first test of the suite runs, and the comparison happens after
# the last one. Any byte change to the real plugin state files in between
# is a leak, and this module fails — the before/after sha256 check that
# used to be a manual release step (issue #62's P1) becomes a red test.

import hashlib
import json
import os
import unittest

_REAL_DIR = os.path.join(
    os.environ.get('XDG_STATE_HOME',
                   os.path.expanduser('~/.local/state')),
    'io.github.glasschan.appimage')
_WATCHED = ('last-sweep', 'sanitized-icons.json')


def _snapshot():
    snap = {}
    for name in _WATCHED:
        path = os.path.join(_REAL_DIR, name)
        try:
            with open(path, 'rb') as handle:
                snap[name] = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            snap[name] = None          # absent now; must still be absent
    return snap


_AT_IMPORT = _snapshot()


class RealStateIsolationCanaryTests(unittest.TestCase):
    """The real user state files must be byte-identical across the whole
    suite run. A mismatch means some test wrote them (or an external
    process — the live panel's throttled sweep — raced the run; re-run
    once to discriminate: a leak reproduces, a race does not)."""

    def test_no_test_touched_the_real_state_files(self):
        after = _snapshot()
        changed = [name for name in _WATCHED
                   if after[name] != _AT_IMPORT[name]]
        self.assertEqual(
            changed, [],
            'real-state files changed during the suite run: %s\n'
            'before: %s\nafter:  %s'
            % (changed,
               json.dumps(_AT_IMPORT, indent=1),
               json.dumps(after, indent=1)))

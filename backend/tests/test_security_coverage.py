# test_security_coverage.py — gate 4: security-regression coverage manifest.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin.
#
# The marketplace security reviewer found the same classes of issues twice;
# every fixed class must STAY covered by a named regression test. This
# manifest pins the required scenarios to test-name slugs: when a scenario
# lands, its test name must contain the slug, and if a required scenario
# ever loses its test this module fails the whole suite.
#
# Convention: name new regression tests so the scenario slug appears in the
# test name (e.g. test_download_deadline_exceeded_no_partial_file pins the
# `deadline` scenario). Add a scenario here in the same commit as its test.

import re
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parents[1]
PARSE_HARNESS = REPO_ROOT / 'tests-js' / 'parse-harness.mjs'

# Required scenarios. Each row is (description, [alternative slug-sets],
# harness_pool) and is satisfied when SOME single test name (or, for rows
# with harness_pool=True, the tests-js parse harness text) contains ALL
# slugs of ANY alternative slug-set (case-insensitive substring match).
#   one set with two slugs  →  'content_length' + 'garbage' in one name
#   several sets            →  'trickle' OR 'slow_drip' (any one suffices)
REQUIRED_SCENARIOS = [
    ('SSRF: redirect to a private target is rejected',
     [{'redirect', 'private'}], False),
    ('SSRF: redirect downgrade to http is rejected',
     [{'redirect', 'http'}], False),
    ('credentials-in-URL (userinfo) is rejected',
     [{'userinf'}], False),
    ('whole-operation download deadline',
     [{'deadline'}], False),
    ('slow-livelock (trickle / slow drip) body hits the deadline',
     [{'trickle'}, {'slow_drip'}], False),
    ('garbage Content-Length header coercion',
     [{'content_length', 'garbage'}], False),
    ('sha256 digest mismatch fails the download',
     [{'digest', 'mismatch'}], False),
    ('zsync SHA-1 binding is verified',
     [{'zsync', 'sha1'}], False),
    ('fail-closed refusal paths without a digest',
     [{'no_digest'}, {'without_digest'}, {'unverified'}], False),
    ('extraction containment: escaping symlink is unlinked',
     [{'symlink', 'escape'}, {'symlink', 'escaping'}], False),
    ('extraction bombs / listing quotas',
     [{'quota'}], False),
    ('icon path traversal is rejected',
     [{'traversal'}], False),
    ('non-regular destinations (FIFO) are refused',
     [{'fifo'}], False),
    ('QML/backend output-stream bound (overflow)',
     [{'overflow'}], True),
    ('download / metadata byte caps',
     [{'oversize'}, {'over_cap'}, {'byte'}], False),
    ('process-group cleanup (no orphaned children)',
     [{'kill'}, {'orphan'}, {'killpg'}], False),
    ('env kill switch stays dead (no ALLOW_LOCAL re-entry)',
     [{'allow_local'}, {'environ'}, {'env_var'}], False),
]


def _backend_test_names() -> set:
    """All `def test_*` names from backend/tests/test_*.py, lowercased."""
    names = set()
    for path in sorted(TESTS_DIR.glob('test_*.py')):
        text = path.read_text(encoding='utf-8')
        names.update(name.lower() for name in re.findall(r'def (test_\w+)', text))
    return names


def _harness_text() -> str:
    return PARSE_HARNESS.read_text(encoding='utf-8').lower()


def _covers(slug_set: set, names: set, harness) -> bool:
    """A row is covered when ONE single test name contains all its slugs
    (or, for harness-pooled rows, the harness text contains them all)."""
    if any(all(slug in name for slug in slug_set) for name in names):
        return True
    return harness is not None and all(slug in harness for slug in slug_set)


class SecurityRegressionCoverageTests(unittest.TestCase):
    def test_required_scenarios_are_covered(self):
        names = _backend_test_names()
        harness_text = _harness_text()

        # Sanity: the scan must actually have found the suite, or every
        # row below would fail for the wrong reason.
        self.assertGreater(len(names), 100,
                           'no test names found under backend/tests — '
                           'the manifest scan is broken')

        satisfied, missing = [], []
        for description, slug_sets, use_harness in REQUIRED_SCENARIOS:
            # The stream bound lives in the JS frontend too; for that row
            # the parse harness text joins the coverage pool.
            harness = harness_text if use_harness else None
            if any(_covers(slug_set, names, harness)
                   for slug_set in slug_sets):
                satisfied.append(description)
            else:
                missing.append((description, slug_sets))

        if missing:
            lines = ['security-regression coverage manifest: missing rows:']
            for description, slug_sets in missing:
                alternatives = ' or '.join(
                    '+'.join(sorted(slug_set)) for slug_set in slug_sets)
                lines.append(f'  - {description}  (needs a test name '
                             f'containing: {alternatives})')
            lines.append('satisfied rows: %d/%d' %
                         (len(satisfied), len(REQUIRED_SCENARIOS)))
            lines.append('Add the missing regression test (name it with the '
                         'slug) — do not delete this manifest row.')
            self.fail('\n'.join(lines))


if __name__ == '__main__':
    unittest.main()

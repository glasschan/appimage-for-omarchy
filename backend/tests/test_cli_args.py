# test_cli_args.py — ticket #44: every verb rejects unknown options.
#
# Test suite written for this plugin. Released under the GNU GPL-3.0 — see
# the repo's LICENSE for terms.
#
# One case per verb: a token starting with '-' that the verb does not
# document is a usage error (message + usage on stderr, exit 2, and the
# JSON error document in --json mode) instead of being silently ignored.
# --preview-integrate keeps its own validation and its own suite.

import json
import unittest

from helpers import FakeXDGTestCase

from omarchy_appimage import cli, constants


class UnknownOptionTests(FakeXDGTestCase):
    """Every verb goes through main.py, so the usage-error path printed
    here is the one the QML layer and scripts actually see."""

    def _assert_rejected(self, verb: str):
        result = self.run_cli(f'--{verb}', '--bogus')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown option: '--bogus'", result.stderr)
        self.assertIn('Usage:', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_integrate_rejects_unknown_option(self):
        self._assert_rejected('integrate')

    def test_list_installed_rejects_unknown_option(self):
        self._assert_rejected('list-installed')

    def test_remove_rejects_unknown_option(self):
        self._assert_rejected('remove')

    def test_list_updates_rejects_unknown_option(self):
        self._assert_rejected('list-updates')

    def test_update_rejects_unknown_option(self):
        self._assert_rejected('update')

    def test_set_update_source_rejects_unknown_option(self):
        self._assert_rejected('set-update-source')

    def test_get_update_source_rejects_unknown_option(self):
        self._assert_rejected('get-update-source')

    def test_list_update_managers_rejects_unknown_option(self):
        self._assert_rejected('list-update-managers')

    def test_fetch_updates_rejects_unknown_option(self):
        self._assert_rejected('fetch-updates')

    def test_pick_file_rejects_unknown_option(self):
        self._assert_rejected('pick-file')

    def test_settings_rejects_unknown_option(self):
        self._assert_rejected('settings')

    def test_set_setting_rejects_unknown_option(self):
        self._assert_rejected('set-setting')

    def test_integrate_rejects_the_undocumented_keep_both(self):
        # --keep-both belongs to --update; --integrate never documented it.
        result = self.run_cli('--integrate', '/dev/null', '--keep-both',
                              '--yes')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("unknown option: '--keep-both'", result.stderr)

    def test_json_mode_emits_the_error_document(self):
        result = self.run_cli('--list-installed', '--bogus', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Usage:', result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'error': "unknown option: '--bogus'",
        })

    def test_documented_options_still_work(self):
        result = self.run_cli('--list-installed', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['schema_version'],
                         constants.JSON_SCHEMA_VERSION)
        self.assertIn('installed', payload)


if __name__ == '__main__':
    unittest.main()

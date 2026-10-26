# test_cli_settings.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import json
import os
from pathlib import Path

from helpers import FakeXDGTestCase


class SettingsCliTests(FakeXDGTestCase):
    def test_settings_json_shape(self):
        payload = json.loads(self.run_cli('--settings', '--json').stdout)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(set(payload['settings']), {
            'appimages_default_folder',
            'manage_files_outside_default_folder',
            'move_appimage_on_integration',
            'update_check_enabled',
            'update_check_interval_minutes',
            'update_check_delay_minutes'})

    def test_settings_plain_text(self):
        result = self.run_cli('--settings')
        self.assertEqual(result.returncode, 0)
        self.assertIn('appimages_default_folder: ~/AppImages', result.stdout)

    def test_set_setting_roundtrip(self):
        result = self.run_cli('--set-setting',
                              'update_check_interval_minutes=30', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'set')
        self.assertEqual(payload['settings']['update_check_interval_minutes'],
                         30)

        # persisted for the next invocation
        payload = json.loads(self.run_cli('--settings', '--json').stdout)
        self.assertEqual(payload['settings']['update_check_interval_minutes'],
                         30)

        # every value shape: bools, the interval floor, zero delay, folder
        for assignment, expected in [
                ('update_check_enabled=false', False),
                ('manage_files_outside_default_folder=off', False),
                ('move_appimage_on_integration=on', True),
                ('update_check_interval_minutes=15', 15),
                ('update_check_delay_minutes=0', 0),
                ('appimages_default_folder=~/Apps2', '~/Apps2')]:
            result = self.run_cli('--set-setting', assignment, '--json')
            self.assertEqual(result.returncode, 0, assignment)
            key, value = assignment.split('=', 1)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['settings'][key], expected, assignment)

    def test_set_setting_interval_below_floor_exit_2(self):
        result = self.run_cli('--set-setting',
                              'update_check_interval_minutes=14', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['result'], 'error')

    def test_set_setting_interval_not_an_int_exit_2(self):
        result = self.run_cli('--set-setting',
                              'update_check_interval_minutes=soon', '--json')
        self.assertEqual(result.returncode, 2)

    def test_set_setting_negative_delay_exit_2(self):
        result = self.run_cli('--set-setting',
                              'update_check_delay_minutes=-1', '--json')
        self.assertEqual(result.returncode, 2)

    def test_set_setting_bad_folder_exit_2(self):
        result = self.run_cli('--set-setting',
                              'appimages_default_folder=relative/path',
                              '--json')
        self.assertEqual(result.returncode, 2)

    def test_set_setting_unknown_key_exit_2(self):
        result = self.run_cli('--set-setting', 'bogus=1', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('bogus', json.loads(result.stdout)['error'])

    def test_set_setting_bad_bool_exit_2(self):
        result = self.run_cli('--set-setting', 'update_check_enabled=maybe',
                              '--json')
        self.assertEqual(result.returncode, 2)

    def test_list_update_managers_json(self):
        payload = json.loads(
            self.run_cli('--list-update-managers', '--json').stdout)
        self.assertEqual(payload['schema_version'], 1)
        names = [m['name'] for m in payload['managers']]
        self.assertEqual(names, ['StaticFileUpdater', 'GithubUpdater',
                                 'GitlabUpdater', 'CodebergUpdater',
                                 'ForgejoUpdater'])
        github = payload['managers'][1]
        self.assertEqual(github['label'], 'Github')
        self.assertEqual(github['config_keys'],
                         ['repo', 'repo_filename', 'allow_prereleases'])

    def test_list_update_managers_plain(self):
        result = self.run_cli('--list-update-managers')
        self.assertEqual(result.returncode, 0)
        self.assertIn('GithubUpdater', result.stdout)


class SetUpdateSourceCliTests(FakeXDGTestCase):
    def setUp(self):
        super().setUp()
        self.install_fake_app()

    def test_set_and_unset_roundtrip(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'StaticFileUpdater',
                              'url=https://example.com/f.appimage',
                              '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'set')
        self.assertEqual(payload['manager'], 'StaticFileUpdater')
        self.assertEqual(payload['config'],
                         {'url': 'https://example.com/f.appimage'})
        self.assertEqual(payload['app']['manager'], 'StaticFileUpdater')

        # --list-installed picks the source up (config wins)
        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(payload['installed'][0]['manager'],
                         'StaticFileUpdater')
        self.assertFalse(payload['installed'][0]['embedded_source'])

        result = self.run_cli('--set-update-source', 'fakeapp', '--unset',
                              '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['result'], 'unset')
        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertIsNone(payload['installed'][0]['manager'])

    def test_embedded_source_flag_on_list(self):
        # a hand-built .upd_info section routes to the embedded manager
        from helpers import make_elf_with_sections
        appimage = os.path.join(self.managed_dir, 'fakeapp.appimage')
        Path(appimage).write_bytes(make_elf_with_sections(
            {'.upd_info': b'gh-releases-zsync|o|r|latest|f-*.zsync\x00'}))
        os.chmod(appimage, 0o755)

        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(payload['installed'][0]['manager'], 'GithubUpdater')
        self.assertTrue(payload['installed'][0]['embedded_source'])
        self.assertIsNone(payload['installed'][0]['available_version'])
        self.assertIsNone(payload['installed'][0]['download_size'])

    def test_wrong_manager_exit_2(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'Nope', 'url=x', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Nope', json.loads(result.stdout)['error'])

    def test_missing_manager_exit_2(self):
        result = self.run_cli('--set-update-source', 'fakeapp', '--json')
        self.assertEqual(result.returncode, 2)

    def test_missing_keys_exit_2(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'GithubUpdater', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('repo', json.loads(result.stdout)['error'])

    def test_extra_keys_exit_2(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'StaticFileUpdater',
                              'url=https://x/f.appimage', 'extra=1',
                              '--json')
        self.assertEqual(result.returncode, 2)

    def test_bool_words_accepted(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'GithubUpdater',
                              'repo=owner/repo',
                              'repo_filename=Foo-*.appimage',
                              'allow_prereleases=yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIs(payload['config']['allow_prereleases'], True)

    def test_bad_bool_exit_2(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'GithubUpdater',
                              'repo=owner/repo',
                              'repo_filename=Foo-*.appimage',
                              'allow_prereleases=maybe', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('boolean', json.loads(result.stdout)['error'])

    def test_validate_config_error_exit_2(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'StaticFileUpdater',
                              'url=ftp://host/f.appimage', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['result'], 'error')

    def test_unset_unknown_target_exit_1(self):
        result = self.run_cli('--set-update-source', 'ghost', '--unset',
                              '--json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['result'], 'error')

    def test_get_update_source_roundtrip(self):
        result = self.run_cli('--set-update-source', 'fakeapp',
                              '--manager', 'GithubUpdater',
                              'repo=owner/repo',
                              'repo_filename=Foo-*.appimage',
                              'allow_prereleases=yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)

        result = self.run_cli('--get-update-source', 'fakeapp', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'ok')
        self.assertEqual(payload['manager'], 'GithubUpdater')
        # the exact key=value pairs round-trip; bools come back as the
        # on-disk "true"/"false" strings set_app_update_config stores
        self.assertEqual(payload['config'], {
            'repo': 'owner/repo',
            'repo_filename': 'Foo-*.appimage',
            'allow_prereleases': 'true'})
        self.assertEqual(payload['app']['name'], 'Fake App')

    def test_get_update_source_no_source_exit_0(self):
        result = self.run_cli('--get-update-source', 'fakeapp', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'no-source')
        self.assertNotIn('manager', payload)
        self.assertNotIn('config', payload)

    def test_get_update_source_unknown_target_exit_1(self):
        result = self.run_cli('--get-update-source', 'ghost', '--json')
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('ghost', payload['error'])

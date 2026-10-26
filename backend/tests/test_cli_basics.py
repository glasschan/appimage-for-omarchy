# test_cli_basics.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import json
import os

from helpers import FakeXDGTestCase


class UsageTests(FakeXDGTestCase):
    def test_no_args_prints_usage_exit_2(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn('Usage:', result.stdout)

    def test_help_exits_0(self):
        for flag in ('--help', '-h', '--list-installed --help'):
            result = self.run_cli(*flag.split())
            self.assertEqual(result.returncode, 0, flag)
            self.assertIn('Usage:', result.stdout, flag)

    def test_unknown_option_exit_2(self):
        result = self.run_cli('--frobnicate')
        self.assertEqual(result.returncode, 2)

    def test_json_requires_yes_for_integrate(self):
        result = self.run_cli('--integrate', '/dev/null', '--json')
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'error')

    def test_json_requires_yes_for_remove(self):
        result = self.run_cli('--remove', 'whatever', '--json')
        self.assertEqual(result.returncode, 2)


class OfflineCliTests(FakeXDGTestCase):
    """list/remove flows that do not need a real AppImage fixture."""

    def _install_fake_app(self, desktop_name='fakeapp', app_name='Fake App'):
        return self.install_fake_app(desktop_name, app_name)

    def test_list_empty(self):
        result = self.run_cli('--list-installed', '--json')
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        # the additive runtime block (schema_version stays 1): the e2e
        # run probes the real host, so only the vocabulary is pinned
        # here — the state matrix itself is unit-tested against fakes
        self.assertEqual(payload['installed'], [])
        self.assertEqual(set(payload['runtime']), {'fuse2'})
        self.assertIn(payload['runtime']['fuse2'],
                      ('present', 'missing', 'unknown'))

    def test_list_installed_json_schema(self):
        appimage, desktop, _icon = self._install_fake_app()
        result = self.run_cli('--list-installed', '--json')
        payload = json.loads(result.stdout)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(len(payload['installed']), 1)

        app = payload['installed'][0]
        self.assertEqual(set(app), {
            'name', 'path', 'desktop_id', 'current_version',
            'available_version', 'download_size', 'manager',
            'embedded_source', 'running', 'running_pids', 'exec_arguments',
            # additive, ticket #32: the launch command as argv, built by
            # the shared launch builder — the panel spawns this.
            'launch_argv',
            # additive, ticket #35: the argv tail for the backend's
            # `--exec-with-scale` entry point — the scale-aware panel
            # launch shares the launcher's scale mechanism.
            'launch_scale_argv',
            'verification', 'installable',
            'pending_update', 'pending_version', 'pending_installable'})
        self.assertEqual(app['name'], 'Fake App')
        self.assertEqual(app['path'], appimage)
        self.assertEqual(app['desktop_id'], 'fakeapp.desktop')
        self.assertEqual(app['current_version'], '1.0')
        self.assertIsNone(app['available_version'])
        self.assertIsNone(app['download_size'])
        self.assertIsNone(app['manager'])
        self.assertFalse(app['embedded_source'])
        self.assertFalse(app['running'])
        # not running -> nothing is scanned for the count
        self.assertEqual(app['running_pids'], 0)
        # the fake desktop entry carries no Exec arguments
        self.assertEqual(app['exec_arguments'], '')
        # no source -> nothing to prove and no verdict to report
        self.assertEqual(app['verification'], 'none')
        self.assertIsNone(app['installable'])

        # the runtime block rides every successful envelope, with or
        # without installed apps
        self.assertEqual(set(payload['runtime']), {'fuse2'})
        self.assertIn(payload['runtime']['fuse2'],
                      ('present', 'missing', 'unknown'))

    def test_json_stdout_is_pure(self):
        self._install_fake_app()
        result = self.run_cli('--list-installed', '--json')
        # the whole stdout must be a single parseable JSON document
        json.loads(result.stdout)
        self.assertEqual(result.stdout.count('\n'), 1)

    def test_remove_by_desktop_id(self):
        appimage, desktop, icon = self._install_fake_app()
        result = self.run_cli('--remove', 'fakeapp.desktop', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)

        # all three artifacts went to trash
        for trashed in (os.path.basename(appimage),
                        os.path.basename(desktop),
                        os.path.basename(icon)):
            self.assertTrue(os.path.isfile(os.path.join(self.trash_files,
                                                        trashed)), trashed)
        # .trashinfo exists for the appimage
        self.assertTrue(os.path.isfile(os.path.join(
            self.trash_info, 'fakeapp.appimage.trashinfo')))
        # list is empty again
        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(payload['installed'], [])

    def test_remove_by_name_case_insensitive(self):
        self._install_fake_app(app_name='Fake App')
        result = self.run_cli('--remove', 'fake app', '--yes')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_remove_delete_bypasses_trash(self):
        appimage, _desktop, _icon = self._install_fake_app()
        result = self.run_cli('--remove', 'fakeapp', '--yes', '--delete')
        self.assertEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(appimage))
        self.assertFalse(os.path.exists(self.trash_files))

    def test_remove_unknown_exit_1(self):
        result = self.run_cli('--remove', 'ghost', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Error', result.stderr)

    def test_remove_unknown_json_error_document(self):
        result = self.run_cli('--remove', 'ghost', '--yes', '--json')
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('ghost', payload['error'])

    def test_integrate_invalid_file_exit_1(self):
        result = self.run_cli('--integrate', '/etc/hostname', '--yes')
        self.assertEqual(result.returncode, 1)

    def test_integrate_missing_file_exit_1(self):
        result = self.run_cli('--integrate', '/nonexistent.appimage',
                              '--yes')
        self.assertEqual(result.returncode, 1)

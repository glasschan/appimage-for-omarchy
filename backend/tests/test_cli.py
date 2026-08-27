# test_cli.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import json
import os
import stat
import unittest

from helpers import FakeXDGTestCase, download_fixture


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
        """Simulate an already-integrated app: managed AppImage + .desktop
        + icon, exactly the layout install_file produces."""
        os.makedirs(os.path.join(self.managed_dir, '.icons'), exist_ok=True)
        os.makedirs(self.applications_dir, exist_ok=True)

        appimage = os.path.join(self.managed_dir,
                                desktop_name + '.appimage')
        with open(appimage, 'wb') as f:
            f.write(b'\x7fELF\x02\x01\x01\x00\x41\x49\x02padding')
        os.chmod(appimage, 0o755)

        icon = os.path.join(self.managed_dir, '.icons', desktop_name + '.png')
        with open(icon, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n')

        desktop = os.path.join(self.applications_dir,
                               desktop_name + '.desktop')
        with open(desktop, 'w') as f:
            f.write(f'''[Desktop Entry]
Name={app_name}
Exec=env DESKTOPINTEGRATION=1 {appimage}
TryExec={appimage}
Icon={icon}
Type=Application
Terminal=false
X-AppImage-Version=1.0
''')
        return appimage, desktop, icon

    def test_list_empty(self):
        result = self.run_cli('--list-installed', '--json')
        self.assertEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertEqual(payload, {'schema_version': 1, 'installed': []})

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
            'embedded_source', 'running'})
        self.assertEqual(app['name'], 'Fake App')
        self.assertEqual(app['path'], appimage)
        self.assertEqual(app['desktop_id'], 'fakeapp.desktop')
        self.assertEqual(app['current_version'], '1.0')
        self.assertIsNone(app['available_version'])
        self.assertIsNone(app['download_size'])
        self.assertIsNone(app['manager'])
        self.assertFalse(app['embedded_source'])
        self.assertFalse(app['running'])

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


class FixtureCliTests(FakeXDGTestCase):
    """Full integrate flows against a real type-2 AppImage (skipped when
    the fixture cannot be downloaded)."""

    def setUp(self):
        super().setUp()
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        self.fixture = fixture
        self.downloads = os.path.join(self.sandbox, 'downloads')
        os.makedirs(self.downloads, exist_ok=True)

    def _fresh_fixture(self, name='nvim.appimage', extra=b'') -> str:
        import shutil
        path = os.path.join(self.downloads, name)
        shutil.copyfile(self.fixture, path)
        if extra:
            with open(path, 'ab') as f:
                f.write(extra)
        return path

    # ------------------------------------------------------------ integrate

    def test_integrate_full_flow(self):
        src = self._fresh_fixture()
        result = self.run_cli('--integrate', src, '--yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'integrated')

        appimage = payload['app']['path']
        self.assertEqual(appimage,
                         os.path.join(self.managed_dir, 'neovim.appimage'))
        # copy is executable
        mode = stat.S_IMODE(os.stat(appimage).st_mode)
        self.assertEqual(mode, 0o755)
        # original was moved away (GearLever move-appimage-on-integration)
        self.assertFalse(os.path.exists(src))

        # desktop entry exists with the expected rewritten fields
        desktop = os.path.join(self.applications_dir, 'neovim.desktop')
        self.assertTrue(os.path.isfile(desktop))
        with open(desktop) as f:
            content = f.read()
        self.assertIn('Name=Neovim (v0.11.3)', content)
        self.assertIn(f'TryExec={appimage}', content)
        self.assertIn(f'Exec=env DESKTOPINTEGRATION=1 {appimage} %F',
                      content)
        self.assertIn(f'Icon={self.managed_dir}/.icons/neovim.png', content)
        self.assertIn('X-AppImage-Version=v0.11.3', content)

        # icon exists and is a real PNG
        icon = os.path.join(self.managed_dir, '.icons', 'neovim.png')
        with open(icon, 'rb') as f:
            self.assertEqual(f.read(4), b'\x89PNG')

    def test_list_after_integrate(self):
        self._fresh_fixture()
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'nvim.appimage'),
                     '--yes')
        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(len(payload['installed']), 1)
        app = payload['installed'][0]
        self.assertEqual(app['name'], 'Neovim (v0.11.3)')
        self.assertEqual(app['desktop_id'], 'neovim.desktop')
        self.assertEqual(app['current_version'], 'v0.11.3')

    def test_remove_after_integrate_by_name(self):
        self._fresh_fixture()
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'nvim.appimage'), '--yes')
        result = self.run_cli('--remove', 'Neovim (v0.11.3)', '--yes',
                              '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'removed')

        # everything landed in trash
        self.assertTrue(os.path.isfile(os.path.join(
            self.trash_files, 'neovim.appimage')))
        self.assertTrue(os.path.isfile(os.path.join(
            self.trash_files, 'neovim.desktop')))
        self.assertTrue(os.path.isfile(os.path.join(
            self.trash_files, 'neovim.png')))
        self.assertEqual(os.listdir(self.managed_dir),
                         ['.icons'])
        self.assertEqual(
            json.loads(self.run_cli('--list-installed', '--json').stdout)
            ['installed'], [])

    def test_integrate_same_content_is_already_integrated(self):
        self._fresh_fixture('one.appimage')
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'one.appimage'), '--yes')
        self._fresh_fixture('two.appimage')
        result = self.run_cli('--integrate',
                              os.path.join(self.downloads, 'two.appimage'),
                              '--yes', '--json')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)['result'],
                         'already-integrated')
        # the second copy was left where it was
        self.assertTrue(os.path.exists(
            os.path.join(self.downloads, 'two.appimage')))

    def test_integrate_conflict_keeps_both(self):
        # same desktop-entry Name, different file content -> keep both
        # with a version-suffixed filename (upstream behaviour)
        self._fresh_fixture('a.appimage')
        self._fresh_fixture('b.appimage', extra=b'x')
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'a.appimage'), '--yes')
        result = self.run_cli('--integrate',
                              os.path.join(self.downloads, 'b.appimage'),
                              '--yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)

        self.assertEqual(payload['app']['path'],
                         os.path.join(self.managed_dir,
                                      'neovim.appimage_v0_11_3.appimage'))
        self.assertTrue(os.path.isfile(os.path.join(
            self.applications_dir, 'neovim.appimage_v0_11_3.desktop')))
        listed = json.loads(
            self.run_cli('--list-installed', '--json').stdout)['installed']
        self.assertEqual(len(listed), 2)

    def test_integrate_conflict_replace(self):
        self._fresh_fixture('a.appimage')
        self._fresh_fixture('b.appimage', extra=b'x')
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'a.appimage'), '--yes')
        result = self.run_cli('--integrate',
                              os.path.join(self.downloads, 'b.appimage'),
                              '--yes', '--replace', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)

        # replace reuses the original filename: only one app remains
        listed = json.loads(
            self.run_cli('--list-installed', '--json').stdout)['installed']
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]['desktop_id'], 'neovim.desktop')

    def test_replace_overwrites_in_place(self):
        # GearLever's REPLACE logic reuses the old filename, so the new
        # binary overwrites the old one at the same path (no trash copy)
        self._fresh_fixture('a.appimage')
        self._fresh_fixture('b.appimage', extra=b'x')
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'a.appimage'), '--yes')
        self.run_cli('--integrate',
                     os.path.join(self.downloads, 'b.appimage'), '--yes',
                     '--replace')
        self.assertTrue(os.path.isfile(os.path.join(
            self.managed_dir, 'neovim.appimage')))
        # old binary was overwritten, not trashed
        self.assertFalse(os.path.exists(os.path.join(
            self.trash_files, 'neovim.appimage')))
        # exactly one neovim installation remains
        listed = json.loads(
            self.run_cli('--list-installed', '--json').stdout)['installed']
        self.assertEqual(len(listed), 1)


if __name__ == '__main__':
    unittest.main()

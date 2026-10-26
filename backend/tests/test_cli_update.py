# test_cli_update.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import json
import os
import shutil
import stat
from pathlib import Path
from unittest import mock

from cli_support import InProcessCliMixin
from helpers import (FakeXDGTestCase, FixtureHTTPServer, download_fixture)

from omarchy_appimage import cli, net, utils
from omarchy_appimage import settings as appimage_settings
from omarchy_appimage.ini_config import Config


class InProcessUpdateCliTests(InProcessCliMixin, FakeXDGTestCase):
    """--list-updates / --fetch-updates / --update flows that need patched
    connectivity or a fake FUSE mount (impossible in a subprocess)."""

    def setUp(self):
        super().setUp()
        # the loopback fixture servers are plain http on a private
        # address: open net.py's test-only seam for the duration of each
        # test (there is deliberately no environment variable — round 2
        # removed it)
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def _app_with_source(self, routes):
        appimage, _desktop, _icon = self.install_fake_app()
        server = FixtureHTTPServer(routes)
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})
        return appimage, server

    def test_list_updates_offline_json_error(self):
        self.install_fake_app()
        with mock.patch.object(net, 'check_internet', return_value=False):
            out, err, code = self.run_cmd(cli.cmd_list_updates, ['--json'])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['error'], 'Internet connection not available')

    def test_list_updates_offline_plain(self):
        self.install_fake_app()
        with mock.patch.object(net, 'check_internet', return_value=False):
            out, err, code = self.run_cmd(cli.cmd_list_updates, [])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('Internet connection not available', err)

    def test_list_updates_with_local_server(self):
        self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        with mock.patch.object(net, 'check_internet', return_value=True):
            out, err, code = self.run_cmd(cli.cmd_list_updates, ['--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(len(payload['updates']), 1)
        update = payload['updates'][0]
        self.assertEqual(update['manager'], 'StaticFileUpdater')
        self.assertEqual(update['download_size'], 5000)
        self.assertFalse(update['embedded_source'])
        self.assertIsNone(update['available_version'])

        # plain-text row format
        with mock.patch.object(net, 'check_internet', return_value=True):
            out, err, code = self.run_cmd(cli.cmd_list_updates, [])
        self.assertEqual(code, 0)
        self.assertIn('[Update available, StaticFileUpdater]', out)
        self.assertIn('Fake App', out)

    def test_list_updates_no_updates(self):
        appimage, _desktop, _icon = self.install_fake_app()
        # serve a body of exactly the local size -> not an update
        with open(appimage, 'rb') as f:
            body = f.read()
        server = FixtureHTTPServer({'/f.appimage': (200, {}, body)})
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})
        with mock.patch.object(net, 'check_internet', return_value=True):
            out, err, code = self.run_cmd(cli.cmd_list_updates, ['--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['updates'], [])

    def test_fetch_updates_offline(self):
        self.install_fake_app()
        with mock.patch.object(net, 'check_internet', return_value=False):
            out, err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {
            'schema_version': 1, 'updates': [],
            'notified': False, 'offline': True})
        # offline rounds must not touch the state file
        self.assertFalse(
            os.path.exists(appimage_settings.updates_state_path()))

    def test_fetch_updates_notifies_only_for_new_updates(self):
        self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        sent = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command', sent):
            out, err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload['updates']), 1)
        self.assertTrue(payload['notified'])
        self.assertFalse(payload['offline'])

        # exactly one notify-send with the expected argv shape
        self.assertEqual(sent.call_count, 1)
        argv = sent.call_args.args[0]
        self.assertEqual(argv[0], 'notify-send')
        self.assertIn('--expire-time=5000', argv)
        self.assertEqual(argv[-2], 'AppImage updates available')

        # the state now remembers the signature (as a per-app record, #39)...
        state = appimage_settings.load_updates_state()
        self.assertEqual(state['fakeapp.desktop']['signature'], '|5000')

        # ...so the next run sees the same update WITHOUT notifying again
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command', sent) as sent2:
            out, err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(json.loads(out)['notified'], False)
        self.assertEqual(sent2.call_count, 1)
        self.assertEqual(len(json.loads(out)['updates']), 1)

    def test_fetch_updates_notification_failure_is_not_fatal(self):
        self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command',
                                  side_effect=FileNotFoundError('notify-send')):
            out, err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload['notified'], False)
        self.assertEqual(len(payload['updates']), 1)
        # the state is still saved (the release was seen)
        entry = appimage_settings.load_updates_state()['fakeapp.desktop']
        self.assertEqual(entry['signature'], '|5000')

    def test_fetch_updates_nonzero_notify_exit(self):
        self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command',
                                  mock.Mock(
                                      return_value=mock.Mock(returncode=1))):
            out, err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(json.loads(out)['notified'], False)

    def test_update_reports_up_to_date_before_the_running_guard(self):
        appimage, _desktop, _icon = self.install_fake_app()
        # serve the exact local bytes: with --force the flow proceeds and
        # the size check answers up-to-date (no download needed)
        with open(appimage, 'rb') as f:
            body = f.read()
        server = FixtureHTTPServer({'/f.appimage': (200, {}, body)})
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})

        # pretend the app is FUSE-mounted (how running type-2 AppImages
        # are detected) via the /proc/mounts seam
        mounts = os.path.join(self.sandbox, 'mounts')
        Path(mounts).write_text(
            f'{appimage} /tmp/.mount_fake fuse.AppImage rw 0 0\n')

        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', mounts):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        # ticket #28(A): the availability decision comes before the
        # running guard, so a running app with nothing to install is up
        # to date — never `skipped-running`, and nothing is closed for a
        # no-op update
        self.assertEqual(payload['result'], 'up-to-date')
        self.assertEqual(payload['app']['path'], appimage)

        # --force overrides the running check and proceeds to the
        # availability check (same bytes served -> up-to-date, exit 0)
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', mounts):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json', '--force'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'up-to-date')

    def test_update_without_source_errors(self):
        self.install_fake_app()
        # in-process the error DOCUMENT is printed by main(), so here we
        # only see the OperationError message on stderr
        out, err, code = self.run_cmd(cli.cmd_update,
                                      ['fakeapp', '--yes', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('--set-update-source', err)

    def test_update_requires_yes_with_json(self):
        self.install_fake_app()
        out, err, code = self.run_cmd(cli.cmd_update,
                                      ['fakeapp', '--json'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['result'], 'error')


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

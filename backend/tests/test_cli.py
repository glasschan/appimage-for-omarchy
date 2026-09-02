# test_cli.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import hashlib
import io
import json
import os
import shutil
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import (FakeXDGTestCase, FixtureHTTPServer, download_fixture)

from omarchy_appimage import cli, net, utils
from omarchy_appimage import settings as appimage_settings
from omarchy_appimage.ini_config import Config


class InProcessCliMixin:
    """Runs cmd_* functions directly so net/internals can be patched."""

    def run_cmd(self, func, args: list):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = func(args)
            except cli.UsageError as e:
                err.write(f'Error: {e}\n')
                code = 2
            except (cli.OperationError, cli.InternalError) as e:
                err.write(f'Error: {e}\n')
                code = 1
        return out.getvalue(), err.getvalue(), code


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
        with open(appimage, 'wb') as f:
            f.write(make_elf_with_sections(
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

        # the state now remembers the signature...
        state = appimage_settings.load_updates_state()
        self.assertEqual(state.get('fakeapp.desktop'), '|5000')

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
        self.assertEqual(appimage_settings.load_updates_state()
                         .get('fakeapp.desktop'), '|5000')

    def test_fetch_updates_nonzero_notify_exit(self):
        self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command',
                                  mock.Mock(
                                      return_value=mock.Mock(returncode=1))):
            out, err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(json.loads(out)['notified'], False)

    def test_update_skipped_while_running(self):
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
        with open(mounts, 'w') as f:
            f.write(f'{appimage} /tmp/.mount_fake fuse.AppImage rw 0 0\n')

        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', mounts):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload['result'], 'skipped-running')
        self.assertIn('--force', payload['message'])
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


class UpdateFlowTests(InProcessCliMixin, FakeXDGTestCase):
    """Full --update flow against a real type-2 AppImage fixture, served
    by a local HTTP server as a 'new release' (same bytes + harmless
    appended padding so the availability check reports an update).

    Round 2: the release is offered through a faked GitHub API response
    carrying the asset's sha256 digest — installation refuses digest-less
    sources now. The network-touching CLI calls run in-process so the
    test-only seam (net._ALLOW_LOCAL_FOR_TESTS) covers them; the old
    environment kill switch is gone."""

    def setUp(self):
        super().setUp()
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        self.fixture = fixture
        self.downloads = os.path.join(self.sandbox, 'downloads')
        os.makedirs(self.downloads, exist_ok=True)
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def _integrate_fixture(self) -> str:
        src = os.path.join(self.downloads, 'nvim.appimage')
        shutil.copyfile(self.fixture, src)
        result = self.run_cli('--integrate', src, '--yes', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        managed = os.path.join(self.managed_dir, 'neovim.appimage')
        self.assertTrue(os.path.isfile(managed))
        return managed

    def _serve_new_release(self, extra: bytes) -> tuple:
        name = 'nvim-updated.appimage'
        path = os.path.join(self.downloads, name)
        shutil.copyfile(self.fixture, path)
        with open(path, 'ab') as f:
            f.write(extra)
        with open(path, 'rb') as f:
            body = f.read()
        server = FixtureHTTPServer({'/' + name: (200, {}, body)})
        self.addCleanup(server.close)
        return server.url('/' + name), body

    def _set_github_source(self) -> None:
        """A custom GithubUpdater source (wins over the fixture's
        embedded gh-releases-zsync string)."""
        out, err, code = self.run_cmd(
            cli.cmd_set_update_source,
            ['neovim', '--manager', 'GithubUpdater', 'repo=owner/repo',
             'repo_filename=nvim-updated*.appimage',
             'allow_prereleases=false', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'set')

    def _github_release_patch(self, url: str, body: bytes):
        """A faked releases/latest API response whose single asset points
        at the loopback server and carries the artifact's sha256 digest
        (mandatory for installation since round 2)."""
        release = {
            'tag_name': 'v0.11.3', 'draft': False, 'prerelease': False,
            'assets': [{
                'name': 'nvim-updated.appimage', 'size': len(body),
                'browser_download_url': url,
                'digest': 'sha256:' + hashlib.sha256(body).hexdigest(),
            }],
        }
        return mock.patch.object(net, 'fetch_json', return_value=release)

    def _state_path(self) -> str:
        return os.path.join(self.config_home, 'io.github.glasschan.appimage',
                            'updates-state.json')

    def test_update_end_to_end(self):
        managed = self._integrate_fixture()
        url, body = self._serve_new_release(b'NEW-RELEASE-PADDING')
        self._set_github_source()
        release = self._github_release_patch(url, body)

        # pre-seed a pending-notification marker like --fetch-updates would
        os.makedirs(os.path.dirname(self._state_path()), exist_ok=True)
        with open(self._state_path(), 'w') as f:
            json.dump({'neovim.desktop': 'v0.11.3|10996216'}, f)

        with release:
            out, err, code = self.run_cmd(cli.cmd_update,
                                          ['neovim', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload['result'], 'updated')
        self.assertEqual(payload['message'],
                         f'{managed} was updated successfully')
        self.assertEqual(payload['downloaded_bytes'], len(body))
        self.assertEqual(payload['app']['path'], managed)
        self.assertEqual(payload['app']['desktop_id'], 'neovim.desktop')

        # the managed file was replaced in place with the served bytes
        with open(managed, 'rb') as f:
            self.assertEqual(f.read(), body)

        # the notification marker was cleared by the successful update
        with open(self._state_path()) as f:
            self.assertEqual(json.load(f), {})

        # app-menu consistency: one app left, same desktop id, the custom
        # source recorded
        payload = json.loads(
            self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(len(payload['installed']), 1)
        self.assertEqual(payload['installed'][0]['desktop_id'],
                         'neovim.desktop')
        self.assertEqual(payload['installed'][0]['manager'], 'GithubUpdater')

        # the served release is now installed -> up-to-date (exit 0):
        # the asset digest matches the local file
        with release:
            out, err, code = self.run_cmd(cli.cmd_update,
                                          ['neovim', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'up-to-date')

    def test_update_keep_both(self):
        managed = self._integrate_fixture()
        url, body = self._serve_new_release(b'KEEP-BOTH-PADDING')
        self._set_github_source()

        with self._github_release_patch(url, body):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['neovim', '--yes', '--keep-both', '--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload['result'], 'updated')

        # KEEP logic: the old appimage stays untouched, the new release
        # was installed under a second (version-suffixed) name
        self.assertTrue(os.path.isfile(managed))
        with open(managed, 'rb') as f:
            self.assertNotEqual(f.read(), body)
        payload = json.loads(
            self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(len(payload['installed']), 2)


if __name__ == '__main__':
    unittest.main()

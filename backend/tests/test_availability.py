# test_availability.py — issue #39 (a check says whether its update can be
# installed) and issue #43 (a failed check keeps what the last one learned).

import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import FakeXDGTestCase, FixtureHTTPServer

from omarchy_appimage import cli, net, settings, updaters
from omarchy_appimage.ini_config import Config
from omarchy_appimage.updaters import (CodebergUpdater, ForgejoUpdater,
                                       GithubUpdater, GitlabUpdater,
                                       StaticFileUpdater, UpdateManager)


def _fake_element(path='/tmp/app/Fake App.appimage'):
    el = mock.Mock()
    el.file_path = path
    el.desktop_file_path = '/tmp/app/fakeapp.desktop'
    el.name = 'Fake App'
    return el


class ManagerVerdictTests(unittest.TestCase):
    """What each source can prove, without any network."""

    def test_a_plain_static_url_is_only_a_size_check(self):
        manager = StaticFileUpdater(_fake_element())
        with mock.patch.object(manager, 'get_config',
                               return_value={'url': 'https://x/a.appimage'}):
            self.assertEqual(manager.verification_method(), 'size')

    def test_a_zsync_control_file_is_a_digest_check(self):
        manager = StaticFileUpdater(_fake_element())
        with mock.patch.object(manager, 'get_config',
                               return_value={'url': 'https://x/a.AppImage.zsync'}):
            self.assertEqual(manager.verification_method(), 'zsync-sha1')

    def test_an_embedded_zsync_string_is_a_digest_check(self):
        manager = StaticFileUpdater(
            _fake_element(), embedded='zsync|https://x/a.AppImage.zsync')
        self.assertEqual(manager.verification_method(), 'zsync-sha1')

    def test_the_gitlab_family_reports_a_size_check(self):
        for manager_class in (GitlabUpdater, CodebergUpdater, ForgejoUpdater):
            with self.subTest(manager_class=manager_class.__name__):
                manager = manager_class(_fake_element())
                self.assertEqual(manager.verification_method(), 'size')

    def test_github_without_a_digest_reports_a_size_check(self):
        manager = GithubUpdater(_fake_element())
        manager._target_asset = {'asset': {'size': 10}, 'zsync': None}
        self.assertEqual(manager.verification_method(), 'size')

    def test_github_with_a_digest_reports_sha256(self):
        manager = GithubUpdater(_fake_element())
        manager._target_asset = {
            'asset': {'size': 10, 'digest': 'sha256:' + 'a' * 64}}
        self.assertEqual(manager.verification_method(), 'sha256')

    def test_a_paired_zsync_does_not_remove_the_github_digest_requirement(self):
        manager = GithubUpdater(_fake_element())
        manager._target_asset = {'asset': {'size': 10},
                                 'zsync': {'browser_download_url': 'https://x/a.zsync'}}
        self.assertEqual(manager.verification_method(), 'size')

    def test_the_base_manager_has_nothing_to_offer(self):
        self.assertEqual(UpdateManager(_fake_element()).verification_method(),
                         'none')

    def test_no_verdict_before_a_check_runs(self):
        manager = GitlabUpdater(_fake_element())
        self.assertIsNone(manager.installable())

    def test_a_size_only_source_is_reported_not_installable(self):
        manager = GitlabUpdater(_fake_element())
        manager.checked = True
        self.assertIs(manager.installable(), False)

    def test_a_digest_source_is_reported_installable(self):
        manager = GithubUpdater(_fake_element())
        manager.checked = True
        manager._target_asset = {
            'asset': {'size': 10, 'digest': 'sha256:' + 'b' * 64}}
        self.assertIs(manager.installable(), True)


class LoopbackCliTestCase(FakeXDGTestCase):
    """Sandbox + the loopback transport seam open for the test's duration."""

    def setUp(self):
        super().setUp()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def run_cmd(self, func, args):
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


class AvailabilityCliTests(LoopbackCliTestCase):
    """The document a listing produces, against a loopback server."""

    def _app_with_source(self, routes, url_path='/f.appimage'):
        self.install_fake_app()
        server = FixtureHTTPServer(routes)
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url(url_path)})
        return el, server

    def test_a_size_only_source_is_listed_as_not_installable(self):
        self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        with mock.patch.object(net, 'check_internet', return_value=True):
            out, err, code = self.run_cmd(cli.cmd_list_updates, ['--json'])
        self.assertEqual(code, 0, err)
        app = json.loads(out)['updates'][0]
        self.assertEqual(app['verification'], 'size')
        self.assertIs(app['installable'], False)

    def test_a_listing_without_a_check_reports_no_verdict(self):
        self.install_fake_app()
        out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0, err)
        app = json.loads(out)['installed'][0]
        self.assertEqual(app['verification'], 'none')
        self.assertIsNone(app['installable'])

    def test_a_not_installable_verdict_matches_a_refused_fetch(self):
        # The point of #39: the verdict is the fetch path's own answer, not
        # a second opinion. Same source, same policy, both must refuse.
        el, _server = self._app_with_source({'/f.appimage': (200, {}, b'B' * 5000)})
        with mock.patch.object(net, 'check_internet', return_value=True):
            out, _err, _code = self.run_cmd(cli.cmd_list_updates, ['--json'])
        self.assertIs(json.loads(out)['updates'][0]['installable'], False)

        manager = cli.UpdateManagerChecker.check_url_for_app(el)
        with self.assertRaises(Exception) as caught:
            manager.download(self.sandbox)
        self.assertIn('refusing to install an unverified artifact',
                      str(caught.exception))


class ControlFileVerdictTests(LoopbackCliTestCase):
    """A control file without a SHA-1 line cannot be installed, so the
    method must not claim one (marketplace finding: installable said true
    for a source whose download refuses)."""

    def _manager(self, routes):
        server = FixtureHTTPServer(routes)
        self.addCleanup(server.close)
        os.makedirs(self.managed_dir, exist_ok=True)
        local = os.path.join(self.managed_dir, 'fake.appimage')
        with open(local, 'wb') as f:
            f.write(b'A' * 10)
        # A real file: the check hashes the installed AppImage.
        manager = updaters.StaticFileUpdater(_fake_element(local))
        manager.get_config = lambda: {'url': server.url('/f.appimage.zsync')}
        return manager, server

    def test_a_control_file_without_a_sha1_line_is_reported_as_size_only(self):
        manager, server = self._manager({})
        server.routes['/f.appimage.zsync'] = (
            200, {}, b'URL: f.appimage\nBlocksize: 2048\n')
        server.routes['/f.appimage'] = (200, {}, b'A' * 5000)
        self.assertIsNone(manager.is_update_available())
        self.assertFalse(manager.checked)
        self.assertEqual(manager.verification_method(), 'size')
        self.assertIsNone(manager.installable())

    def test_a_control_file_with_a_sha1_line_is_installable(self):
        manager, server = self._manager({})
        digest = 'b' * 40
        server.routes['/f.appimage.zsync'] = (
            200, {}, f'URL: f.appimage\nSHA-1: {digest}\n'.encode())
        server.routes['/f.appimage'] = (200, {}, b'A' * 5000)
        self.assertTrue(manager.is_update_available())
        manager.checked = True
        self.assertEqual(manager.verification_method(), 'zsync-sha1')
        self.assertTrue(manager.installable())


class UpdateStateMergeTests(LoopbackCliTestCase):
    """#43: a sweep updates what it learned and keeps the rest."""

    def _seed_state(self, state):
        settings.save_updates_state(state)

    def test_a_failed_check_keeps_the_previous_result(self):
        self.install_fake_app()
        server = FixtureHTTPServer({})
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})
        self._seed_state({'fakeapp.desktop': 'earlier-release|5000'})

        with mock.patch.object(net, 'check_internet', return_value=True):
            _out, _err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)

        # the check could not see the release (no route), so what the last
        # successful sweep learned is still there, still in its old shape
        self.assertEqual(settings.load_updates_state()['fakeapp.desktop'],
                         'earlier-release|5000')

    def test_a_legacy_signature_still_suppresses_a_notification(self):
        self.install_fake_app()
        server = FixtureHTTPServer({'/f.appimage': (200, {}, b'B' * 5000)})
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})
        self._seed_state({'fakeapp.desktop': '|5000'})

        sent = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command', sent):
            out, _err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)['notified'])
        self.assertEqual(sent.call_count, 0)

    def test_entries_for_uninstalled_apps_are_dropped(self):
        self._seed_state({'ghost.desktop': 'gone|1'})
        with mock.patch.object(net, 'check_internet', return_value=True):
            _out, _err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)
        self.assertNotIn('ghost.desktop', settings.load_updates_state())

    def test_an_app_the_listing_could_not_see_keeps_its_entry(self):
        # The listing skips an app whose desktop entry is momentarily
        # unreadable; that must not look like an uninstall.
        os.makedirs(self.managed_dir, exist_ok=True)
        present = os.path.join(self.managed_dir, 'invisible.appimage')
        with open(present, 'wb') as f:
            f.write(b'\x7fELF')
        self._seed_state({'invisible.desktop': {
            'signature': 'seen|1', 'installable': False, 'path': present}})
        with mock.patch.object(net, 'check_internet', return_value=True):
            _out, _err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)
        self.assertEqual(settings.load_updates_state()['invisible.desktop'],
                         {'signature': 'seen|1', 'installable': False,
                          'path': present})

    def test_a_new_result_keeps_its_installability(self):
        self.install_fake_app()
        server = FixtureHTTPServer({'/f.appimage': (200, {}, b'B' * 5000)})
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})
        with mock.patch.object(net, 'check_internet', return_value=True), \
                mock.patch.object(cli, 'run_command',
                                  mock.Mock(return_value=mock.Mock(returncode=0))):
            _out, _err, code = self.run_cmd(cli.cmd_fetch_updates, ['--json'])
        self.assertEqual(code, 0)
        entry = settings.load_updates_state()['fakeapp.desktop']
        self.assertEqual(entry['signature'], '|5000')
        self.assertIs(entry['installable'], False)


if __name__ == '__main__':
    unittest.main()
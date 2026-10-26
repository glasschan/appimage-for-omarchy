# test_cli_flows.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import hashlib
import json
import os
import shutil
from pathlib import Path
from unittest import mock

from cli_support import InProcessCliMixin
from helpers import (FakeXDGTestCase, FixtureHTTPServer, download_fixture)

from omarchy_appimage import cli, net


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
        Path(self._state_path()).write_text(
            json.dumps({'neovim.desktop': 'v0.11.3|10996216'}))

        with release, mock.patch.object(
                cli.appimage_provider, 'apply_release_version',
                wraps=cli.appimage_provider.apply_release_version) as spy:
            out, err, code = self.run_cmd(cli.cmd_update,
                                          ['neovim', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        # the manager's release tag is offered as the version (the fixture
        # states its own, so apply_release_version declines it)
        self.assertEqual([c.args[1] for c in spy.call_args_list],
                         ['v0.11.3'])
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

# test_cli_urls.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
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
import unittest
from pathlib import Path
from unittest import mock

from cli_support import InProcessCliMixin
from helpers import (FakeXDGTestCase, download_fixture)

from omarchy_appimage import cli, net
from omarchy_appimage.ini_config import Config
from omarchy_appimage.provider import AppImageListElement, InstalledStatus


def _last_json(out: str) -> dict:
    """The result document of an integrate run: the last stdout line.
    Since ticket #58 a URL --json integrate emits progress documents
    ahead of it, so callers read the last line, never the whole stream."""
    return json.loads(out.splitlines()[-1])


class UrlIntegrateTests(InProcessCliMixin, FakeXDGTestCase):
    """--integrate <github release download URL>: download, verify the
    asset's sha256 digest via the GitHub API (mandatory), integrate, and
    auto-prepopulate the per-app GithubUpdater source from the URL.

    Hermetic: the GitHub API and the download are patched at the net
    seam; the fixture bytes are a real extractable AppImage so the full
    install_file path (metadata extraction -> desktop/icon) runs."""

    URL = ('https://github.com/neovim/neovim/releases/download/'
           'v4.5.6/nvim-4.5.6-linux-x86_64.AppImage')

    def setUp(self):
        super().setUp()
        self.fixture = download_fixture()
        if self.fixture is None:
            self.skipTest('fixture download unavailable')
        with open(self.fixture, 'rb') as f:
            self.body = f.read()

    def _patch_download_and_api(self, digest_suffix: str = None):
        """Patch the GitHub API response and the download to a hermetic
        in-memory copy of the fixture."""
        asset_digest = (digest_suffix
                        if digest_suffix is not None
                        else hashlib.sha256(self.body).hexdigest())
        release = {
            'tag_name': 'v4.5.6', 'draft': False, 'prerelease': False,
            'assets': [{
                'name': 'nvim-4.5.6-linux-x86_64.AppImage',
                'size': len(self.body),
                'digest': 'sha256:' + asset_digest,
            }],
        }
        api = mock.patch.object(net, 'fetch_json', return_value=release)
        api.start()
        self.addCleanup(api.stop)

        def _fake_download(url, dest, progress_cb=None, **kwargs):
            # byte_cb (ticket #58 progress stream) is accepted but not
            # driven here: these tests pin the result document, and a
            # caller that reads only the last line keeps working
            Path(dest).write_bytes(self.body)
            return len(self.body)
        dl = mock.patch.object(net, 'download_to_file',
                               side_effect=_fake_download)
        dl.start()
        self.addCleanup(dl.stop)

    def _integrate(self, url=URL, extra_args=()):
        return self.run_cmd(cli.cmd_integrate, [url, '--yes', '--json',
                                                *extra_args])

    # ------------------------------------------------------------ parsing

    def test_github_release_url_data_parses(self):
        d = cli._github_release_url_data(self.URL)
        self.assertEqual(d, {
            'owner': 'neovim', 'repo': 'neovim',
            'tag': 'v4.5.6', 'filename': 'nvim-4.5.6-linux-x86_64.AppImage',
        })

    def test_github_release_url_data_rejects_non_github(self):
        self.assertIsNone(cli._github_release_url_data(
            'https://example.com/a/b/releases/download/v1/x.AppImage'))
        self.assertIsNone(cli._github_release_url_data(
            'https://github.com/a/b/releases/tag/v1/x.AppImage'))
        self.assertIsNone(cli._github_release_url_data('not a url'))

    # ------------------------------------------------------- auto source

    def test_auto_set_github_source_derives_glob(self):
        el = AppImageListElement(name='X', description='', provider='P',
                                 installed_status=InstalledStatus.INSTALLED,
                                 file_path=os.path.join(self.sandbox, 'x'))
        cli._auto_set_github_source(el, {
            'owner': 'neovim', 'repo': 'neovim', 'tag': 'v4.5.6',
            'filename': 'nvim-4.5.6-linux-x86_64.AppImage'})
        cfg = Config.get_app_update_config(el)
        self.assertEqual(cfg['manager'], 'GithubUpdater')
        self.assertEqual(cfg['repo'], 'neovim/neovim')
        self.assertEqual(cfg['repo_filename'], 'nvim-*-linux-x86_64.AppImage')
        self.assertEqual(cfg['allow_prereleases'], 'false')

    def test_auto_set_github_source_does_not_clobber(self):
        el = AppImageListElement(name='X', description='', provider='P',
                                 installed_status=InstalledStatus.INSTALLED,
                                 file_path=os.path.join(self.sandbox, 'x'))
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': 'https://x/f.appimage'})
        cli._auto_set_github_source(el, {
            'owner': 'a', 'repo': 'b', 'tag': 'v1', 'filename': 'b.appimage'})
        cfg = Config.get_app_update_config(el)
        self.assertEqual(cfg['manager'], 'StaticFileUpdater')

    def test_auto_set_github_source_keeps_exact_name_when_no_version(self):
        el = AppImageListElement(name='X', description='', provider='P',
                                 installed_status=InstalledStatus.INSTALLED,
                                 file_path=os.path.join(self.sandbox, 'x'))
        cli._auto_set_github_source(el, {
            'owner': 'a', 'repo': 'b', 'tag': 'v1',
            'filename': 'arch.AppImage'})
        cfg = Config.get_app_update_config(el)
        self.assertEqual(cfg['repo_filename'], 'arch.AppImage')

    # --------------------------------------------------------- integrate

    def test_integrate_from_url_installs_and_auto_sources(self):
        self._patch_download_and_api()
        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        payload = _last_json(out)
        self.assertEqual(payload['result'], 'integrated')

        # the AppImage landed in the managed folder
        managed = os.path.join(self.managed_dir, 'neovim.appimage')
        self.assertTrue(os.path.isfile(managed))

        # the auto GithubUpdater source is stored
        el = cli.appimage_provider.list_installed()[0]
        cfg = Config.get_app_update_config(el)
        self.assertEqual(cfg['manager'], 'GithubUpdater')
        self.assertEqual(cfg['repo'], 'neovim/neovim')
        self.assertEqual(cfg['repo_filename'], 'nvim-*-linux-x86_64.AppImage')

        # the fixture states its own X-AppImage-Version, so the URL's tag
        # (v4.5.6) must NOT replace it (apply_release_version)
        payload = json.loads(
            self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(payload['installed'][0]['current_version'],
                         'v0.11.3')

    def test_reintegrating_the_same_url_repairs_a_missing_version(self):
        # An install made by an older plugin states no version, and an even
        # older one baked GearLever's md5 artifact into the display Name.
        # Re-integrating the same URL installs nothing, but it must repair
        # that metadata: otherwise the app stays versionless until some
        # later release happens to overwrite it.
        self._patch_download_and_api()
        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        self.assertEqual(_last_json(out)['result'], 'integrated')

        # degrade the install to what an older plugin would have left: no
        # X-AppImage-Version at all, and the md5 hash in the display Name
        installed = cli.appimage_provider.list_installed()[0]
        desktop = Path(installed.desktop_file_path)
        kept = [line for line in desktop.read_text().splitlines()
                if not line.startswith('X-AppImage-Version=')]
        desktop.write_text('\n'.join(
            'Name=Neovim (77efdf)' if line.startswith('Name=') else line
            for line in kept) + '\n')
        self.assertIsNone(
            cli.appimage_provider.list_installed()[0].version)

        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        payload = _last_json(out)
        self.assertEqual(payload['result'], 'already-integrated')
        self.assertEqual(payload['app']['current_version'], 'v4.5.6')
        self.assertEqual(payload['app']['name'], 'Neovim')

        installed = cli.appimage_provider.list_installed()[0]
        self.assertEqual(installed.version, 'v4.5.6')
        self.assertEqual(installed.name, 'Neovim')

        # idempotent: a second repair run changes nothing and still exits 0
        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        self.assertEqual(_last_json(out)['result'], 'already-integrated')

    def test_integrate_from_url_writes_the_release_tag_as_the_version(self):
        # End to end: an AppImage that states no version of its own gets
        # the tag the URL names, written into the installed desktop entry
        # (X-AppImage-Version + the 'Name (version)' suffix) instead of
        # being listed with no version at all.
        self._patch_download_and_api()
        real_load = cli.appimage_provider._load_appimage_metadata
        popped = []

        def _load_versionless(el):
            result = real_load(el)
            if not popped:
                popped.append(True)
                result.desktop_entry.main.pop('X-AppImage-Version', None)
            return result

        with mock.patch.object(cli.appimage_provider,
                               '_load_appimage_metadata',
                               side_effect=_load_versionless):
            out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        payload = _last_json(out)
        self.assertEqual(payload['app']['current_version'], 'v4.5.6')
        self.assertEqual(payload['app']['name'], 'Neovim (v4.5.6)')

        installed = cli.appimage_provider.list_installed()[0]
        self.assertEqual(installed.version, 'v4.5.6')
        self.assertEqual(installed.name, 'Neovim (v4.5.6)')

    def test_integrate_from_url_offers_the_release_tag_as_the_version(self):
        # An image with no X-AppImage-Version of its own (real ones exist:
        # Terax, OpenTypeless) is integrated with the tag the URL names —
        # otherwise the app is listed with no version at all.
        self._patch_download_and_api()
        seen = []
        real = cli.appimage_provider.apply_release_version

        def _spy(el, version):
            seen.append(version)
            return real(el, version)

        with mock.patch.object(cli.appimage_provider,
                               'apply_release_version', side_effect=_spy):
            out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        self.assertEqual(_last_json(out)['result'], 'integrated')
        self.assertEqual(seen, ['v4.5.6'])

    def test_integrate_from_url_refuses_missing_digest(self):
        # an asset with no sha256 digest is refused (same contract as
        # GithubUpdater.download) before any byte is spent
        self._patch_download_and_api(digest_suffix='')
        out, err, code = self._integrate()
        self.assertEqual(code, 1)
        self.assertIn('no sha256 digest', err)

    def test_integrate_url_falls_back_to_static_on_lookup_failure(self):
        # a /releases/download/ URL on an unknown host classifies as a
        # Forgejo release; when its release API has no metadata the pasted
        # URL is installed as a plain static source instead of erroring.
        url = 'https://example.com/a/b/releases/download/v1/x.AppImage'
        api = mock.patch.object(net, 'fetch_json',
                                side_effect=net.NetworkError('404'))
        api.start()
        self.addCleanup(api.stop)

        def _fake_download(u, dest, progress_cb=None, **kwargs):
            Path(dest).write_bytes(self.body)
            return len(self.body)
        dl = mock.patch.object(net, 'download_to_file',
                               side_effect=_fake_download)
        dl.start()
        self.addCleanup(dl.stop)

        out, err, code = self._integrate(url=url)
        self.assertEqual(code, 0, err)
        self.assertEqual(_last_json(out)['result'], 'integrated')
        el = cli.appimage_provider.list_installed()[0]
        cfg = Config.get_app_update_config(el)
        self.assertEqual(cfg['manager'], 'StaticFileUpdater')
        self.assertEqual(cfg['url'], url)


class UrlMultiSourceIntegrateTests(InProcessCliMixin, FakeXDGTestCase):
    """--integrate <url> for the non-GitHub source table (zsync, GitLab,
    Codeberg, Forgejo, plain https) — hermetic: every egress crosses a
    patched net seam, the fixture bytes are a real extractable AppImage so
    the full install path runs."""

    ZSYNC_URL = 'https://dl.example.org/MyApp.AppImage.zsync'
    GITLAB_URL = ('https://gitlab.com/owner/repo/-/releases/v1.2.3/downloads/'
                  'MyApp-1.2.3.AppImage')
    CODEBERG_URL = ('https://codeberg.org/owner/repo/releases/download/'
                    'v1.2.3/MyApp-1.2.3.AppImage')
    FORGEJO_URL = ('https://forgejo.example.org/owner/repo/releases/download/'
                   'v1.2.3/MyApp-1.2.3.AppImage')
    STATIC_URL = 'https://dl.example.org/MyApp.AppImage'

    def setUp(self):
        super().setUp()
        self.fixture = download_fixture()
        if self.fixture is None:
            self.skipTest('fixture download unavailable')
        with open(self.fixture, 'rb') as f:
            self.body = f.read()
        self.downloads = []

    def _patch_download(self):
        def _fake_download(url, dest, progress_cb=None, **kwargs):
            self.downloads.append(url)
            Path(dest).write_bytes(self.body)
            return len(self.body)
        dl = mock.patch.object(net, 'download_to_file',
                               side_effect=_fake_download)
        dl.start()
        self.addCleanup(dl.stop)

    def _integrate(self, url):
        return self.run_cmd(cli.cmd_integrate, [url, '--yes', '--json'])

    def _assert_installed(self, out, err, code):
        self.assertEqual(code, 0, err)
        self.assertEqual(_last_json(out)['result'], 'integrated')
        self.assertTrue(os.path.isfile(
            os.path.join(self.managed_dir, 'neovim.appimage')))

    def _installed_config(self):
        el = cli.appimage_provider.list_installed()[0]
        return Config.get_app_update_config(el)

    def _zsync_control(self, target_url):
        return (f'URL: {target_url}\n'
                f'SHA-1: {hashlib.sha1(self.body).hexdigest()}\n\n'
                'zsync: 0.6.2\n')

    def test_zsync_url_installs_and_sets_static_source(self):
        target = 'https://dl.example.org/MyApp-1.2.3.AppImage'
        self._patch_download()
        with mock.patch.object(net, 'fetch_text',
                               return_value=self._zsync_control(target)):
            out, err, code = self._integrate(self.ZSYNC_URL)
        self._assert_installed(out, err, code)
        # the control file's target URL was downloaded, not the .zsync
        self.assertEqual(self.downloads, [target])
        cfg = self._installed_config()
        self.assertEqual(cfg['manager'], 'StaticFileUpdater')
        self.assertEqual(cfg['url'], self.ZSYNC_URL)

    def test_gitlab_url_installs_and_sets_gitlab_source(self):
        release = [{
            'tag_name': 'v1.2.3',
            'assets': {'links': [{
                'name': 'MyApp-1.2.3.AppImage',
                'direct_asset_url': self.GITLAB_URL,
            }]},
        }]
        self._patch_download()
        with mock.patch.object(net, 'fetch_json', return_value=release):
            out, err, code = self._integrate(self.GITLAB_URL)
        self._assert_installed(out, err, code)
        cfg = self._installed_config()
        self.assertEqual(cfg['manager'], 'GitlabUpdater')
        self.assertEqual(cfg['repo_url'], 'https://gitlab.com/owner/repo')
        self.assertEqual(cfg['repo_filename'], 'MyApp-*.AppImage')

    def test_codeberg_url_installs_and_sets_codeberg_source(self):
        releases = [{
            'tag_name': 'v1.2.3',
            'assets': [{
                'name': 'MyApp-1.2.3.AppImage', 'size': len(self.body),
                'browser_download_url': self.CODEBERG_URL,
            }],
        }]
        self._patch_download()
        with mock.patch.object(net, 'fetch_json', return_value=releases):
            out, err, code = self._integrate(self.CODEBERG_URL)
        self._assert_installed(out, err, code)
        cfg = self._installed_config()
        self.assertEqual(cfg['manager'], 'CodebergUpdater')
        self.assertEqual(cfg['repo'], 'owner/repo')
        self.assertEqual(cfg['repo_filename'], 'MyApp-*.AppImage')
        self.assertEqual(cfg['allow_prereleases'], 'false')

    def test_forgejo_url_installs_and_sets_forgejo_source(self):
        release = {
            'tag_name': 'v1.2.3',
            'assets': [{
                'name': 'MyApp-1.2.3.AppImage', 'size': len(self.body),
                'browser_download_url': self.FORGEJO_URL,
            }],
        }
        self._patch_download()
        with mock.patch.object(net, 'fetch_json', return_value=release):
            out, err, code = self._integrate(self.FORGEJO_URL)
        self._assert_installed(out, err, code)
        cfg = self._installed_config()
        self.assertEqual(cfg['manager'], 'ForgejoUpdater')
        self.assertEqual(cfg['repo_url'],
                         'https://forgejo.example.org/owner/repo')
        self.assertEqual(cfg['repo_filename'], 'MyApp-*.AppImage')
        self.assertEqual(cfg['allow_prereleases'], 'false')

    def test_plain_https_url_installs_as_static_source(self):
        self._patch_download()
        out, err, code = self._integrate(self.STATIC_URL)
        self._assert_installed(out, err, code)
        self.assertEqual(self.downloads, [self.STATIC_URL])
        cfg = self._installed_config()
        self.assertEqual(cfg['manager'], 'StaticFileUpdater')
        self.assertEqual(cfg['url'], self.STATIC_URL)

    def test_existing_source_is_not_overwritten(self):
        # first integrate records a static source; drop the managed file
        # (leaving the per-app config) and integrate again through a
        # .zsync URL — the stored source must stay untouched
        self._patch_download()
        out, err, code = self._integrate(self.STATIC_URL)
        self._assert_installed(out, err, code)
        os.remove(os.path.join(self.managed_dir, 'neovim.appimage'))

        target = 'https://dl.example.org/MyApp-1.2.3.AppImage'
        with mock.patch.object(net, 'fetch_text',
                               return_value=self._zsync_control(target)):
            out, err, code = self._integrate(self.ZSYNC_URL)
        self._assert_installed(out, err, code)
        cfg = self._installed_config()
        self.assertEqual(cfg['manager'], 'StaticFileUpdater')
        self.assertEqual(cfg['url'], self.STATIC_URL)


class IntegrateProgressTests(InProcessCliMixin, FakeXDGTestCase):
    """--integrate <url> --json emits `event: progress` lines ahead of the
    single, unchanged result document (ticket #58). Hermetic like the
    classes above: the GitHub API and the download are patched at the net
    seam and the fake download drives the byte_cb the way
    net.download_to_file does — byte_cb(done, total), total 0 when the
    server sends no Content-Length."""

    URL = ('https://github.com/neovim/neovim/releases/download/'
           'v4.5.6/nvim-4.5.6-linux-x86_64.AppImage')

    def setUp(self):
        super().setUp()
        self.fixture = download_fixture()
        if self.fixture is None:
            self.skipTest('fixture download unavailable')
        with open(self.fixture, 'rb') as f:
            self.body = f.read()
        release = {
            'tag_name': 'v4.5.6', 'draft': False, 'prerelease': False,
            'assets': [{
                'name': 'nvim-4.5.6-linux-x86_64.AppImage',
                'size': len(self.body),
                'digest': 'sha256:' + hashlib.sha256(self.body).hexdigest(),
            }],
        }
        api = mock.patch.object(net, 'fetch_json', return_value=release)
        api.start()
        self.addCleanup(api.stop)

    def _patch_download(self, drive=None):
        """Patch the download; `drive(byte_cb)` simulates the transfer's
        progress callbacks before the bytes are written."""

        def _fake_download(url, dest, progress_cb=None, **kwargs):
            byte_cb = kwargs.get('byte_cb')
            if drive and byte_cb:
                drive(byte_cb)
            Path(dest).write_bytes(self.body)
            return len(self.body)

        dl = mock.patch.object(net, 'download_to_file',
                               side_effect=_fake_download)
        dl.start()
        self.addCleanup(dl.stop)

    def _integrate(self, *extra):
        return self.run_cmd(cli.cmd_integrate,
                            [self.URL, '--yes', '--json', *extra])

    def _documents(self, out):
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def _progress_phases(self, docs):
        return [d['phase'] for d in docs if d.get('event') == 'progress']

    def test_progress_documents_precede_the_unchanged_result(self):
        total = len(self.body)
        self._patch_download(lambda cb: (cb(total // 4, total),
                                         cb(total, total)))
        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        docs = self._documents(out)
        self.assertEqual(self._progress_phases(docs),
                         ['downloading', 'downloading', 'verifying',
                          'installing'])
        for doc in docs:
            self.assertEqual(doc['schema_version'], 1)
        downloading = [d for d in docs if d.get('phase') == 'downloading']
        self.assertEqual(downloading[0]['bytes'], total // 4)
        self.assertEqual(downloading[0]['total'], total)
        self.assertEqual(downloading[1]['bytes'], total)
        # the result document is unchanged and still last
        self.assertNotIn('event', docs[-1])
        self.assertEqual(docs[-1]['result'], 'integrated')
        self.assertIn('app', docs[-1])

    def test_progress_fires_on_percent_changes_only(self):
        total = 10_000
        self._patch_download(lambda cb: (cb(1, total), cb(99, total),
                                         cb(100, total), cb(150, total),
                                         cb(199, total), cb(200, total),
                                         cb(total, total)))
        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        downloading = [d for d in self._documents(out)
                       if d.get('phase') == 'downloading']
        # percents 0, 1, 2 and 100 changed; everything inside a percent
        # is swallowed
        self.assertEqual([d['bytes'] for d in downloading],
                         [1, 100, 200, total])

    def test_no_content_length_reports_bytes_with_total_zero(self):
        self._patch_download(lambda cb: (cb(500, 0), cb(600, 0),
                                         cb(1000, 0)))
        out, err, code = self._integrate()
        self.assertEqual(code, 0, err)
        downloading = [d for d in self._documents(out)
                       if d.get('phase') == 'downloading']
        self.assertEqual(len(downloading), 1)
        self.assertEqual(downloading[0]['bytes'], 500)
        self.assertEqual(downloading[0]['total'], 0)
        self.assertEqual(self._progress_phases(self._documents(out)),
                         ['downloading', 'verifying', 'installing'])

    def test_plain_run_output_is_unchanged(self):
        self._patch_download()
        out, err, code = self.run_cmd(cli.cmd_integrate, [self.URL, '--yes'])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith('was integrated successfully\n'))
        for line in out.splitlines():
            self.assertFalse(line.startswith('{'))

    def test_failing_run_keeps_the_stream_well_formed(self):
        # a verification failure after progress was emitted: the in-process
        # runner reports the OperationError on stderr with exit 1; under
        # the real main() the error document is printed last, so the
        # progress lines must never corrupt the stream
        from omarchy_appimage.updaters import UpdateError

        def drive(cb):
            cb(len(self.body), len(self.body))
            raise UpdateError('digest mismatch')

        self._patch_download(drive)
        out, err, code = self._integrate()
        self.assertEqual(code, 1)
        self.assertIn('digest mismatch', err)
        docs = self._documents(out)
        self.assertEqual(self._progress_phases(docs), ['downloading'])
        self.assertEqual(docs[-1]['event'], 'progress')

    def test_static_url_progress_threads_through_download_plain(self):
        url = 'https://dl.example.org/MyApp.AppImage'
        total = len(self.body)
        self._patch_download(lambda cb: cb(total, total))
        out, err, code = self.run_cmd(
            cli.cmd_integrate, [url, '--yes', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(self._progress_phases(self._documents(out)),
                         ['downloading', 'verifying', 'installing'])
        self.assertEqual(self._documents(out)[-1]['result'], 'integrated')


if __name__ == '__main__':
    unittest.main()

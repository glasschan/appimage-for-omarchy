# test_updaters.py — update-manager unit tests (hermetic, no external net).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# HTTP traffic is served by an in-process ThreadingHTTPServer bound to
# 127.0.0.1:0; GitHub API payloads are faked by monkeypatching
# omarchy_appimage.net.fetch_json / fetch_text, so the suite never leaves
# the machine.

import hashlib
import os
import unittest
from unittest import mock

from helpers import (FakeXDGTestCase, FixtureHTTPServer,
                     make_elf_with_sections)

from omarchy_appimage import net
from omarchy_appimage.ini_config import Config
from omarchy_appimage.provider import AppImageListElement, InstalledStatus
from omarchy_appimage.updaters import (CodebergUpdater, ForgejoUpdater,
                                       GithubUpdater, GitlabUpdater,
                                       StaticFileUpdater, UpdateError,
                                       UpdateManagerChecker)


# ------------------------------------------------------------------- base

class UpdaterTestCase(FakeXDGTestCase):
    def setUp(self):
        super().setUp()
        self._servers = []
        self.addCleanup(self._close_servers)

    def _close_servers(self):
        for server in self._servers:
            server.close()

    def serve(self, routes: dict) -> FixtureHTTPServer:
        server = FixtureHTTPServer(routes)
        self._servers.append(server)
        return server

    def make_el(self, name='test.appimage', content=None) -> AppImageListElement:
        path = self.put_fake_appimage(name, content)
        return AppImageListElement(
            name=name, description='', provider='AppImage',
            installed_status=InstalledStatus.INSTALLED, file_path=path)

    def set_source(self, el, manager_name, data):
        Config.set_app_update_config(el, manager_name, data)


def zsync_body(url_line: str, sha1: str) -> bytes:
    header = f'zsync: 0.6.2\nFilename: f.appimage\nSHA-1: {sha1}\n'
    if url_line:
        header += f'URL: {url_line}\n'
    return (header + '\n[' + ('x' * 4096) + ']').encode()


# ------------------------------------------------------- StaticFileUpdater

class StaticFileUpdaterTests(UpdaterTestCase):
    def test_size_difference_means_update(self):
        el = self.make_el(content=b'\x7fELF\x02\x01\x01\x00\x41\x49\x02' + b'A' * 900)
        server = self.serve({'/f.appimage': (200, {}, b'B' * 1234)})
        manager = StaticFileUpdater(el=el)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': server.url('/f.appimage')})

        self.assertIs(manager.is_update_available(), True)
        self.assertEqual(manager.download_size, 1234)

    def test_same_size_means_up_to_date(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02' + b'A' * 900
        el = self.make_el(content=body)
        server = self.serve({'/f.appimage': (200, {}, body)})
        manager = StaticFileUpdater(el=el)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': server.url('/f.appimage')})

        self.assertIs(manager.is_update_available(), False)
        self.assertEqual(manager.download_size, len(body))

    def test_unreachable_url_is_not_an_update(self):
        # a dead port yields no content-length -> tri-state False, no raise
        el = self.make_el()
        manager = StaticFileUpdater(el=el)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': 'http://127.0.0.1:9/f.appimage'})
        self.assertIs(manager.is_update_available(), False)

    def test_zsync_sha1_mismatch_means_update(self):
        content = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
        el = self.make_el(content=content)
        other = 'f' * 40
        assert hashlib.sha1(content).hexdigest() != other
        server = self.serve({'/f.appimage.zsync':
                             (200, {}, zsync_body('', other))})
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/f.appimage.zsync'))
        self.assertIs(manager.is_update_available(), True)

    def test_zsync_sha1_match_means_up_to_date(self):
        content = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
        el = self.make_el(content=content)
        local_sha1 = hashlib.sha1(content).hexdigest()
        server = self.serve({'/f.appimage.zsync':
                             (200, {}, zsync_body('', local_sha1))})
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/f.appimage.zsync'))
        self.assertIs(manager.is_update_available(), False)

    def test_embedded_zsync_without_sha1_line_is_none(self):
        # no SHA-1 in the control file: the embedded source's state is
        # indeterminate (tri-state None), never a definite False —
        # --update still attempts the download (CONTRACT.md)
        content = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
        el = self.make_el(content=content)
        header = 'zsync: 0.6.2\nFilename: f.appimage\n\n[x]'
        server = self.serve({'/f.appimage.zsync': (200, {}, header.encode())})
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/f.appimage.zsync'))
        self.assertIsNone(manager.is_update_available())

    def test_download_writes_served_bytes(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02' + b'download' * 10
        server = self.serve({'/f.appimage': (200, {}, body)})
        el = self.make_el()
        manager = StaticFileUpdater(el=el)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': server.url('/f.appimage')})

        dest_dir = os.path.join(self.sandbox, 'dl')
        os.makedirs(dest_dir)
        fractions = []
        dest = manager.download(dest_dir, progress_cb=fractions.append)
        self.assertEqual(os.path.basename(dest), 'update.appimage')
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), body)
        self.assertTrue(fractions)  # progress was reported

    def test_download_resolves_absolute_zsync_url(self):
        body = b'appimage-bytes'
        server = self.serve({})
        server.routes.update({
            '/f.appimage.zsync': (200, {},
                                  zsync_body(server.url('/real.appimage'),
                                             'a' * 40)),
            '/real.appimage': (200, {}, body),
        })
        el = self.make_el()
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/f.appimage.zsync'))

        dest_dir = os.path.join(self.sandbox, 'dl2')
        os.makedirs(dest_dir)
        dest = manager.download(dest_dir)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), body)

    def test_download_resolves_relative_zsync_url(self):
        body = b'relative-bytes'
        server = self.serve({
            '/updates/f.appimage.zsync': (200, {},
                                          zsync_body('real.appimage',
                                                     'a' * 40)),
            '/updates/real.appimage': (200, {}, body),
        })
        el = self.make_el()
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/updates/f.appimage.zsync'))

        dest_dir = os.path.join(self.sandbox, 'dl3')
        os.makedirs(dest_dir)
        dest = manager.download(dest_dir)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), body)

    def test_download_zsync_without_url_line_strips_suffix(self):
        body = b'no-url-line'
        server = self.serve({
            '/updates/f.appimage.zsync': (200, {}, zsync_body('', 'a' * 40)),
            '/updates/f.appimage': (200, {}, body),
        })
        el = self.make_el()
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/updates/f.appimage.zsync'))

        dest_dir = os.path.join(self.sandbox, 'dl4')
        os.makedirs(dest_dir)
        dest = manager.download(dest_dir)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), body)

    def test_validate_config(self):
        manager = StaticFileUpdater(el=None)
        manager.validate_config({'url': 'https://host/f.appimage'})
        with self.assertRaises(UpdateError):
            manager.validate_config({'url': 'ftp://host/f.appimage'})
        with self.assertRaises(UpdateError):
            manager.validate_config({'url': ''})


# ----------------------------------------------------------- GithubUpdater

def make_release(assets, tag='v2.0.0', draft=False, prerelease=False):
    return {
        'tag_name': tag,
        'draft': draft,
        'prerelease': prerelease,
        'assets': [dict({
            'name': 'Foo-1.0-x86_64.appimage',
            'size': 100,
            'browser_download_url': 'https://host/Foo-1.0-x86_64.appimage',
            'digest': '',
        }, **a) for a in assets],
    }


class GithubUpdaterTests(UpdaterTestCase):
    CONFIG = {'repo': 'owner/repo', 'repo_filename': 'Foo-*.appimage',
              'allow_prereleases': False}

    def test_available_with_size_difference(self):
        el = self.make_el(content=b'\x7fELF' + b'\x00' * 96)
        self.set_source(el, 'GithubUpdater', dict(self.CONFIG))
        release = make_release([{'name': 'Foo-1.0-x86_64.appimage',
                                 'size': 4321}])
        with mock.patch.object(net, 'fetch_json', return_value=release) as mj:
            manager = GithubUpdater(el=el)
            self.assertIs(manager.is_update_available(), True)
        mj.assert_called_once_with(
            'https://api.github.com/repos/owner/repo/releases/latest')
        self.assertEqual(manager.available_version, 'v2.0.0')
        self.assertEqual(manager.download_size, 4321)

    def test_up_to_date_when_same_size(self):
        el = self.make_el(content=b'\x7fELF' + b'\x00' * 96)
        self.set_source(el, 'GithubUpdater', dict(self.CONFIG))
        release = make_release([{'size': 100}])
        with mock.patch.object(net, 'fetch_json', return_value=release):
            manager = GithubUpdater(el=el)
            self.assertIs(manager.is_update_available(), False)

    def test_sha256_digest_comparison(self):
        content = b'\x7fELF' + b'\x00' * 96
        el = self.make_el(content=content)
        self.set_source(el, 'GithubUpdater', dict(self.CONFIG))

        same = 'sha256:' + hashlib.sha256(content).hexdigest()
        with mock.patch.object(net, 'fetch_json',
                               return_value=make_release([{'digest': same,
                                                           'size': 999}])):
            manager = GithubUpdater(el=el)
            self.assertIs(manager.is_update_available(), False)

        other = 'sha256:' + hashlib.sha256(b'other').hexdigest()
        with mock.patch.object(net, 'fetch_json',
                               return_value=make_release([{'digest': other,
                                                           'size': 999}])):
            manager = GithubUpdater(el=el)
            self.assertIs(manager.is_update_available(), True)

    def test_draft_release_is_skipped(self):
        el = self.make_el()
        self.set_source(el, 'GithubUpdater', dict(self.CONFIG))
        release = make_release([{'name': 'Foo-1.0-x86_64.appimage'}],
                               draft=True)
        with mock.patch.object(net, 'fetch_json', return_value=release):
            manager = GithubUpdater(el=el)
            # source configured but state not determinable (tri-state None)
            self.assertIsNone(manager.is_update_available())

    def test_prereleases_when_allowed(self):
        el = self.make_el(content=b'\x7fELF' + b'\x00' * 50)
        config = dict(self.CONFIG, allow_prereleases=True)
        self.set_source(el, 'GithubUpdater', config)
        releases = [make_release([], tag='v3.0.0', prerelease=True),
                    make_release([{'name': 'Foo-2.0-x86_64.appimage',
                                   'size': 500}], tag='v2.0.0-rc1',
                                 prerelease=True)]
        with mock.patch.object(net, 'fetch_json',
                               return_value=releases) as mj:
            manager = GithubUpdater(el=el)
            self.assertIs(manager.is_update_available(), True)
        # the plain /releases endpoint is used (not /releases/latest)
        self.assertEqual(
            mj.call_args.args[0],
            'https://api.github.com/repos/owner/repo/releases')
        self.assertEqual(manager.available_version, 'v2.0.0-rc1')

    def test_arch_preference_picks_x86_64(self):
        el = self.make_el()
        self.set_source(el, 'GithubUpdater', dict(self.CONFIG))
        release = make_release([
            {'name': 'Foo-1.0-arm64.appimage', 'size': 1},
            {'name': 'Foo-1.0-x86_64.appimage', 'size': 2},
        ])
        with mock.patch.object(StaticFileUpdater, 'system_arch', 'x86_64'), \
                mock.patch.object(GithubUpdater, 'system_arch', 'x86_64'), \
                mock.patch.object(net, 'fetch_json', return_value=release):
            manager = GithubUpdater(el=el)
            target = manager.fetch_target_asset()
        self.assertEqual(target['asset']['name'], 'Foo-1.0-x86_64.appimage')

    def test_zsync_twin_asset_selected_when_embedded(self):
        el = self.make_el()
        release = make_release([
            {'name': 'Foo-1.0-x86_64.appimage.zsync', 'size': 300,
             'browser_download_url': 'https://host/Foo.zsync'},
            {'name': 'Foo-1.0-x86_64.appimage', 'size': 300,
             'browser_download_url': 'https://host/Foo.appimage'},
        ])
        embedded = 'gh-releases-zsync|owner|repo|latest|Foo-*.appimage.zsync'
        manager = GithubUpdater(el=el, embedded=embedded)
        with mock.patch.object(net, 'fetch_json', return_value=release):
            target = manager.fetch_target_asset()
        self.assertEqual(target['asset']['name'], 'Foo-1.0-x86_64.appimage')
        self.assertEqual(target['zsync']['name'],
                         'Foo-1.0-x86_64.appimage.zsync')
        self.assertEqual(manager.available_version, 'v2.0.0')

    def test_zsync_sha1_decides_availability(self):
        content = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
        el = self.make_el(content=content)
        release = make_release([
            {'name': 'Foo-1.0-x86_64.appimage.zsync', 'size': 300,
             'browser_download_url': 'https://host/Foo.zsync'},
            {'name': 'Foo-1.0-x86_64.appimage', 'size': 300,
             'browser_download_url': 'https://host/Foo.appimage'},
        ])
        embedded = 'gh-releases-zsync|owner|repo|latest|Foo-*.appimage.zsync'
        local_sha1 = hashlib.sha1(content).hexdigest()

        # zsync control file matching the local hash -> up to date
        with mock.patch.object(net, 'fetch_json', return_value=release), \
                mock.patch.object(net, 'fetch_text',
                                  return_value=zsync_body(
                                      '', local_sha1).decode()):
            manager = GithubUpdater(el=el, embedded=embedded)
            self.assertIs(manager.is_update_available(), False)

        # different hash -> update available
        with mock.patch.object(net, 'fetch_json', return_value=release), \
                mock.patch.object(net, 'fetch_text',
                                  return_value=zsync_body(
                                      '', 'e' * 40).decode()):
            manager = GithubUpdater(el=el, embedded=embedded)
            self.assertIs(manager.is_update_available(), True)

    def test_embedded_fetch_failure_is_none(self):
        # embedded source, releases unreachable: state not determinable
        # (tri-state None) so --update still attempts the download
        el = self.make_el()
        embedded = 'gh-releases-zsync|owner|repo|latest|Foo-*.zsync'
        manager = GithubUpdater(el=el, embedded=embedded)
        with mock.patch.object(net, 'fetch_json',
                               side_effect=net.NetworkError('down')):
            self.assertIsNone(manager.is_update_available())

    def test_get_url_data_from_release_url(self):
        manager = GithubUpdater(el=None)
        data = manager.get_url_data(
            'https://github.com/owner/repo/releases/download/v1/Foo.appimage')
        self.assertEqual(data, {'username': 'owner', 'repo': 'repo',
                                'release': 'latest',
                                'filename': 'Foo.appimage'})
        self.assertIsNone(manager.get_url_data('https://gitlab.com/a/b'))

    def test_validate_config(self):
        manager = GithubUpdater(el=None)
        manager.validate_config({'repo': 'owner/repo'})
        with self.assertRaises(UpdateError):
            manager.validate_config({'repo': 'justname'})


# ------------------------------------------------- Gitlab / Gitea family

class OtherManagerTests(UpdaterTestCase):
    def test_gitlab_validate_config(self):
        manager = GitlabUpdater(el=None)
        manager.validate_config(
            {'repo_url': 'https://gitlab.com/owner/project/-',
             'repo_filename': '*'})
        with self.assertRaises(UpdateError):
            manager.validate_config({'repo_url': 'http://gitlab.com/x/y'})

    def test_forgejo_validate_config(self):
        manager = ForgejoUpdater(el=None)
        manager.validate_config(
            {'repo_url': 'https://forge.example/owner/repo',
             'repo_filename': '*', 'allow_prereleases': False})
        with self.assertRaises(UpdateError):
            manager.validate_config(
                {'repo_url': 'https://forge.example/owner',
                 'repo_filename': '*', 'allow_prereleases': False})

    def test_codeberg_validate_config(self):
        manager = CodebergUpdater(el=None)
        manager.validate_config({'repo': 'owner/repo'})
        with self.assertRaises(UpdateError):
            manager.validate_config({'repo': 'owner'})

    def test_gitlab_is_update_available_size_check(self):
        el = self.make_el(content=b'\x7fELF' + b'\x00' * 40)
        self.set_source(el, 'GitlabUpdater',
                        {'repo_url': 'https://gitlab.com/owner/project',
                         'repo_filename': 'Foo*'})
        releases = [{'tag_name': 'v9',
                     'assets': {'links': [{
                         'name': 'Foo-1.0.appimage',
                         'direct_asset_url': 'https://gitlab.com/Foo'}]}}]
        with mock.patch.object(net, 'fetch_json', return_value=releases), \
                mock.patch.object(net, 'head_headers',
                                  return_value={'content-length': '9999'}):
            manager = GitlabUpdater(el=el)
            self.assertIs(manager.is_update_available(), True)
            self.assertEqual(manager.download_size, 9999)

    def test_codeberg_size_check(self):
        el = self.make_el(content=b'\x7fELF' + b'\x00' * 40)
        self.set_source(el, 'CodebergUpdater',
                        {'repo': 'owner/repo', 'repo_filename': 'Foo*',
                         'allow_prereleases': False})
        releases = [{'tag_name': 'v1',
                     'assets': [{'name': 'Foo-1.0.appimage', 'size': 7,
                                 'browser_download_url': 'https://cb/Foo'}]}]
        with mock.patch.object(net, 'fetch_json', return_value=releases):
            manager = CodebergUpdater(el=el)
            self.assertIs(manager.is_update_available(), True)


# -------------------------------------------------- UpdateManagerChecker

class CheckerTests(UpdaterTestCase):
    def test_embedded_gh_string_routes_to_github(self):
        content = make_elf_with_sections(
            {'.upd_info': b'gh-releases-zsync|neovim|neovim|latest|'
                          b'nvim-*.zsync\x00'})
        el = self.make_el(content=content)
        manager = UpdateManagerChecker.check_url_for_app(el)
        self.assertIsInstance(manager, GithubUpdater)
        self.assertTrue(manager.embedded)
        self.assertTrue(manager.embedded.startswith('gh-releases-zsync|'))

    def test_embedded_zsync_string_routes_to_static(self):
        content = make_elf_with_sections(
            {'.upd_info': b'zsync|http://example.com/f.appimage.zsync\x00'})
        el = self.make_el(content=content)
        manager = UpdateManagerChecker.check_url_for_app(el)
        self.assertIsInstance(manager, StaticFileUpdater)
        self.assertEqual(manager.get_embedded_url(),
                         'http://example.com/f.appimage.zsync')

    def test_no_source(self):
        el = self.make_el()
        self.assertIsNone(UpdateManagerChecker.check_url_for_app(el))

    def test_custom_config_beats_embedded(self):
        content = make_elf_with_sections(
            {'.upd_info': b'gh-releases-zsync|o|r|latest|*.zsync\x00'})
        el = self.make_el(content=content)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': 'https://example.com/f.appimage'})
        manager = UpdateManagerChecker.check_url_for_app(el)
        self.assertIsInstance(manager, StaticFileUpdater)
        self.assertIsNone(manager.embedded)   # not the embedded route
        self.assertEqual(manager.get_config()['url'],
                         'https://example.com/f.appimage')

    def test_unknown_stored_manager_is_none(self):
        el = self.make_el()
        Config.set_app_update_config(el, 'BogusUpdater', {'x': 'y'})
        self.assertIsNone(UpdateManagerChecker.check_url_for_app(el))

    def test_manager_metadata_shape(self):
        meta = UpdateManagerChecker.manager_metadata()
        self.assertEqual([m['name'] for m in meta],
                         ['StaticFileUpdater', 'GithubUpdater',
                          'GitlabUpdater', 'CodebergUpdater',
                          'ForgejoUpdater'])
        for m in meta:
            self.assertTrue(m['label'])
            for key in m['config_keys']:
                self.assertIsInstance(key, str)
        github = next(m for m in meta if m['name'] == 'GithubUpdater')
        self.assertEqual(github['config_keys'],
                         ['repo', 'repo_filename', 'allow_prereleases'])

    def test_get_model_by_name_roundtrip(self):
        for model in UpdateManagerChecker.get_models():
            self.assertIs(UpdateManagerChecker.get_model_by_name(model.name),
                          model)
        with self.assertRaises(UpdateError):
            UpdateManagerChecker.get_model_by_name('Nope')


if __name__ == '__main__':
    unittest.main()

# test_updaters.py — update-manager unit tests (hermetic, no external net).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# HTTP traffic is served by an in-process ThreadingHTTPServer bound to
# 127.0.0.1:0; GitHub API payloads are faked by monkeypatching
# omarchy_appimage.net.fetch_json / fetch_text, so the suite never leaves
# the machine.

import contextlib
import hashlib
import os
import unittest
from unittest import mock

from helpers import (FakeXDGTestCase, FixtureHTTPServer,
                     make_elf_with_sections)

from omarchy_appimage import net, updaters
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
        # the loopback fixture servers are plain http on a private
        # address: open net.py's test-only seam for the duration of each
        # test (production paths keep the guard on; there is no
        # environment variable — round 2 removed it)
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)
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

    def test_download_plain_url_refuses_without_downloading(self):
        # round 2, fail closed: a plain static URL exposes no digest, so
        # download() refuses BEFORE downloading anything
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02plain'
        server = self.serve({'/f.appimage': (200, {}, body)})
        el = self.make_el()
        manager = StaticFileUpdater(el=el)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': server.url('/f.appimage')})

        dest_dir = os.path.join(self.sandbox, 'dl-plain')
        os.makedirs(dest_dir)
        progress = mock.Mock()
        with mock.patch.object(net, 'download_to_file') as m_dl:
            with self.assertRaises(UpdateError) as caught:
                manager.download(dest_dir, progress_cb=progress)
        self.assertIn('no cryptographic digest', str(caught.exception))
        self.assertIn('static URL sources', str(caught.exception))
        m_dl.assert_not_called()          # nothing was downloaded
        progress.assert_not_called()
        self.assertEqual(os.listdir(dest_dir), [])  # no artifact written

    def test_download_matching_size_still_refuses(self):
        # a HEAD content-length that matches the artifact does not
        # authorize an install either — the refusal is unconditional
        # (round 1's size fallback is gone)
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02short'
        server = self.serve({'/f.appimage': (200, {}, body)})
        el = self.make_el()
        manager = StaticFileUpdater(el=el)
        self.set_source(el, 'StaticFileUpdater',
                        {'url': server.url('/f.appimage')})

        dest_dir = os.path.join(self.sandbox, 'dl-size')
        os.makedirs(dest_dir)
        with mock.patch.object(net, 'head_headers',
                               return_value={'content-length':
                                             str(len(body))}):
            with self.assertRaises(UpdateError):
                manager.download(dest_dir)
        self.assertEqual(os.listdir(dest_dir), [])

    def test_download_zsync_without_sha1_fails_closed(self):
        # round 2, fail closed: a zsync control file without a SHA-1
        # line has no cryptographic binding — the size fallback from
        # round 1 is gone, and the target is never downloaded
        body = b'zsync-no-sha1-body'
        header = 'zsync: 0.6.2\nFilename: f.appimage\n\n[x]'
        server = self.serve({
            '/f.appimage.zsync': (200, {}, header.encode()),
            '/f.appimage': (200, {}, body),
        })
        el = self.make_el()
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/f.appimage.zsync'))

        dest_dir = os.path.join(self.sandbox, 'dl-zsync-nosha1')
        os.makedirs(dest_dir)
        with mock.patch.object(net, 'download_to_file') as m_dl:
            with self.assertRaises(UpdateError) as caught:
                manager.download(dest_dir)
        self.assertIn('no SHA-1 line', str(caught.exception))
        m_dl.assert_not_called()          # target never downloaded
        self.assertEqual(os.listdir(dest_dir), [])

    def test_download_resolves_absolute_zsync_url(self):
        body = b'appimage-bytes'
        server = self.serve({})
        server.routes.update({
            '/f.appimage.zsync': (200, {},
                                  zsync_body(server.url('/real.appimage'),
                                             hashlib.sha1(body).hexdigest())),
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
                                                     hashlib.sha1(body)
                                                     .hexdigest())),
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
            '/updates/f.appimage.zsync': (200, {},
                                          zsync_body('', hashlib.sha1(body)
                                                     .hexdigest())),
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

    def test_validate_config_rejects_http(self):
        # https-only since the security hardening (plain http update
        # sources are rejected by validation); the seam is forced OFF
        # here — UpdaterTestCase opens it only for the fixture servers
        manager = StaticFileUpdater(el=None)
        with mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', False):
            with self.assertRaises(UpdateError):
                manager.validate_config({'url': 'http://host/f.appimage'})

    def test_download_zsync_sha1_mismatch_raises_and_unlinks(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02served'
        wrong = 'f' * 40
        assert hashlib.sha1(body).hexdigest() != wrong
        server = self.serve({
            '/f.appimage.zsync': (200, {}, zsync_body('', wrong)),
            '/f.appimage': (200, {}, body),
        })
        el = self.make_el()
        manager = StaticFileUpdater(el=el, embedded='zsync|'
                                    + server.url('/f.appimage.zsync'))

        dest_dir = os.path.join(self.sandbox, 'dl-sha1')
        os.makedirs(dest_dir)
        dest = os.path.join(dest_dir, 'update.appimage')
        with self.assertRaises(UpdateError):
            manager.download(dest_dir)
        self.assertFalse(os.path.exists(dest))  # partial artifact removed

    def test_garbage_content_length_header_is_ignored(self):
        # a garbage Content-Length must not crash the size check
        self.assertEqual(updaters._header_content_length(
            {'content-length': 'not-a-number'}), 0)
        self.assertEqual(updaters._header_content_length(
            {'content-length': None}), 0)
        self.assertEqual(updaters._header_content_length(
            {'content-length': '123'}), 123)


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

    def _download_github(self, asset_overrides, body,
                         embedded=None, fetch_text_value=None):
        """Serve `body` from the fixture server for a faked release asset;
        returns (manager, dest_path) after download() ran (or raised)."""
        url = self.serve({'/Foo.appimage': (200, {}, body)}).url(
            '/Foo.appimage')
        el = self.make_el()
        if embedded:
            manager = GithubUpdater(el=el, embedded=embedded)
        else:
            self.set_source(el, 'GithubUpdater', dict(self.CONFIG))
            manager = GithubUpdater(el=el)
        asset = {'browser_download_url': url, **asset_overrides}
        release = make_release([asset]) if not embedded else make_release([
            {'name': 'Foo-1.0-x86_64.appimage.zsync', 'size': 300,
             'browser_download_url': 'https://host/Foo.zsync'},
            {'name': 'Foo-1.0-x86_64.appimage',
             'browser_download_url': url, **asset_overrides},
        ])
        patches = [mock.patch.object(net, 'fetch_json', return_value=release)]
        if fetch_text_value is not None:
            patches.append(mock.patch.object(net, 'fetch_text',
                                             return_value=fetch_text_value))
        return manager, el, patches

    def test_download_digest_mismatch_raises_and_unlinks(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02asset-bytes'
        wrong = 'sha256:' + hashlib.sha256(b'other').hexdigest()
        manager, _el, patches = self._download_github(
            {'digest': wrong, 'size': 0}, body)

        dest_dir = os.path.join(self.sandbox, 'gh-digest')
        os.makedirs(dest_dir)
        dest = os.path.join(dest_dir, 'update.appimage')
        with self.assertRaises(UpdateError), contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            manager.download(dest_dir)
        self.assertFalse(os.path.exists(dest))  # partial artifact removed

    def test_download_digest_match_succeeds(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02asset-bytes'
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        manager, _el, patches = self._download_github(
            {'digest': digest, 'size': len(body)}, body)

        dest_dir = os.path.join(self.sandbox, 'gh-digest-ok')
        os.makedirs(dest_dir)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            dest = manager.download(dest_dir)
        self.assertTrue(os.path.exists(dest))

    def test_download_missing_digest_refuses_before_download(self):
        # round 2, fail closed: the asset's sha256 digest is required.
        # The API may return a null digest — it must refuse BEFORE any
        # byte is downloaded, not fall through to size-only acceptance.
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02asset-bytes'
        manager, _el, patches = self._download_github(
            {'digest': None, 'size': len(body)}, body)

        dest_dir = os.path.join(self.sandbox, 'gh-digest-null')
        os.makedirs(dest_dir)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with mock.patch.object(net, 'download_to_file') as m_dl:
                with self.assertRaises(UpdateError) as caught:
                    manager.download(dest_dir, progress_cb=mock.Mock())
        self.assertIn('no sha256 digest', str(caught.exception))
        m_dl.assert_not_called()          # nothing was downloaded
        self.assertEqual(os.listdir(dest_dir), [])  # no artifact written

    def test_download_malformed_digest_refuses(self):
        # a digest that does not match sha256:<64 hex> is as good as none
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02asset-bytes'
        manager, _el, patches = self._download_github(
            {'digest': 'sha256:not-a-digest', 'size': len(body)}, body)

        dest_dir = os.path.join(self.sandbox, 'gh-digest-bad')
        os.makedirs(dest_dir)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with mock.patch.object(net, 'download_to_file') as m_dl:
                with self.assertRaises(UpdateError) as caught:
                    manager.download(dest_dir)
        self.assertIn('no sha256 digest', str(caught.exception))
        m_dl.assert_not_called()
        self.assertEqual(os.listdir(dest_dir), [])

    def test_download_zsync_sha1_mismatch_raises_and_unlinks(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02zsync-bytes'
        embedded = 'gh-releases-zsync|owner|repo|latest|Foo-*.appimage.zsync'
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        manager, _el, patches = self._download_github(
            {'digest': digest, 'size': 0}, body, embedded=embedded,
            fetch_text_value=zsync_body('', 'e' * 40).decode())

        dest_dir = os.path.join(self.sandbox, 'gh-zsync')
        os.makedirs(dest_dir)
        dest = os.path.join(dest_dir, 'update.appimage')
        with self.assertRaises(UpdateError), contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            manager.download(dest_dir)
        self.assertFalse(os.path.exists(dest))

    def test_download_zsync_sha1_cross_check_passes(self):
        # belt and braces: the embedded flow verifies the digest AND the
        # zsync control file's SHA-1 for the same artifact
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02zsync-ok'
        embedded = 'gh-releases-zsync|owner|repo|latest|Foo-*.appimage.zsync'
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        manager, _el, patches = self._download_github(
            {'digest': digest, 'size': 0}, body, embedded=embedded,
            fetch_text_value=zsync_body(
                '', hashlib.sha1(body).hexdigest()).decode())

        dest_dir = os.path.join(self.sandbox, 'gh-zsync-ok')
        os.makedirs(dest_dir)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            dest = manager.download(dest_dir)
        self.assertTrue(os.path.exists(dest))
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), body)

    def test_download_size_mismatch_raises_and_unlinks(self):
        # size is no longer an authenticity binding, but a mismatch with
        # the advertised size still means metadata and artifact disagree
        # (cross-check on top of a verified digest)
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02sized'
        digest = 'sha256:' + hashlib.sha256(body).hexdigest()
        manager, _el, patches = self._download_github(
            {'digest': digest, 'size': 9999}, body)

        dest_dir = os.path.join(self.sandbox, 'gh-size')
        os.makedirs(dest_dir)
        dest = os.path.join(dest_dir, 'update.appimage')
        with self.assertRaises(UpdateError), contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            manager.download(dest_dir)
        self.assertFalse(os.path.exists(dest))


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

    def test_gitlab_download_refuses_without_digest(self):
        # round 2, fail closed: the GitLab API exposes no digests, so
        # installation refuses outright (nothing is downloaded); the
        # tri-state availability check above stays fully informative
        el = self.make_el()
        self.set_source(el, 'GitlabUpdater',
                        {'repo_url': 'https://gitlab.com/owner/project',
                         'repo_filename': 'Foo*'})

        dest_dir = os.path.join(self.sandbox, 'gl-refuse')
        os.makedirs(dest_dir)
        with mock.patch.object(net, 'download_to_file') as m_dl:
            with self.assertRaises(UpdateError) as caught:
                GitlabUpdater(el=el).download(dest_dir)
        self.assertIn('Gitlab', str(caught.exception))
        self.assertIn('no digest metadata', str(caught.exception))
        m_dl.assert_not_called()
        self.assertEqual(os.listdir(dest_dir), [])

    def test_codeberg_download_refuses_without_digest(self):
        el = self.make_el()
        self.set_source(el, 'CodebergUpdater',
                        {'repo': 'owner/repo', 'repo_filename': 'Foo*',
                         'allow_prereleases': False})

        dest_dir = os.path.join(self.sandbox, 'cb-refuse')
        os.makedirs(dest_dir)
        with mock.patch.object(net, 'download_to_file') as m_dl:
            with self.assertRaises(UpdateError) as caught:
                CodebergUpdater(el=el).download(dest_dir)
        self.assertIn('Codeberg', str(caught.exception))
        self.assertIn('no digest metadata', str(caught.exception))
        m_dl.assert_not_called()
        self.assertEqual(os.listdir(dest_dir), [])

    def test_forgejo_download_refuses_without_digest(self):
        el = self.make_el()
        self.set_source(el, 'ForgejoUpdater',
                        {'repo_url': 'https://forge.example/owner/repo',
                         'repo_filename': 'Foo*',
                         'allow_prereleases': False})

        dest_dir = os.path.join(self.sandbox, 'fj-refuse')
        os.makedirs(dest_dir)
        with mock.patch.object(net, 'download_to_file') as m_dl:
            with self.assertRaises(UpdateError) as caught:
                ForgejoUpdater(el=el).download(dest_dir)
        self.assertIn('Forgejo', str(caught.exception))
        self.assertIn('no digest metadata', str(caught.exception))
        m_dl.assert_not_called()
        self.assertEqual(os.listdir(dest_dir), [])


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

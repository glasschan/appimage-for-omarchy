# test_sources.py — issue #45: one source module per publisher, with the
# verification policy as a parameter of the fetch.

import os
import unittest
from unittest import mock

from helpers import FakeXDGTestCase, FixtureHTTPServer

from omarchy_appimage import cli, net, provider, updaters
from omarchy_appimage.updaters import (POLICY_REDUCED, POLICY_STRICT,
                                       StaticFileUpdater, UpdateError)

MAGIC = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02' + b'padding' * 6


def _element(path='/tmp/app/Fake App.appimage'):
    el = mock.Mock()
    el.file_path = path
    el.desktop_file_path = '/tmp/app/fakeapp.desktop'
    el.name = 'Fake App'
    return el


def _static(url):
    manager = StaticFileUpdater(_element())
    manager.get_config = lambda: {'url': url}
    return manager


class PolicyTests(FakeXDGTestCase):
    def setUp(self):
        super().setUp()
        # the loopback seam: the suite's fixture servers are http:// on
        # 127.0.0.1 and every production guard stays closed
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def test_an_unknown_policy_is_rejected(self):
        with self.assertRaises(UpdateError):
            updaters.check_policy('whatever')
        with self.assertRaises(UpdateError):
            _static('https://x/a.appimage').fetch('/tmp', 'whatever')

    def test_the_strict_policy_refuses_a_plain_url_before_any_byte(self):
        download = mock.Mock()
        with mock.patch.object(net, 'download_to_file', download):
            with self.assertRaises(UpdateError) as caught:
                updaters.download_plain('https://x/a.appimage', '/tmp',
                                        POLICY_STRICT)
        self.assertIn('unverified artifact', str(caught.exception))
        self.assertEqual(download.call_count, 0)

    def test_a_source_without_a_digest_is_refused_under_strict(self):
        download = mock.Mock()
        manager = _static('https://x/a.appimage')
        manager.checked = True
        self.assertIs(manager.installable(), False)
        with mock.patch.object(net, 'download_to_file', download):
            with self.assertRaises(UpdateError):
                manager.fetch('/tmp', POLICY_STRICT)
        self.assertEqual(download.call_count, 0)

    def test_the_reduced_policy_downloads_a_plain_url(self):
        server = FixtureHTTPServer({'/f.appimage': (200, {}, MAGIC)})
        self.addCleanup(server.close)
        manager = _static(server.url('/f.appimage'))
        manager.download_size = len(MAGIC)
        path = manager.fetch(self.sandbox, POLICY_REDUCED,
                             is_appimage=cli.appimage_provider.can_install_file)
        self.assertTrue(path.endswith('download.appimage'))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), MAGIC)

    def test_the_advertised_size_is_still_enforced(self):
        server = FixtureHTTPServer({'/f.appimage': (200, {}, MAGIC)})
        self.addCleanup(server.close)
        with self.assertRaises(UpdateError) as caught:
            updaters.download_plain(server.url('/f.appimage'), self.sandbox,
                                    POLICY_REDUCED, expected_size=len(MAGIC) + 1)
        self.assertIn('size mismatch', str(caught.exception))

    def test_a_zsync_control_file_without_a_sha1_line_is_strict_only(self):
        target = (200, {}, MAGIC)
        control = (200, {}, b'zsync: 0.6.2\nFilename: f.appimage\n\n')
        server = FixtureHTTPServer({'/f.appimage.zsync': control,
                                    '/f.appimage': target})
        self.addCleanup(server.close)

        download = mock.Mock()
        with mock.patch.object(net, 'download_to_file', download):
            with self.assertRaises(UpdateError) as caught:
                updaters.download_zsync(server.url('/f.appimage.zsync'),
                                        self.sandbox, POLICY_STRICT)
        self.assertIn('no SHA-1 line', str(caught.exception))
        self.assertEqual(download.call_count, 0)

        path = updaters.download_zsync(server.url('/f.appimage.zsync'),
                                       self.sandbox, POLICY_REDUCED)
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), MAGIC)

    def test_github_assets_demand_their_digest_under_both_policies(self):
        release = {'assets': [{'name': 'f.AppImage', 'size': len(MAGIC),
                               'digest': None,
                               'browser_download_url': 'https://x/f.AppImage'}]}
        download = mock.Mock()
        with mock.patch.object(net, 'fetch_json', return_value=release), \
                mock.patch.object(net, 'download_to_file', download):
            for policy in (POLICY_STRICT, POLICY_REDUCED):
                with self.subTest(policy=policy):
                    with self.assertRaises(UpdateError) as caught:
                        updaters.download_github_asset(
                            {'owner': 'o', 'repo': 'r', 'tag': 'v1',
                             'filename': 'f.AppImage', 'url': 'https://x/f'},
                            self.sandbox, policy)
                    self.assertIn('unverified artifact', str(caught.exception))
        self.assertEqual(download.call_count, 0)


class DescribeTests(unittest.TestCase):
    def test_describe_is_offline(self):
        manager = _static('https://x/a.appimage')

        def boom(*_args, **_kwargs):
            raise AssertionError('describe must not touch the network')

        with mock.patch.object(net, 'fetch_json', boom), \
                mock.patch.object(net, 'fetch_text', boom), \
                mock.patch.object(net, 'head_headers', boom):
            described = manager.describe()
        self.assertEqual(described['manager'], 'StaticFileUpdater')
        self.assertEqual(described['verification'], 'size')
        self.assertIsNone(described['installable'])
        self.assertFalse(described['embedded'])


class PreviewVerificationTests(unittest.TestCase):
    def test_the_preview_names_the_method_and_policy(self):
        github = cli._verification_preview('github')
        self.assertEqual(github['method'], 'sha256')
        # GitHub is fetched under the strict policy even for a pasted URL:
        # the preview must name the policy the fetch really uses.
        self.assertEqual(github['policy'], POLICY_STRICT)

        zsync = cli._verification_preview('zsync')
        self.assertEqual(zsync['method'], 'zsync-sha1')

        plain = cli._verification_preview('static')
        self.assertEqual(plain['method'], 'none')
        self.assertTrue(plain['reduced_assurance'])

    def test_the_vocabulary_is_the_listing_one(self):
        for kind in ('github', 'zsync', 'static', 'local'):
            with self.subTest(kind=kind):
                self.assertIn(cli._verification_preview(kind)['method'],
                              cli.VERIFICATIONS)


class UpdatePolicyTests(FakeXDGTestCase):
    def setUp(self):
        super().setUp()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def test_an_update_fetches_under_the_strict_policy(self):
        recorded = {}

        class RecordingManager:
            available_version = '2.0'
            download_size = 4

            def fetch(self, dest_dir, policy, progress_cb=None,
                      is_appimage=None):
                recorded['policy'] = policy
                raise UpdateError('stop here')

        el = cli.appimage_provider.create_list_element_from_file(
            self._installed_app())
        with self.assertRaises(Exception):
            cli.appimage_provider.update_from_url(
                RecordingManager(), el)
        self.assertEqual(recorded['policy'], POLICY_STRICT)

    def _installed_app(self):
        self.install_fake_app()
        path = os.path.join(self.managed_dir, 'fakeapp.appimage')
        # the ELF arch read needs a real header, not the two-byte stub
        with open(path, 'wb') as handle:
            handle.write(MAGIC)
        return path


if __name__ == '__main__':
    unittest.main()
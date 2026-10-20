# test_preview_integrate.py — --preview-integrate: the offline
# pre-install preview document (ticket #15).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# The preview must be OFFLINE by design and must never execute the
# AppImage: every test that exercises the verb booby-traps the network
# seams (net.* + raw sockets) and the subprocess seam, so any attempt at
# egress or execution fails the test instead of passing silently.

import json
import os
import shutil
import subprocess
import unittest
from unittest import mock

from helpers import (FakeXDGTestCase, download_fixture, make_minimal_elf)

from test_cli import InProcessCliMixin

from omarchy_appimage import cli, net
from omarchy_appimage.provider import AppImageListElement, InstalledStatus


class OfflinePreviewMixin:
    """Booby-traps every egress and execution seam: a preview that
    touches the network, opens a socket or spawns a process fails the
    test. ResourceWarnings stay fatal via the suite runner (-W
    error::ResourceWarning)."""

    def forbid_network_and_execution(self):
        for name in ('fetch_json', 'fetch_text', 'download_to_file',
                     'check_internet'):
            p = mock.patch.object(
                net, name,
                side_effect=AssertionError(
                    'preview must not touch the network: net.' + name))
            p.start()
            self.addCleanup(p.stop)

        sock = mock.patch(
            'socket.socket',
            side_effect=AssertionError('preview must not open sockets'))
        sock.start()
        self.addCleanup(sock.stop)

        popen = mock.patch.object(
            subprocess, 'Popen',
            side_effect=AssertionError('preview must never execute '
                                       'anything (subprocess.Popen called)'))
        popen.start()
        self.addCleanup(popen.stop)


class LocalPreviewTests(InProcessCliMixin, OfflinePreviewMixin,
                        FakeXDGTestCase):
    """--preview-integrate <local file>: name/version read in place from
    the embedded desktop entry, destination derived exactly like
    --integrate derives it, nothing executed, zero network."""

    def setUp(self):
        super().setUp()
        self.fixture = download_fixture()
        if self.fixture is None:
            self.skipTest('fixture download unavailable')
        self.forbid_network_and_execution()

    def _preview(self, path=None):
        return self.run_cmd(cli.cmd_preview_integrate,
                            [path or self.fixture, '--json'])

    def test_local_file_document(self):
        out, err, code = self._preview()
        self.assertEqual(code, 0, err)
        doc = json.loads(out)

        self.assertEqual(doc['schema_version'], 1)
        self.assertEqual(doc['result'], 'preview')
        self.assertTrue(doc['advisory'])

        self.assertEqual(doc['source'],
                         {'kind': 'local', 'input': self.fixture})

        # name + version read in place from the embedded desktop entry
        self.assertEqual(doc['app'], {'name': 'Neovim',
                                      'version': 'v0.11.3'})

        # destination derived the same way --integrate derives it
        self.assertEqual(doc['destination'], {
            'folder': self.managed_dir,
            'filename': 'neovim.appimage',
            'path': os.path.join(self.managed_dir, 'neovim.appimage'),
        })

        # a local file is not downloaded: no digest applies and that is
        # NOT "reduced assurance" (nothing is fetched)
        self.assertEqual(doc['verification']['method'], 'none')
        self.assertFalse(doc['verification']['reduced_assurance'])

        self.assertFalse(doc['already_integrated'])
        self.assertFalse(doc['name_conflict'])

        # nothing was installed or written anywhere
        self.assertFalse(os.path.exists(self.managed_dir))

    def test_local_file_never_executes_and_never_touches_the_network(self):
        # setUp() armed the traps; a successful preview proves none of
        # them fired (net.*, raw sockets, subprocess.Popen)
        out, err, code = self._preview()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'preview')

    def test_local_file_without_desktop_entry_still_previews(self):
        # a well-formed type-2 AppImage whose squashfs payload cannot be
        # read in place (here: a bare ELF header, no image attached):
        # the preview stays advisory — app is null, destination derives
        # from the file name exactly like install-time naming would
        fake = self.put_fake_appimage('mystery.appimage',
                                      content=make_minimal_elf())
        out, err, code = self._preview(fake)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertIsNone(doc['app'])
        self.assertEqual(doc['destination'], {
            'folder': self.managed_dir,
            'filename': 'gearlever_mystery.appimage',
            'path': os.path.join(self.managed_dir,
                                 'gearlever_mystery.appimage'),
        })

    def test_already_integrated_target_is_flagged(self):
        # integrate the file first (local file: no network involved) —
        # traps are NOT armed for this setup step, integrate may run its
        # best-effort update-desktop-database helper. Integrate MOVES the
        # original, so the preview below re-checks a byte-identical copy,
        # the way a user re-dropping the same file would.
        out, err, code = self.run_cmd(cli.cmd_integrate,
                                      [self.fixture, '--yes', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'integrated')

        copy_path = os.path.join(self.sandbox, 'again.appimage')
        shutil.copyfile(
            os.path.join(self.managed_dir, 'neovim.appimage'), copy_path)
        self.forbid_network_and_execution()

        out, err, code = self._preview(copy_path)
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertTrue(doc['already_integrated'])
        # the install would target the already-managed file
        self.assertTrue(doc['destination']['path'].startswith(
            self.managed_dir + os.sep))
        self.assertIn('Neovim', doc['app']['name'])

    def test_name_conflict_with_installed_app_is_flagged(self):
        # another version of the same app installed: preview must flag
        # that integrate would keep both (or replace with --replace),
        # and derive the non-clobbering suffixed destination
        self.install_fake_app(desktop_name='neovim', app_name='Neovim')

        out, err, code = self._preview()
        self.assertEqual(code, 0, err)
        doc = json.loads(out)
        self.assertFalse(doc['already_integrated'])
        self.assertTrue(doc['name_conflict'])
        # GearLever conflict suffixing: version-suffixed file name, so a
        # keep-both install never overwrites the installed copy
        self.assertEqual(
            doc['destination']['filename'],
            'neovim.appimage_v0_11_3.appimage')


class UrlPreviewTests(InProcessCliMixin, OfflinePreviewMixin,
                      FakeXDGTestCase):
    """--preview-integrate <url>: each host family classifies offline and
    reports the verification method that WILL apply at integrate time.
    Fakes assert no network request occurs (the same URL matrix as the
    integrate classification tests)."""

    GITHUB_URL = ('https://github.com/neovim/neovim/releases/download/'
                  'v4.5.6/nvim-4.5.6-linux-x86_64.AppImage')
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
        self.forbid_network_and_execution()

    def _preview(self, url):
        out, err, code = self.run_cmd(cli.cmd_preview_integrate,
                                      [url, '--json'])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def _assert_url_common(self, doc, url, kind):
        self.assertEqual(doc['result'], 'preview')
        self.assertEqual(doc['source'], {'kind': kind, 'input': url})
        # nothing is downloaded at preview time: no app metadata and no
        # final file name can be known offline
        self.assertIsNone(doc['app'])
        self.assertIsNone(doc['destination']['filename'])
        self.assertIsNone(doc['destination']['path'])
        self.assertEqual(doc['destination']['folder'], self.managed_dir)
        self.assertFalse(doc['already_integrated'])

    def test_github_url_reports_mandatory_sha256(self):
        doc = self._preview(self.GITHUB_URL)
        self._assert_url_common(doc, self.GITHUB_URL, 'github')
        self.assertEqual(doc['verification']['method'], 'github-sha256')
        self.assertFalse(doc['verification']['reduced_assurance'])

    def test_zsync_url_reports_sha1_method(self):
        doc = self._preview(self.ZSYNC_URL)
        self._assert_url_common(doc, self.ZSYNC_URL, 'zsync')
        self.assertEqual(doc['verification']['method'], 'zsync-sha1')
        self.assertFalse(doc['verification']['reduced_assurance'])

    def test_gitlab_url_is_reduced_assurance(self):
        doc = self._preview(self.GITLAB_URL)
        self._assert_url_common(doc, self.GITLAB_URL, 'gitlab')
        self.assertEqual(doc['verification']['method'], 'none')
        self.assertTrue(doc['verification']['reduced_assurance'])

    def test_codeberg_url_is_reduced_assurance(self):
        doc = self._preview(self.CODEBERG_URL)
        self._assert_url_common(doc, self.CODEBERG_URL, 'codeberg')
        self.assertEqual(doc['verification']['method'], 'none')
        self.assertTrue(doc['verification']['reduced_assurance'])

    def test_forgejo_url_is_reduced_assurance(self):
        doc = self._preview(self.FORGEJO_URL)
        self._assert_url_common(doc, self.FORGEJO_URL, 'forgejo')
        self.assertEqual(doc['verification']['method'], 'none')
        self.assertTrue(doc['verification']['reduced_assurance'])

    def test_static_url_is_reduced_assurance(self):
        doc = self._preview(self.STATIC_URL)
        self._assert_url_common(doc, self.STATIC_URL, 'static')
        self.assertEqual(doc['verification']['method'], 'none')
        self.assertTrue(doc['verification']['reduced_assurance'])

    def test_plain_text_url_preview(self):
        out, err, code = self.run_cmd(cli.cmd_preview_integrate,
                                      [self.GITHUB_URL])
        self.assertEqual(code, 0, err)
        self.assertIn('github', out)
        self.assertIn(self.GITHUB_URL, out)


class DestinationDerivationTests(FakeXDGTestCase):
    """The preview's destination block reuses the install-time naming
    helper verbatim (unit grain, like the auto-source tests)."""

    def _element(self, name):
        return AppImageListElement(
            name=name, description='', provider='AppImage',
            installed_status=InstalledStatus.NOT_INSTALLED,
            file_path=os.path.join(self.sandbox, 'x.appimage'))

    def test_conflict_by_display_name(self):
        self.install_fake_app(desktop_name='fakeapp', app_name='Fake App')
        conflict = cli._find_name_conflict(self._element('Fake App'))
        self.assertIsNotNone(conflict)
        self.assertTrue(conflict.file_path.startswith(self.managed_dir))

    def test_conflict_by_original_appimage_name(self):
        # an app installed with the keep-both suffix ('Name (version)')
        # is still matched through X-AppImage-Name
        appimage, desktop, icon = self.install_fake_app(
            desktop_name='fakeapp', app_name='Fake App (1.0)')
        with open(desktop, 'a') as f:
            f.write('X-AppImage-Name=Fake App\n')
        self.assertIsNotNone(cli._find_name_conflict(
            self._element('Fake App')))

    def test_no_conflict_for_a_different_app(self):
        self.install_fake_app(desktop_name='fakeapp', app_name='Fake App')
        self.assertIsNone(cli._find_name_conflict(
            self._element('Other App')))


class PreviewErrorTests(FakeXDGTestCase):
    """Unreadable paths and non-https URLs produce the existing typed
    error document (exit 1); wrong usage exits 2."""

    def test_missing_file_error_document(self):
        result = self.run_cli('--preview-integrate', '/nonexistent/x.appimage',
                              '--json')
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('no such file', payload['error'])

    def test_directory_error_document(self):
        result = self.run_cli('--preview-integrate', self.sandbox, '--json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['result'], 'error')

    def test_non_appimage_file_error_document(self):
        plain = os.path.join(self.sandbox, 'plain.txt')
        with open(plain, 'w') as f:
            f.write('not an appimage\n')
        result = self.run_cli('--preview-integrate', plain, '--json')
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('not an AppImage', payload['error'])

    def test_http_url_is_refused_like_integrate_egress(self):
        result = self.run_cli('--preview-integrate',
                              'http://example.com/x.AppImage', '--json')
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('https', payload['error'])

    def test_ftp_url_is_refused(self):
        result = self.run_cli('--preview-integrate',
                              'ftp://example.com/x.AppImage', '--json')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['result'], 'error')

    def test_missing_argument_exit_2(self):
        result = self.run_cli('--preview-integrate', '--json')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)['result'], 'error')

    def test_extra_argument_exit_2(self):
        result = self.run_cli('--preview-integrate', '/tmp/a', '/tmp/b',
                              '--json')
        self.assertEqual(result.returncode, 2)

    def test_unknown_option_exit_2(self):
        # an unknown -- option is a usage error (CONTRACT.md exit-code
        # table), never a silently ignored token
        fake = self.put_fake_appimage('opt.appimage',
                                      content=make_minimal_elf())
        result = self.run_cli('--preview-integrate', fake, '--bogus',
                              '--json')
        self.assertEqual(result.returncode, 2)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('--bogus', payload['error'])

    def test_json_stdout_is_pure(self):
        # exactly one JSON document, single trailing newline, nothing
        # else on stdout (CONTRACT.md output purity)
        fake = self.put_fake_appimage('pure.appimage',
                                      content=make_minimal_elf())
        result = self.run_cli('--preview-integrate', fake, '--json')
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.endswith('}\n'))
        self.assertFalse(result.stdout.endswith('}\n\n'))
        self.assertEqual(json.loads(result.stdout)['result'], 'preview')
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()

# test_extractor.py — extraction hardening tests: icon-name
# sanitization, _DirSource containment, extraction quotas. Hermetic.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from omarchy_appimage import extractor
from omarchy_appimage.squashfs import SquashfsError

PNG = b'\x89PNG\r\n\x1a\n'


class SafeIconBaseTests(unittest.TestCase):
    """The desktop entry's Icon value is untrusted; only a strict shape
    produces name-based candidates."""

    def test_plain_names_accepted(self):
        self.assertEqual(extractor._safe_icon_base('Foo'), 'Foo')
        self.assertEqual(extractor._safe_icon_base('org.example.App'),
                         'org.example.App')
        self.assertEqual(extractor._safe_icon_base('app_name-2.5'),
                         'app_name-2.5')

    def test_extension_stripped_before_check(self):
        self.assertEqual(extractor._safe_icon_base('foo.png'), 'foo')
        self.assertEqual(extractor._safe_icon_base('foo.svg'), 'foo')

    def test_traversal_and_absolute_names_rejected(self):
        for name in ('../../etc/passwd.png', '/abs/x.png', 'a/b.png',
                     '..png', '.png', '.hidden.png', 'foo/..',
                     '..', '.', '', 'foo\\bar.png'):
            self.assertEqual(extractor._safe_icon_base(name), '',
                             f'{name!r} must produce no candidates')

    def test_dots_only_names_rejected(self):
        self.assertEqual(extractor._safe_icon_base('..png'), '')


class _FakeSource:
    """Records every path handed to exists()/read() so tests can assert
    no unsafe candidate is ever probed."""

    def __init__(self, files: dict = None, symlinks: dict = None):
        self.files = files or {}          # path -> bytes
        self.symlinks = symlinks or {}    # path -> target
        self.probed = []

    def root_entries(self):
        return iter([])

    def is_symlink(self, path):
        return path in self.symlinks

    def resolve_symlink(self, path):
        return self.symlinks.get(path, '')

    def exists(self, path):
        self.probed.append(path)
        return path in self.files or path in self.symlinks

    def read(self, path, max_size=None):
        self.probed.append(path)
        return self.files.get(path)


class FindIconTests(unittest.TestCase):
    def test_unsafe_icon_name_probes_nothing_name_based(self):
        source = _FakeSource({'/.DirIcon': b'not-an-image'})
        # image_format() on the .DirIcon body: text/plain candidate path
        data = extractor._find_icon(source, '../../etc/passwd.png')
        self.assertIsNone(data)
        for probed in source.probed:
            self.assertFalse('..' in probed or probed.startswith('/etc'),
                             f'unsafe candidate probed: {probed}')

    def test_diricon_text_path_with_traversal_rejected(self):
        # .DirIcon as a text file holding a path with '..' components:
        # the path must never even be probed
        source = _FakeSource({'/.DirIcon': b'../../etc/evil.png'})
        data = extractor._find_icon(source, '')
        self.assertIsNone(data)
        for probed in source.probed:
            self.assertNotIn('..', probed.split('/'))

    def test_diricon_text_path_inside_image_accepted(self):
        source = _FakeSource({
            '/.DirIcon': b'share/icons/app.png',
            '/share/icons/app.png': PNG,
        })
        data = extractor._find_icon(source, '')
        self.assertEqual(data, PNG)
        self.assertIn('/share/icons/app.png', source.probed)


class DirSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='dirsource-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(os.chdir, os.getcwd())
        with open(os.path.join(self.root, 'good.png'), 'wb') as f:
            f.write(PNG)
        # a symlink pointing OUTSIDE the root
        os.symlink('/etc/passwd', os.path.join(self.root, 'evil.png'))
        # a symlink pointing INSIDE the root
        os.symlink('good.png', os.path.join(self.root, 'linked.png'))

    def _source(self):
        return extractor._DirSource(self.root)

    def test_symlink_escaping_root_does_not_exist(self):
        source = self._source()
        self.assertFalse(source.exists('/evil.png'))

    def test_symlink_escaping_root_read_is_none(self):
        source = self._source()
        self.assertIsNone(source.read('/evil.png'))

    def test_symlink_escaping_root_resolves_to_empty(self):
        source = self._source()
        self.assertEqual(source.resolve_symlink('/evil.png'), '')

    def test_traversal_path_is_nonexistent(self):
        source = self._source()
        self.assertFalse(source.exists('../outside.txt'))
        self.assertIsNone(source.read('../outside.txt'))
        self.assertIsNone(source.read('/../etc/passwd'))

    def test_regular_file_reads(self):
        source = self._source()
        self.assertTrue(source.exists('/good.png'))
        self.assertEqual(source.read('/good.png'), PNG)

    def test_symlink_inside_root_read_requires_explicit_resolution(self):
        # reads never follow a symlink: resolve_symlink + re-read instead
        source = self._source()
        self.assertTrue(source.is_symlink('/linked.png'))
        self.assertIsNone(source.read('/linked.png'))
        target = source.resolve_symlink('/linked.png')
        self.assertEqual(target, '/good.png')
        self.assertEqual(source.read(target), PNG)

    def test_max_size_is_enforced(self):
        source = self._source()
        self.assertIsNone(source.read('/good.png', max_size=2))

    def test_fifo_is_not_read(self):
        os.mkfifo(os.path.join(self.root, 'pipe'))
        source = self._source()
        self.assertTrue(source.exists('/pipe'))
        self.assertIsNone(source.read('/pipe'))


class SanitizeExtractedTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='sanitize-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_escaping_symlink_is_unlinked(self):
        os.symlink('/etc/passwd', os.path.join(self.root, 'evil.png'))
        with open(os.path.join(self.root, 'ok.png'), 'wb') as f:
            f.write(PNG)

        extractor._sanitize_extracted(self.root)

        self.assertFalse(os.path.lexists(os.path.join(self.root,
                                                      'evil.png')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'ok.png')))

    def test_inside_symlink_is_kept(self):
        with open(os.path.join(self.root, 'ok.png'), 'wb') as f:
            f.write(PNG)
        os.symlink('ok.png', os.path.join(self.root, 'linked.png'))

        extractor._sanitize_extracted(self.root)

        self.assertTrue(os.path.islink(os.path.join(self.root,
                                                    'linked.png')))

    def test_file_count_quota_backstop(self):
        for i in range(5):
            with open(os.path.join(self.root, f'f{i}'), 'wb') as f:
                f.write(b'x')
        with mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 3):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        # the over-quota tree was removed
        self.assertFalse(os.path.exists(self.root))

    def test_total_size_quota_backstop(self):
        for i in range(3):
            with open(os.path.join(self.root, f'f{i}'), 'wb') as f:
                f.write(b'x' * 100)
        with mock.patch.object(extractor, 'MAX_EXTRACT_BYTES', 128):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        self.assertFalse(os.path.exists(self.root))


class ListingQuotaTests(unittest.TestCase):
    UNSQUASHFS_LS = '''\
Parallel unsquashfs: Using 8 processors
4 inodes (6 blocks) to write

drwxr-xr-x root/root          46 2023-11-15 08:00 usr
-rw-r--r-- root/root        1234 2023-11-15 08:00 usr/bin/app
lrwxrwxrwx root/root          12 2023-11-15 08:00 .DirIcon
'''
    BSDAR_TVF = '''\
-rw-r--r--  0 root     root      1234 Jan 01  2023 usr/bin/app
drwxr-xr-x  0 root     root         0 Jan 01  2023 usr
lrwxr-xr-x  0 root     root        12 Jan 01  2023 .DirIcon -> ok.png
'''

    def test_parses_unsquashfs_listing(self):
        files, total = extractor._listing_totals(self.UNSQUASHFS_LS, 2)
        self.assertEqual(files, 2)          # regular file + symlink
        self.assertEqual(total, 1234)

    def test_parses_bsdtar_listing(self):
        files, total = extractor._listing_totals(self.BSDAR_TVF, 4)
        self.assertEqual(files, 2)
        self.assertEqual(total, 1234)

    def test_quota_enforcement_raises(self):
        with self.assertRaisesRegex(SquashfsError, 'quota'):
            extractor._enforce_quotas(extractor.MAX_EXTRACT_FILES + 1, 0)
        with self.assertRaisesRegex(SquashfsError, 'quota'):
            extractor._enforce_quotas(0, extractor.MAX_EXTRACT_BYTES + 1)
        # no raise within the bounds
        extractor._enforce_quotas(1, 1)

    def _garbage_appimage(self) -> str:
        """A file that fails the stdlib squashfs reader (so _open_source
        falls through to the external extractors)."""
        fd, path = tempfile.mkstemp(prefix='garbage-', suffix='.appimage')
        with os.fdopen(fd, 'wb') as f:
            f.write(b'\x00' * 64)
        self.addCleanup(os.unlink, path)
        return path

    def test_listing_failure_means_no_extraction(self):
        # fail closed: when the pre-extraction listing cannot be
        # produced, no extraction command may ever run
        path = self._garbage_appimage()
        calls = []

        def fake_run_command(command, **kwargs):
            calls.append(command)
            if '-ls' in command or '-tvf' in command:
                raise subprocess.CalledProcessError(1, command)
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')

        with mock.patch.object(extractor.elf, 'get_squashfs_offset',
                               return_value=0), \
                mock.patch.object(extractor.shutil, 'which',
                                  return_value='/usr/bin/tool'), \
                mock.patch.object(extractor, 'run_command',
                                  side_effect=fake_run_command):
            with self.assertRaisesRegex(SquashfsError, 'Could not extract'):
                extractor._open_source(path)
        self.assertTrue(calls)
        # only listing commands were tried, never an extraction
        self.assertTrue(all('-ls' in c or '-tvf' in c for c in calls))

    def test_quota_exceeded_on_listing_refuses_extraction(self):
        path = self._garbage_appimage()
        calls = []
        bomb = ''.join(f'-rw-r--r-- root/root  {i} 2023-01-01 00:00 f{i}\n'
                       for i in range(4))

        def fake_run_command(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0,
                             stdout=bomb.encode(), stderr=b'')

        with mock.patch.object(extractor.elf, 'get_squashfs_offset',
                               return_value=0), \
                mock.patch.object(extractor.shutil, 'which',
                                  return_value='/usr/bin/unsquashfs'), \
                mock.patch.object(extractor, 'run_command',
                                  side_effect=fake_run_command), \
                mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 2):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._open_source(path)
        # the listing ran, the extraction never did
        self.assertEqual([c for c in calls if '-ls' not in c
                          and '-tvf' not in c], [])


if __name__ == '__main__':
    unittest.main()

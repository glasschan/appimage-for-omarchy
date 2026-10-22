# test_extractor.py — extraction hardening tests: icon-name
# sanitization, _DirSource containment, extraction quotas. Hermetic.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omarchy_appimage import extractor
from omarchy_appimage.squashfs import SquashfsError

# A real 1x1 8-bit RGB PNG (IHDR/IDAT/IEND only, no ancillary chunk), so
# _find_icon's sanitiser accepts it and returns it byte-identically: the
# rebuild keeps those chunks verbatim and re-compresses nothing.
PNG = (b'\x89PNG\r\n\x1a\n'
       b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02'
       b'\x00\x00\x00\x90wS\xde'
       b'\x00\x00\x00\x0cIDATx\xdac```\x00\x00\x00\x04\x00\x01\xc8\xea\xeb\xf9'
       b'\x00\x00\x00\x00IEND\xaeB`\x82')


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

    def test_dirsource_follows_inroot_symlinked_root_icon(self):
        # a root-level icon that is a symlink inside the image (the
        # common /icon.png -> usr/share/icons/app.png layout) must be
        # found on the _DirSource path too — _VfsSource follows it, and
        # reads never go *through* a final symlink, so the candidate is
        # resolved first (containment enforced inside resolve_symlink)
        root = tempfile.mkdtemp(prefix='iconlink-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, 'usr', 'share', 'icons'))
        Path(root, 'usr', 'share', 'icons', 'app.png').write_bytes(PNG)
        os.symlink('usr/share/icons/app.png', os.path.join(root, 'icon.png'))

        source = extractor._DirSource(root)
        self.assertEqual(extractor._find_icon(source, 'icon'), PNG)


class DirSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='dirsource-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(os.chdir, os.getcwd())
        Path(self.root, 'good.png').write_bytes(PNG)
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
        Path(self.root, 'ok.png').write_bytes(PNG)

        extractor._sanitize_extracted(self.root)

        self.assertFalse(os.path.lexists(os.path.join(self.root,
                                                      'evil.png')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'ok.png')))

    def test_inside_symlink_is_kept(self):
        Path(self.root, 'ok.png').write_bytes(PNG)
        os.symlink('ok.png', os.path.join(self.root, 'linked.png'))

        extractor._sanitize_extracted(self.root)

        self.assertTrue(os.path.islink(os.path.join(self.root,
                                                    'linked.png')))

    def test_escaping_dir_symlink_is_unlinked(self):
        # os.walk reports directory symlinks in dirnames; they must not
        # survive sanitization (walk would otherwise descend out of root)
        os.makedirs(os.path.join(self.root, 'sub'))
        Path(self.root, 'sub', 'ok.png').write_bytes(PNG)
        os.symlink('/etc', os.path.join(self.root, 'sub', 'evil-dir'))

        extractor._sanitize_extracted(self.root)

        self.assertFalse(os.path.lexists(os.path.join(self.root, 'sub',
                                                      'evil-dir')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'sub',
                                                    'ok.png')))

    def test_directory_entries_count_toward_quota(self):
        # directories count as entries too: enough of them bust the
        # file-count quota even without any regular file
        for i in range(4):
            os.makedirs(os.path.join(self.root, f'd{i}'))
        with mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 2):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        self.assertFalse(os.path.exists(self.root))

    def test_file_count_quota_backstop(self):
        for i in range(5):
            Path(self.root, f'f{i}').write_bytes(b'x')
        with mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 3):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        # the over-quota tree was removed
        self.assertFalse(os.path.exists(self.root))

    def test_total_size_quota_backstop(self):
        for i in range(3):
            Path(self.root, f'f{i}').write_bytes(b'x' * 100)
        with mock.patch.object(extractor, 'MAX_EXTRACT_BYTES', 128):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        self.assertFalse(os.path.exists(self.root))


class SuperblockQuotaTests(unittest.TestCase):
    """Rounds 4-5: the external-extraction quota preflight reads the
    squashfs superblock (binary metadata, immune to entry-name injection)
    and any image that cannot be quota-checked is refused outright."""

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

    def _fake_reader(self, inode_count=3, bytes_used_total=1000,
                     error=None):
        reader = mock.Mock()
        reader.inode_count = inode_count
        reader.bytes_used_total = bytes_used_total
        if error is not None:
            return mock.Mock(side_effect=error)
        return mock.Mock(return_value=reader)

    def _open_source(self, path, reader_mock):
        # the stdlib reader fails first (as it does for images the
        # external extractors exist for), then the superblock preflight
        # and the external tools take over
        calls = []

        def fake_run_command(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')

        with mock.patch.object(extractor, '_VfsSource',
                               side_effect=SquashfsError('stdlib failed')), \
                mock.patch.object(extractor.elf, 'get_squashfs_offset',
                                  return_value=0), \
                mock.patch.object(extractor.shutil, 'which',
                                  return_value='/usr/bin/unsquashfs'), \
                mock.patch.object(extractor, 'run_command',
                                  side_effect=fake_run_command), \
                mock.patch.object(extractor, 'SquashfsReader', reader_mock):
            result = extractor._open_source(path), calls
        return result

    def test_unreadable_superblock_refuses_external_extraction(self):
        # fail closed: a superblock we cannot read means the image cannot
        # be quota-checked — no extraction command may ever run
        path = self._garbage_appimage()
        reader = self._fake_reader(error=SquashfsError('bad magic'))
        with self.assertRaisesRegex(SquashfsError, 'superblock'):
            self._open_source(path, reader)

    def test_non_elf_refuses_without_extraction_commands(self):
        # the offset lookup fails on real (unmocked) garbage: the
        # total-failure verdict fires before any command could run
        path = self._garbage_appimage()
        calls = []

        def fake_run_command(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')

        with mock.patch.object(extractor.shutil, 'which',
                               return_value='/usr/bin/unsquashfs'), \
                mock.patch.object(extractor, 'run_command',
                                  side_effect=fake_run_command):
            with self.assertRaisesRegex(SquashfsError, 'Could not extract'):
                extractor._open_source(path)
        self.assertEqual(calls, [])

    def test_superblock_preflight_passes_through_to_extraction(self):
        path = self._garbage_appimage()
        source, calls = self._open_source(
            path, self._fake_reader(inode_count=3, bytes_used_total=1000))
        self.assertIsNotNone(source)
        # exactly one command ran — the extraction; no listing preflight
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'unsquashfs')
        self.assertNotIn('-ls', calls[0])

    def test_superblock_quota_exceeded_refuses_extraction(self):
        path = self._garbage_appimage()
        reader = self._fake_reader(
            bytes_used_total=extractor.MAX_EXTRACT_BYTES + 1)
        with self.assertRaisesRegex(SquashfsError, 'quota'):
            self._open_source(path, reader)

    def test_vfs_path_gated_by_superblock_quota(self):
        # round-6: the stdlib VFS reader used to return BEFORE the quota
        # preflight — every path now passes the same gate first
        path = self._garbage_appimage()
        vfs = mock.Mock()
        with mock.patch.object(extractor, '_superblock_quota',
                               return_value=(extractor.MAX_EXTRACT_FILES + 1,
                                             0)), \
                mock.patch.object(extractor, '_VfsSource', vfs), \
                mock.patch.object(extractor.elf, 'get_squashfs_offset',
                                  return_value=0):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._open_source(path)
        vfs.assert_not_called()


if __name__ == '__main__':
    unittest.main()

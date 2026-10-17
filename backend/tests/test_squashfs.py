# test_squashfs.py — pure-Python squashfs reader tests (fixture-dependent
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
# parts skip gracefully when the download is unavailable).

import os
import struct
import tempfile
import unittest
import zlib
from unittest import mock

from helpers import (NVIM_SQUASHFS_OFFSET, FakeXDGTestCase, download_fixture)

from omarchy_appimage.squashfs import (SQUASHFS_MAGIC, SquashfsReader,
                                       SquashfsError)


def _fixture_or_skip(test):
    fixture = download_fixture()
    if fixture is None:
        test.skipTest('fixture download unavailable')
    return fixture


class SquashfsReaderTests(FakeXDGTestCase):
    def setUp(self):
        super().setUp()
        self.fixture = _fixture_or_skip(self)
        self.reader = SquashfsReader(self.fixture, NVIM_SQUASHFS_OFFSET)
        # never leave the fixture file handle open (ResourceWarning)
        self.addCleanup(self.reader.close)

    def test_superblock(self):
        self.assertEqual(self.reader.s_major, 4)
        self.assertEqual(self.reader.compression, 6)  # zstd
        self.assertEqual(self.reader.block_size, 131072)

    def test_root_listing(self):
        names = sorted(n for n, _, _ in self.reader.list_dir('/'))
        self.assertEqual(names,
                         ['.DirIcon', 'AppRun', 'nvim.desktop', 'nvim.png',
                          'usr'])

    def test_read_desktop_file(self):
        content = self.reader.read_file('/nvim.desktop')
        self.assertIn(b'[Desktop Entry]', content)
        self.assertIn(b'Name=Neovim', content)
        self.assertIn(b'X-AppImage-Version=v0.11.3', content)

    def test_readlink_chain(self):
        self.assertEqual(self.reader.readlink('/.DirIcon'), 'nvim.png')
        self.assertEqual(self.reader.readlink('/nvim.png'),
                         'usr/share/icons/hicolor/128x128/apps/nvim.png')

    def test_read_icon_through_symlinks(self):
        # reading /.DirIcon follows the whole chain to the real PNG
        data = self.reader.read_file('/.DirIcon')
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')

    def test_multiblock_file(self):
        # usr/bin/nvim is 10.8 MB = 83 zstd blocks without a fragment
        data = self.reader.read_file('/usr/bin/nvim')
        self.assertEqual(len(data), 10815784)

    def test_big_directory(self):
        # >256 entries: long-directory inode + straddling entries
        entries = sorted(n for n, _, _ in self.reader.list_dir(
            '/usr/share/nvim/runtime/syntax'))
        self.assertGreater(len(entries), 500)
        self.assertIn('python.vim', entries)
        self.assertIn('sh.vim', entries)

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self.reader.read_file('/does-not-exist')

    def test_bad_magic_raises(self):
        with self.assertRaises(SquashfsError):
            SquashfsReader(self.fixture, 0)  # ELF header is not squashfs


def _write_superblock(path, block_size=1 << 20, block_log=20, compression=1,
                      inode_count=1):
    """96-byte minimal superblock for parse-level tests (gzip)."""
    sb = struct.pack('<IIIIIHHHHHHQQQQQQQQ',
                     SQUASHFS_MAGIC, inode_count, 0, block_size, 0,
                     compression, block_log, 0, 0, 4, 0,
                     0, 0, 0, 0, 0, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(sb)


class SuperblockValidationTests(unittest.TestCase):
    """Round-6 review: superblock fields size every allocation and
    decompression cap downstream — hostile values are refused at parse
    time, before anything reads a block."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix='sb-', suffix='.img')
        os.close(fd)
        self.addCleanup(os.unlink, self.path)

    def test_rejects_implausible_block_size(self):
        _write_superblock(self.path, block_size=0x80000000, block_log=31)
        with self.assertRaisesRegex(SquashfsError, 'implausible block size'):
            SquashfsReader(self.path, 0)

    def test_rejects_block_log_mismatch(self):
        _write_superblock(self.path, block_size=1 << 20, block_log=19)
        with self.assertRaisesRegex(SquashfsError, 'block_log'):
            SquashfsReader(self.path, 0)

    def test_decompress_bomb_is_bounded(self):
        # a ~8 KB stored gzip frame that wants to expand to 8 MB: with
        # block_size = 1 MiB the bounded decompressor must refuse, not
        # allocate the full expansion
        _write_superblock(self.path, block_size=1 << 20, block_log=20)
        reader = SquashfsReader(self.path, 0)
        self.addCleanup(reader.close)
        bomb = zlib.compress(b'\x00' * 8_000_000)
        with self.assertRaisesRegex(SquashfsError, 'expands beyond'):
            reader._decompress(bomb, max_out=1 << 20)

    def test_decompress_accepts_legal_block(self):
        _write_superblock(self.path, block_size=1 << 20, block_log=20)
        reader = SquashfsReader(self.path, 0)
        self.addCleanup(reader.close)
        self.assertEqual(reader._decompress(zlib.compress(b'A' * 4096),
                                            max_out=1 << 20), b'A' * 4096)

    def test_metadata_block_over_8k_refused(self):
        # metadata blocks decompress to at most SQUASHFS_METADATA_SIZE per
        # the spec — a frame that wants more is hostile
        _write_superblock(self.path)
        comp = zlib.compress(b'B' * 9000)
        with open(self.path, 'ab') as f:
            f.write(struct.pack('<H', len(comp)))
            f.write(comp)
        reader = SquashfsReader(self.path, 0)
        self.addCleanup(reader.close)
        with self.assertRaisesRegex(SquashfsError, 'expands beyond'):
            reader._metadata_block(96, 0)


class AccumulationBoundTests(unittest.TestCase):
    """Round-7 review: directory listings, fragment tables, inode spans
    and the block caches are all driven by untrusted metadata fields —
    each accumulation gets an explicit ceiling."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix='acc-', suffix='.img')
        os.close(fd)
        self.addCleanup(os.unlink, self.path)
        _write_superblock(self.path)
        self.reader = SquashfsReader(self.path, 0)
        self.addCleanup(self.reader.close)

    def _dir_inode(self, file_size):
        from omarchy_appimage.squashfs import _Inode
        node = _Inode()
        node.type = 1  # DIR
        node.file_size = file_size
        node.offset = 0
        node.start_block = 0
        return node

    def test_dir_listing_over_cap_refused(self):
        from omarchy_appimage.squashfs import SQUASHFS_MAX_DIR_LISTING_BYTES
        node = self._dir_inode(SQUASHFS_MAX_DIR_LISTING_BYTES + 4)
        with self.assertRaisesRegex(SquashfsError, 'directory listing'):
            self.reader._read_dir(node)

    def test_dir_listing_within_cap_parses_empty(self):
        node = self._dir_inode(3)  # 3-byte listing header, no entries
        self.assertEqual(self.reader._read_dir(node), [])

    def test_fragment_count_over_cap_refused(self):
        from omarchy_appimage.squashfs import SQUASHFS_MAX_FRAGMENT_COUNT
        self.reader.fragment_count = SQUASHFS_MAX_FRAGMENT_COUNT + 1
        with self.assertRaisesRegex(SquashfsError, 'fragment table'):
            self.reader._fragment_table()

    def test_inode_span_over_cap_refused(self):
        from omarchy_appimage.squashfs import SQUASHFS_MAX_INODE_SPAN
        # a symlink whose declared target inflates the span past the cap:
        # the ref must be refused before _stream decompresses the chain
        node = mock.Mock()
        node.type = 3  # SYMLINK
        node.block_list = []
        node.file_size = SQUASHFS_MAX_INODE_SPAN * 16
        stream = mock.Mock(return_value=(b'\x00' * 64, 0))
        with mock.patch.object(self.reader, '_parse_inode',
                               return_value=node), \
                mock.patch.object(self.reader, '_stream', stream):
            with self.assertRaisesRegex(SquashfsError, 'inode spans'):
                self.reader._read_inode(0)
        self.assertEqual(stream.call_count, 1)  # first window only

    def test_meta_cache_is_bounded(self):
        from omarchy_appimage.squashfs import SQUASHFS_MAX_CACHED_META_BLOCKS
        # many distinct uncompressed metadata blocks at ascending offsets:
        # each 4-byte stored payload, hdr | 0x8000
        blob = b''.join(struct.pack('<HHHH', 0x8000 | 4, 1, 1, 1)
                        for _ in range(SQUASHFS_MAX_CACHED_META_BLOCKS + 500))
        with open(self.path, 'ab') as f:
            f.write(blob)
        table_start = 96
        for bo in range(0, (SQUASHFS_MAX_CACHED_META_BLOCKS + 500) * 8, 8):
            self.reader._metadata_block(table_start, bo)
        self.assertLessEqual(len(self.reader._meta_cache),
                             SQUASHFS_MAX_CACHED_META_BLOCKS)

    def test_stream_cache_is_bounded(self):
        from omarchy_appimage.squashfs import SQUASHFS_MAX_CACHED_STREAMS
        blob = b''.join(struct.pack('<HHHH', 0x8000 | 4, 1, 1, 1)
                        for _ in range(SQUASHFS_MAX_CACHED_STREAMS + 500))
        with open(self.path, 'ab') as f:
            f.write(blob)
        table_start = 96
        for bo in range(0, (SQUASHFS_MAX_CACHED_STREAMS + 500) * 8, 8):
            self.reader._stream(table_start, bo, 4)
        self.assertLessEqual(len(self.reader._stream_cache),
                             SQUASHFS_MAX_CACHED_STREAMS)


if __name__ == '__main__':
    unittest.main()

# test_squashfs.py — pure-Python squashfs reader tests (fixture-dependent
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
# parts skip gracefully when the download is unavailable).

import unittest

from helpers import (NVIM_SQUASHFS_OFFSET, FakeXDGTestCase, download_fixture)

from omarchy_appimage.squashfs import SquashfsReader, SquashfsError


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


if __name__ == '__main__':
    unittest.main()

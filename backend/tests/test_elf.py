# test_elf.py — ELF header parsing unit tests.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import unittest

from helpers import (FakeXDGTestCase, NVIM_SQUASHFS_OFFSET, download_fixture,
                     make_elf_with_sections, make_minimal_elf)

from omarchy_appimage import elf

NVIM_UPD_INFO = ('gh-releases-zsync|neovim|neovim|latest|'
                 'nvim-linux-x86_64.appimage.zsync')


class ElfSyntheticTests(unittest.TestCase):
    def test_type2_detection(self):
        path = self.addCleanup_tmp(make_minimal_elf())
        self.assertEqual(elf.get_appimage_type(path), '2')

    def test_type1_detection(self):
        header = bytearray(make_minimal_elf())
        header[8:11] = b'\x41\x49\x01'
        path = self.addCleanup_tmp(bytes(header))
        self.assertEqual(elf.get_appimage_type(path), '1')

    def test_not_an_appimage(self):
        header = bytearray(make_minimal_elf())
        header[8:11] = b'\x00\x00\x00'
        path = self.addCleanup_tmp(bytes(header))
        self.assertEqual(elf.get_appimage_type(path), '0')

    def test_offset_64le(self):
        header = make_minimal_elf(is64=True, e_shoff=0x1234,
                                  e_shentsize=64, e_shnum=10)
        path = self.addCleanup_tmp(header)
        self.assertEqual(elf.get_squashfs_offset(path), 0x1234 + 64 * 10)

    def test_offset_64be(self):
        header = make_minimal_elf(is64=True, big_endian=True, e_shoff=0x500,
                                  e_shentsize=64, e_shnum=2)
        path = self.addCleanup_tmp(header)
        self.assertEqual(elf.get_squashfs_offset(path), 0x500 + 128)

    def test_offset_32le(self):
        header = make_minimal_elf(is64=False, e_shoff=0x400,
                                  e_shentsize=40, e_shnum=5)
        path = self.addCleanup_tmp(header)
        self.assertEqual(elf.get_squashfs_offset(path), 0x400 + 200)

    def test_offset_rejects_non_elf(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.bin', delete=False) as f:
            f.write(b'MZ garbage')
            path = f.name
        self.addCleanup(os.unlink, path)
        with self.assertRaises(ValueError):
            elf.get_squashfs_offset(path)

    def test_arch_x86_64(self):
        path = self.addCleanup_tmp(make_minimal_elf(e_machine=0x3E))
        self.assertEqual(elf.get_elf_arch(path), 'x86_64')

    def test_arch_aarch64(self):
        path = self.addCleanup_tmp(make_minimal_elf(e_machine=0xB7))
        self.assertEqual(elf.get_elf_arch(path), 'aarch64')

    def test_arch_unknown(self):
        path = self.addCleanup_tmp(make_minimal_elf(e_machine=0x1234))
        self.assertEqual(elf.get_elf_arch(path), 'UNKNOWN')

    def test_upd_info_extracts_string(self):
        data = make_elf_with_sections({
            '.upd_info': b'gh-releases-zsync|app|repo|latest|f-*.zsync\x00',
        })
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path),
                         'gh-releases-zsync|app|repo|latest|f-*.zsync')

    def test_upd_info_nul_padded(self):
        # real AppImages pad .upd_info to a fixed size with NUL bytes
        data = make_elf_with_sections({
            '.upd_info': b'zsync|http://example.com/f.appimage.zsync'
                         + b'\x00' * 100,
        })
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path),
                         'zsync|http://example.com/f.appimage.zsync')

    def test_upd_info_missing_section(self):
        data = make_elf_with_sections({'.note.test': b'whatever'})
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path), '')

    def test_upd_info_empty_section(self):
        data = make_elf_with_sections({'.upd_info': b''})
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path), '')

    def test_upd_info_32bit_header(self):
        data = make_elf_with_sections(
            {'.upd_info': b'gh-releases-zsync|o|r|latest|*.zsync\x00'},
            is64=False)
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path),
                         'gh-releases-zsync|o|r|latest|*.zsync')

    def test_upd_info_big_endian(self):
        data = make_elf_with_sections(
            {'.upd_info': b'zsync|http://h/f.zsync\x00'}, big_endian=True)
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path), 'zsync|http://h/f.zsync')

    def test_upd_info_not_an_elf(self):
        path = self.addCleanup_tmp(b'MZ definitely not ELF')
        self.assertEqual(elf.read_upd_info(path), '')

    def test_upd_info_alongside_other_sections(self):
        data = make_elf_with_sections({
            '.note.test': b'\x00\x01\x02',
            '.upd_info': b'gh-releases-zsync|o|r|latest|*.zsync\x00',
            '.data': b'x' * 16,
        })
        path = self.addCleanup_tmp(data)
        self.assertEqual(elf.read_upd_info(path),
                         'gh-releases-zsync|o|r|latest|*.zsync')

    def test_upd_info_truncated_section_table(self):
        # a file cut off inside the section header table: the last entries
        # read short, the loop stops early and e_shstrndx may point past
        # the collected entries — must return empty, never raise IndexError
        data = make_elf_with_sections({
            '.note.test': b'\x00\x01\x02',
            '.upd_info': b'gh-releases-zsync|o|r|latest|*.zsync\x00',
        })
        truncated = data[:len(data) - 40]   # mid-way into the last shdr
        path = self.addCleanup_tmp(truncated)
        self.assertEqual(elf._read_sections(path), [])
        self.assertEqual(elf.read_upd_info(path), '')

    def addCleanup_tmp(self, data: bytes) -> str:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.appimage',
                                         delete=False) as f:
            f.write(data)
            path = f.name
        self.addCleanup(os.unlink, path)
        return path


class ElfFixtureTests(FakeXDGTestCase):
    def test_offset_matches_gearlever_script(self):
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        self.assertEqual(elf.get_squashfs_offset(fixture),
                         NVIM_SQUASHFS_OFFSET)

    def test_type_and_arch(self):
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        self.assertEqual(elf.get_appimage_type(fixture), '2')
        self.assertEqual(elf.get_elf_arch(fixture), 'x86_64')

    def test_upd_info_fixture(self):
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        self.assertEqual(elf.read_upd_info(fixture), NVIM_UPD_INFO)


if __name__ == '__main__':
    unittest.main()

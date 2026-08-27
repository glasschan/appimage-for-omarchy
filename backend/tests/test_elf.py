# test_elf.py — ELF header parsing unit tests.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import unittest

from helpers import (FakeXDGTestCase, NVIM_SQUASHFS_OFFSET, download_fixture,
                     make_minimal_elf)

from omarchy_appimage import elf


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


if __name__ == '__main__':
    unittest.main()

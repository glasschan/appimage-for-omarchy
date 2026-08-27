# elf.py — ELF header parsing for AppImages (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# GearLever computed the embedded squashfs offset by shelling out to
# `get_appimage_offset` (od + awk) or `file` for the architecture; both
# are reimplemented here with `struct` so that no external tools are
# required. The offset logic mirrors GearLever's
# build-aux/get_appimage_offset.sh: the squashfs image starts right
# after the last ELF section header, i.e. e_shoff + e_shentsize*e_shnum.

import struct

APPIMAGE_TYPE1_MAGIC = b'\x41\x49\x01'  # 0x414901
APPIMAGE_TYPE2_MAGIC = b'\x41\x49\x02'  # 0x414902


def read_header(path: str, length: int = 64) -> bytes:
    with open(path, 'rb') as f:
        return f.read(length)


def get_appimage_type(path: str) -> str:
    """Return '1', '2' or '0' (not an AppImage), like GearLever's
    AppImageProvider.get_appimage_type()."""
    magic = read_header(path, 11)[8:11]
    if magic == APPIMAGE_TYPE1_MAGIC:
        return '1'
    if magic == APPIMAGE_TYPE2_MAGIC:
        return '2'
    return '0'


def get_squashfs_offset(path: str) -> int:
    """Return the byte offset of the embedded squashfs image of a
    type-2 AppImage (the end of the ELF section header table)."""
    header = read_header(path)

    if header[0:4] != b'\x7fELF':
        raise ValueError(f'{path}: not an ELF executable')

    ei_class = header[4]   # 1 = 32-bit, 2 = 64-bit
    ei_data = header[5]    # 1 = little-endian, 2 = big-endian

    if ei_class == 2 and ei_data == 1:
        e_shoff = struct.unpack_from('<Q', header, 0x28)[0]
        e_shentsize, e_shnum = struct.unpack_from('<HH', header, 0x3A)
    elif ei_class == 2 and ei_data == 2:
        e_shoff = struct.unpack_from('>Q', header, 0x28)[0]
        e_shentsize, e_shnum = struct.unpack_from('>HH', header, 0x3A)
    elif ei_class == 1 and ei_data == 1:
        e_shoff = struct.unpack_from('<I', header, 0x20)[0]
        e_shentsize, e_shnum = struct.unpack_from('<HH', header, 0x2E)
    elif ei_class == 1 and ei_data == 2:
        e_shoff = struct.unpack_from('>I', header, 0x20)[0]
        e_shentsize, e_shnum = struct.unpack_from('>HH', header, 0x2E)
    else:
        raise ValueError(f'{path}: unsupported ELF class/data encoding')

    return e_shoff + e_shentsize * e_shnum


def get_elf_arch(path: str) -> str:
    """Return 'x86_64', 'aarch64' or 'UNKNOWN' from the ELF machine type.

    Replaces GearLever's `file --brief` subprocess (AppImageProvider.
    get_elf_arch) with a direct header read."""
    header = read_header(path)
    if header[0:4] != b'\x7fELF':
        return 'UNKNOWN'

    endian = '>' if header[5] == 2 else '<'
    e_machine = struct.unpack_from(endian + 'H', header, 0x12)[0]

    if e_machine in (0x3E, 0x07):  # EM_X86_64, EM_86064 / EM_X86_64-alt
        return 'x86_64'
    if e_machine == 0xB7:          # EM_AARCH64
        return 'aarch64'
    return 'UNKNOWN'

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


def _read_sections(path: str):
    """Parse the ELF section table into [(name, sh_offset, sh_size), ...].

    Returns an empty list when the file is not an ELF or has no section
    table. Handles extended numbering (e_shnum == 0 with the real count in
    section 0's sh_size, e_shstrndx == SHN_XINDEX with the real index in
    section 0's sh_link) the way readelf does."""
    try:
        with open(path, 'rb') as f:
            header = f.read(64)
            if len(header) < 64 or header[0:4] != b'\x7fELF':
                return []

            ei_class = header[4]   # 1 = 32-bit, 2 = 64-bit
            ei_data = header[5]    # 1 = little-endian, 2 = big-endian
            if ei_class not in (1, 2) or ei_data not in (1, 2):
                return []
            fmt = '<' if ei_data == 1 else '>'

            if ei_class == 2:
                e_shoff = struct.unpack_from(fmt + 'Q', header, 0x28)[0]
                e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
                    fmt + 'HHH', header, 0x3A)
                off_fmt, off_at, size_at, link_at = fmt + 'Q', 24, 32, 40
            else:
                e_shoff = struct.unpack_from(fmt + 'I', header, 0x20)[0]
                e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(
                    fmt + 'HHH', header, 0x2E)
                off_fmt, off_at, size_at, link_at = fmt + 'I', 16, 20, 24

            if e_shoff == 0 or e_shentsize == 0:
                return []

            def entry(index: int):
                f.seek(e_shoff + index * e_shentsize)
                raw = f.read(e_shentsize)
                if len(raw) < e_shentsize:
                    return None
                sh_name = struct.unpack_from(fmt + 'I', raw, 0)[0]
                sh_offset = struct.unpack_from(off_fmt, raw, off_at)[0]
                sh_size = struct.unpack_from(off_fmt, raw, size_at)[0]
                sh_link = struct.unpack_from(fmt + 'I', raw, link_at)[0]
                return (sh_name, sh_offset, sh_size, sh_link)

            first = entry(0)
            if first is None:
                return []

            # Extended numbering escapes live in section 0.
            if e_shnum == 0:
                e_shnum = first[2]                 # sh_size of section 0
            if e_shstrndx == 0xffff:               # SHN_XINDEX
                e_shstrndx = first[3]              # sh_link of section 0
            if e_shnum == 0 or e_shnum > 0x10000 or e_shstrndx >= e_shnum:
                return []

            entries = []
            for i in range(e_shnum):
                e = entry(i)
                if e is None:
                    break
                entries.append(e)

            # A truncated table can leave fewer entries than e_shstrndx
            # names; bail out instead of raising IndexError below.
            if e_shstrndx >= len(entries):
                return []

            # Resolve section names through the section-name string table.
            _, str_off, str_size, _ = entries[e_shstrndx]
            f.seek(str_off)
            names = f.read(str_size)

            sections = []
            for sh_name, sh_offset, sh_size, _ in entries:
                if sh_name >= len(names):
                    sections.append(('', sh_offset, sh_size))
                    continue
                end = names.find(b'\x00', sh_name)
                name = names[sh_name:end if end != -1 else len(names)]
                sections.append((name.decode('utf-8', 'replace'),
                                 sh_offset, sh_size))
            return sections
    except OSError:
        return []


def read_upd_info(path: str) -> str:
    """Return the AppImage update-information string from the ELF
    `.upd_info` section (e.g. 'gh-releases-zsync|owner|repo|latest|*.zsync'),
    or '' when the section is missing or empty.

    Replaces GearLever's `readelf --string-dump=.upd_info` subprocess
    (UpdateManagerChecker.check_app_embedded_url) with a direct section
    read; the AppImageSpec puts the update information in this section of
    the runtime header."""
    for name, offset, size in _read_sections(path):
        if name != '.upd_info':
            continue
        try:
            with open(path, 'rb') as f:
                f.seek(offset)
                data = f.read(size)
        except OSError:
            return ''
        return data.split(b'\x00', 1)[0].decode('utf-8', 'replace').strip()
    return ''

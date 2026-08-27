# squashfs.py — minimal read-only SquashFS 4.0 reader (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# The SquashFS on-disk layout handled here follows the SquashFS 4.0 spec
# (https://github.com/plougher/squashfs-tools/blob/master/README-4.0);
# GearLever delegated this job to external extractors (7zz / unsquashfs /
# appimage-extract), which are not guaranteed to exist on a stock Arch /
# Omarchy system. This reader only extracts the few files needed for
# desktop integration (.desktop entries and icons) and it NEVER executes
# the AppImage.
#
# Supported compressions: gzip (zlib), xz (lzma) and zstd
# (compression.zstd on Python >= 3.14, otherwise the `zstd` CLI).
# Unsupported images raise UnsupportedCompression and the caller can fall
# back to external tools (see extractor.py).

import os
import struct
import subprocess
import zlib
import lzma
import logging

SQUASHFS_MAGIC = 0x73717368  # 'hsqs' little-endian

COMP_GZIP = 1
COMP_LZMA = 2
COMP_LZO = 3
COMP_XZ = 4
COMP_LZ4 = 5
COMP_ZSTD = 6

SQUASHFS_METADATA_SIZE = 8192
SQUASHFS_COMPRESSED_BIT_BLOCK = 1 << 24
SQUASHFS_INVALID_FRAG = 0xFFFFFFFF

# on-disk inode types (squashfs_fs.h, SquashFS 4.x)
DIR = 1
REG = 2
SYMLINK = 3
BLKDEV = 4
CHRDEV = 5
FIFO = 6
SOCKET = 7
LDIR = 8
LREG = 9
LSYMLINK = 10


class SquashfsError(Exception):
    pass


class UnsupportedCompression(SquashfsError):
    pass


def _zstd_available():
    try:
        from compression import zstd  # noqa: F401  Python >= 3.14 (PEP 784)
        return 'stdlib'
    except ImportError:
        pass

    from shutil import which
    if which('zstd'):
        return 'cli'

    return None


_ZSTD_MODE = None


def _zstd_decompress(data: bytes) -> bytes:
    global _ZSTD_MODE
    if _ZSTD_MODE is None:
        _ZSTD_MODE = _zstd_available()

    if _ZSTD_MODE == 'stdlib':
        from compression import zstd
        # SquashFS zstd frames are streamed without the content-size header;
        # ZstdDecompressor.decompress() handles that.
        return zstd.ZstdDecompressor().decompress(data)

    if _ZSTD_MODE == 'cli':
        result = subprocess.run(
            ['zstd', '-d', '-c', '-q'],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise SquashfsError(
                'zstd CLI failed: ' + result.stderr.decode(errors='replace'))
        return result.stdout

    raise UnsupportedCompression('zstd decompression not available '
                                 '(needs Python >= 3.14 or the zstd CLI)')


class _Inode:
    __slots__ = ('type', 'start_block', 'nlink', 'file_size', 'offset',
                 'parent', 'fragment', 'block_list', 'target', 'inode_number')

    def __init__(self):
        self.type = 0
        self.start_block = 0
        self.nlink = 0
        self.file_size = 0
        self.offset = 0
        self.parent = 0
        self.fragment = SQUASHFS_INVALID_FRAG
        self.block_list = []
        self.target = None
        self.inode_number = 0


class SquashfsReader:
    """Read files out of a SquashFS 4.0 image embedded in an AppImage.

    Only little-endian images are supported (AppImages always are).
    """

    def __init__(self, path: str, offset: int):
        self.path = path
        self.offset = offset
        self._meta_cache = {}
        self._stream_cache = {}
        self._fragment_cache = None
        self._f = open(path, 'rb')

        try:
            self._parse_superblock()
        except BaseException:
            self._f.close()
            raise

    def _parse_superblock(self):
        sb = self._read_at(self.offset, 96)
        (self.magic, self.inode_count, _mkfs, self.block_size,
         self.fragment_count, self.compression, self.block_log,
         _flags, _no_ids, self.s_major, self.s_minor, self.root_inode_ref,
         _bytes_used, _id_table, _xattr, self.inode_table_start,
         self.directory_table_start, self.fragment_table_start,
         _lookup_table) = struct.unpack('<IIIIIHHHHHHQQQQQQQQ', sb[:0x60])

        if self.magic != SQUASHFS_MAGIC:
            raise SquashfsError('not a little-endian squashfs image '
                                f'(magic=0x{self.magic:08x})')
        if self.s_major != 4:
            raise SquashfsError(f'unsupported squashfs version {self.s_major}')
        if self.compression not in (COMP_GZIP, COMP_XZ, COMP_ZSTD):
            names = {COMP_LZMA: 'lzma', COMP_LZO: 'lzo', COMP_LZ4: 'lz4'}
            name = names.get(self.compression, str(self.compression))
            raise UnsupportedCompression(f'squashfs compression "{name}" '
                                         'is not supported by the stdlib reader')

    # ------------------------------------------------------------------ io

    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _read_at(self, absolute: int, size: int) -> bytes:
        self._f.seek(absolute)
        data = self._f.read(size)
        if len(data) != size:
            raise SquashfsError(f'short read at 0x{absolute:x}')
        return data

    def _decompress(self, data: bytes) -> bytes:
        if self.compression == COMP_GZIP:
            return zlib.decompress(data)
        if self.compression == COMP_XZ:
            return lzma.decompress(data)
        if self.compression == COMP_ZSTD:
            return _zstd_decompress(data)
        raise UnsupportedCompression(f'compression id {self.compression}')

    def _metadata_block(self, table_start: int, block_offset: int):
        """Return (decompressed data, next block offset)."""
        key = (table_start, block_offset)
        cached = self._meta_cache.get(key)
        if cached is not None:
            return cached

        hdr = struct.unpack('<H', self._read_at(
            self.offset + table_start + block_offset, 2))[0]
        if hdr & 0x8000:
            stored = hdr & 0x7FFF
            data = self._read_at(self.offset + table_start + block_offset + 2,
                                 stored)
        else:
            stored = hdr
            raw = self._read_at(self.offset + table_start + block_offset + 2,
                                stored)
            data = self._decompress(raw)

        result = (data, block_offset + 2 + stored)
        self._meta_cache[key] = result
        return result

    def _stream(self, table_start: int, block_offset: int, min_end: int):
        """Concatenate chained metadata blocks until they cover `min_end`
        bytes.  Inodes and directory entries are packed as continuous
        streams and may straddle metadata block boundaries.

        Returns (buffer, next_block_offset)."""
        key = (table_start, block_offset)
        cached = self._stream_cache.get(key)
        if cached is not None and len(cached[0]) >= min_end:
            return cached

        buf = b''
        bo = block_offset
        while len(buf) < min_end:
            data, nb = self._metadata_block(table_start, bo)
            buf += data
            bo = nb

        self._stream_cache[key] = (buf, bo)
        return self._stream_cache[key]

    # --------------------------------------------------------------- inodes

    def _read_inode(self, ref: int) -> _Inode:
        block_offset = ref >> 16
        pos = ref & 0xFFFF
        buf, _ = self._stream(self.inode_table_start, block_offset, pos + 64)
        node = self._parse_inode(buf, pos)

        # the block list of large regular files (and long symlink targets)
        # can extend past the first metadata block; re-read with the full
        # span if the initial 64-byte window was not enough
        span = None
        if node.type == REG and node.block_list:
            span = 16 + 16 + 4 * len(node.block_list)
        elif node.type == LREG and node.block_list:
            span = 16 + 36 + 4 * len(node.block_list)
        elif node.type in (SYMLINK, LSYMLINK) and node.file_size:
            span = 16 + 8 + (4 if node.type == LSYMLINK else 0) + node.file_size

        if span and pos + span > len(buf):
            buf, _ = self._stream(self.inode_table_start, block_offset,
                                  pos + span)
            node = self._parse_inode(buf, pos)

        return node

    def _parse_inode(self, data: bytes, pos: int) -> _Inode:
        node = _Inode()
        (node.type, _mode, _uid, _guid, _mtime,
         node.inode_number) = struct.unpack_from('<HHHHII', data, pos)
        pos += 16

        t = node.type
        if t == DIR:
            (node.start_block, node.nlink, node.file_size, node.offset,
             node.parent) = struct.unpack_from('<IIHHI', data, pos)
        elif t == LDIR:
            # long directory: nlink, file_size, start_block, parent,
            # i_count, offset (then i_count index entries, which we skip).
            (node.nlink, node.file_size, node.start_block, node.parent,
             _i_count, node.offset) = struct.unpack_from('<IIIIHH', data, pos)
        elif t in (SYMLINK, LSYMLINK):
            node.nlink, size = struct.unpack_from('<II', data, pos)
            pos += 8
            if t == LSYMLINK:
                pos += 4  # xattr id
            node.target = data[pos:pos + size].decode('utf-8', 'replace')
            node.file_size = size
        elif t in (REG, LREG):
            if t == REG:
                (blocks_start, node.fragment, frag_offset,
                 node.file_size) = struct.unpack_from('<IIII', data, pos)
                pos += 16
            else:  # LREG: files > 4 GiB
                (blocks_start, node.file_size, _sparse, node.nlink,
                 node.fragment, frag_offset) = \
                    struct.unpack_from('<QQQIII', data, pos)
                pos += 36  # + 4 bytes of xattr id

            if node.fragment == SQUASHFS_INVALID_FRAG:
                count = (node.file_size + self.block_size - 1) // self.block_size
            else:
                count = node.file_size // self.block_size

            node.block_list = list(struct.unpack_from(
                f'<{count}I', data, pos)) if count else []
            # blocks_start is a byte offset from the start of the squashfs
            # image (verified against images produced by mksquashfs 4.x).
            node.start_block = blocks_start
            node.offset = frag_offset
        else:
            raise SquashfsError(f'unhandled inode type {t}')

        return node

    # ----------------------------------------------------------- directories

    def _read_dir(self, inode: _Inode):
        """Return (name, inode_ref, type) for every entry of a directory.

        Directory listings are packed as a continuous byte stream that may
        straddle metadata block boundaries (and individual entries with
        them), so the chained metadata blocks are first concatenated into
        one contiguous buffer.
        """
        listing_size = inode.file_size - 3
        if listing_size <= 0:
            return []

        buf, _ = self._stream(self.directory_table_start, inode.start_block,
                              inode.offset + listing_size)

        entries = []
        pos = inode.offset
        remaining = listing_size

        while remaining >= 12 and pos + 12 <= len(buf):
            count, start_block, _ino = struct.unpack_from('<III', buf, pos)
            if count > 255:
                break  # defensive: corrupt listing
            pos += 12
            remaining -= 12

            for _ in range(count + 1):
                if remaining < 8 or pos + 8 > len(buf):
                    return entries

                e_offset, _e_ino, e_type, name_size = \
                    struct.unpack_from('<HHHH', buf, pos)
                pos += 8
                remaining -= 8

                name_len = name_size + 1
                if remaining < name_len or pos + name_len > len(buf):
                    return entries
                name = buf[pos:pos + name_len]
                pos += name_len
                remaining -= name_len

                try:
                    name = name.decode('utf-8')
                except UnicodeDecodeError:
                    name = name.decode('utf-8', 'replace')

                entries.append((name, (start_block << 16) | e_offset, e_type))

        return entries

    # ------------------------------------------------------------- fragments

    def _fragment_table(self):
        if self._fragment_cache is not None:
            return self._fragment_cache

        entries = []
        per_block = SQUASHFS_METADATA_SIZE // 16  # 16 bytes per entry
        index_count = (self.fragment_count + per_block - 1) // per_block

        if self.fragment_count:
            raw = self._read_at(self.offset + self.fragment_table_start,
                                8 * index_count)
            index = struct.unpack(f'<{index_count}Q', raw)
            for blk in index:
                data, _ = self._metadata_block_absolute(blk)
                for i in range(0, len(data) - 15, 16):
                    start, size = struct.unpack_from('<QI', data, i)
                    entries.append((start, size))

        self._fragment_cache = entries
        return entries

    def _metadata_block_absolute(self, block_offset: int):
        """Read a metadata block addressed from the image start (used by
        the fragment index table)."""
        hdr = struct.unpack('<H', self._read_at(self.offset + block_offset,
                                                2))[0]
        if hdr & 0x8000:
            stored = hdr & 0x7FFF
            data = self._read_at(self.offset + block_offset + 2, stored)
        else:
            stored = hdr
            data = self._decompress(
                self._read_at(self.offset + block_offset + 2, stored))
        return data, None

    # ------------------------------------------------------------ public API

    def list_dir(self, path: str = '/'):
        inode = self._resolve(path)
        if inode.type not in (DIR, LDIR):
            raise SquashfsError(f'{path!r} is not a directory')
        return self._read_dir(inode)

    def readlink(self, path: str) -> str:
        inode = self._resolve(path, follow_final_symlink=False)
        if inode.target is None:
            raise SquashfsError(f'{path!r} is not a symlink')
        return inode.target

    def read_file(self, path: str, max_size: int = 32 * 1024 * 1024) -> bytes:
        inode = self._resolve(path)
        if inode.type not in (REG, LREG):
            raise SquashfsError(f'{path!r} is not a regular file')
        if inode.file_size > max_size:
            raise SquashfsError(f'{path!r} is too large '
                                f'({inode.file_size} bytes)')

        out = bytearray()
        addr = inode.start_block  # byte offset from the squashfs start
        for size in inode.block_list:
            if size == 0:  # sparse block
                out += b'\x00' * self.block_size
                continue

            if size & SQUASHFS_COMPRESSED_BIT_BLOCK:
                stored = size & ~SQUASHFS_COMPRESSED_BIT_BLOCK
                out += self._read_at(self.offset + addr, stored)
            else:
                stored = size
                out += self._decompress(self._read_at(self.offset + addr,
                                                      stored))
            addr += stored

        if inode.fragment != SQUASHFS_INVALID_FRAG:
            table = self._fragment_table()
            if inode.fragment >= len(table):
                raise SquashfsError(f'bad fragment index {inode.fragment}')
            frag_start, frag_size = table[inode.fragment]

            if frag_size & SQUASHFS_COMPRESSED_BIT_BLOCK:
                stored = frag_size & ~SQUASHFS_COMPRESSED_BIT_BLOCK
                frag = self._read_at(self.offset + frag_start, stored)
            else:
                stored = frag_size
                frag = self._decompress(
                    self._read_at(self.offset + frag_start, stored))

            tail = inode.file_size - len(out)
            out += frag[inode.offset:inode.offset + tail]

        return bytes(out[:inode.file_size])

    def _resolve(self, path: str, follow_final_symlink: bool = True) -> _Inode:
        parts = [p for p in path.split('/') if p]
        inode = self._read_inode(self.root_inode_ref)
        walked = []

        for i, part in enumerate(parts):
            if inode.type not in (DIR, LDIR):
                raise SquashfsError(f'{"/".join(walked) or "/"}: not a directory')
            found = None
            for name, child_ref, _t in self._read_dir(inode):
                if name == part:
                    found = child_ref
                    break
            if found is None:
                raise FileNotFoundError(f'no such file: {path}')
            walked.append(part)
            inode = self._read_inode(found)
            is_final = (i == len(parts) - 1)
            if inode.target is not None and (follow_final_symlink
                                             or not is_final):
                target = inode.target
                if not target.startswith('/'):
                    target = '/' + '/'.join(walked[:-1] + [target])
                # normalize and re-resolve (bounded by nlink sanity)
                target = os.path.normpath(target)
                inode = self._resolve(target)
                walked[-1] = target

        return inode

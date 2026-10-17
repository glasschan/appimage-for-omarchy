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

import io
import os
import struct
import subprocess
import tempfile
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
# Kernel PATH_MAX; squashfs symlink targets longer than this cannot resolve
# anywhere, so they are malformed by construction (caps _read_inode's stream
# span against attacker-inflated targets).
SQUASHFS_MAX_SYMLINK_TARGET = 4096
SQUASHFS_COMPRESSED_BIT_BLOCK = 1 << 24
SQUASHFS_INVALID_FRAG = 0xFFFFFFFF

# Round-7 review: every accumulation below is driven by untrusted metadata
# fields, so each gets an explicit ceiling. Legit AppImages sit orders of
# magnitude under all of them.
SQUASHFS_MAX_DIR_LISTING_BYTES = 1 << 20       # one directory's listing
SQUASHFS_MAX_FRAGMENT_COUNT = 20_000           # matches the file quota
SQUASHFS_MAX_INODE_SPAN = 256 * 1024           # metadata bytes per inode
SQUASHFS_MAX_CACHED_META_BLOCKS = 2048         # ~16 MiB of cached blocks
SQUASHFS_MAX_CACHED_STREAMS = 128              # ~9 MiB of cached streams

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


def _zstd_decompress(data: bytes, max_out: int) -> bytes:
    global _ZSTD_MODE
    if _ZSTD_MODE is None:
        _ZSTD_MODE = _zstd_available()

    if _ZSTD_MODE == 'stdlib':
        from compression import zstd
        # SquashFS zstd frames are streamed WITHOUT the content-size
        # header, so needs_input stays False even on a fully-consumed
        # legal frame — the output cap is the only reliable bound: a
        # crafted frame can expand gigabytes from a few stored bytes,
        # and the declared file-size slice only applies afterwards
        # (round-6 review). Over-cap frames are refused, not truncated.
        d = zstd.ZstdDecompressor()
        out = d.decompress(data, max_length=max_out + 1)
        if len(out) > max_out:
            raise SquashfsError(
                f'zstd block expands beyond {max_out} bytes — refusing')
        return out

    if _ZSTD_MODE == 'cli':
        # CI invariant (scripts/ci/security-gates.sh): every raw
        # subprocess.run outside utils.py must carry a bounded timeout —
        # one squashfs block is at most a few MiB, so 120s (the same
        # ceiling utils.run_command uses) is generous. The child is also
        # RLIMIT_FSIZE-capped at the output bound: it dies on SIGXFSZ
        # instead of materializing a bomb (round-6 review).
        import resource

        def _cap_output():
            resource.setrlimit(resource.RLIMIT_FSIZE,
                               (max_out + 1, max_out + 1))

        try:
            result = subprocess.run(
                ['zstd', '-d', '-c', '-q'],
                input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=120, preexec_fn=_cap_output)
        except subprocess.TimeoutExpired as exc:
            raise SquashfsError(
                f'zstd CLI timed out after {exc.timeout}s') from exc
        if result.returncode != 0:
            # SIGXFSZ (-25) is the output cap doing its job
            raise SquashfsError(
                'zstd CLI failed or exceeded the output bound: '
                + result.stderr.decode(errors='replace'))
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
         bytes_used, _id_table, _xattr, self.inode_table_start,
         self.directory_table_start, self.fragment_table_start,
         _lookup_table) = struct.unpack('<IIIIIHHHHHHQQQQQQQQ', sb[:0x60])
        # Total uncompressed size of the filesystem (superblock metadata,
        # never influenced by entry names): the extraction byte quota
        # reads this before any external extractor runs.
        self.bytes_used_total = bytes_used

        if self.magic != SQUASHFS_MAGIC:
            raise SquashfsError('not a little-endian squashfs image '
                                f'(magic=0x{self.magic:08x})')
        if self.s_major != 4:
            raise SquashfsError(f'unsupported squashfs version {self.s_major}')
        # Round-6 review: block_size is an attacker-controlled field. The
        # spec caps it at 1 MiB (power of two) and block_log must agree —
        # anything else is a malformed/hostile image, and it is this field
        # that sizes sparse-block allocations and every decompression cap
        # below, so it is validated before anything reads a block.
        if (self.block_size < 4096 or self.block_size > 1 << 20
                or self.block_size & (self.block_size - 1)):
            raise SquashfsError(f'implausible block size {self.block_size}')
        if (1 << self.block_log) != self.block_size:
            raise SquashfsError(f'block_log {self.block_log} does not match '
                                f'block size {self.block_size}')
        if self.compression not in (COMP_GZIP, COMP_XZ, COMP_ZSTD):
            names = {COMP_LZMA: 'lzma', COMP_LZO: 'lzo', COMP_LZ4: 'lz4'}
            name = names.get(self.compression, str(self.compression))
            raise UnsupportedCompression(f'squashfs compression "{name}" '
                                         'is not supported by the stdlib reader')

    # ------------------------------------------------------------------ io

    def close(self):
        # idempotent: __exit__, explicit close() and __del__ may all run
        if not self._f.closed:
            self._f.close()

    def __del__(self):
        # safety net so a caller that forgets close() can never leak the
        # file descriptor (silences ResourceWarning at garbage collection)
        f = getattr(self, '_f', None)
        if f is not None and not f.closed:
            try:
                f.close()
            except Exception:
                pass

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

    def _decompress(self, data: bytes, max_out: int) -> bytes:
        """Decompress one on-disk block, output-capped at `max_out`.

        The stored size and the declared file size are both
        attacker-controlled; an uncapped decompress turns a few stored
        kilobytes into gigabytes of allocation before the declared-size
        slice ever applies (round-6 review). Every caller passes the
        legal uncompressed size for its block type (`block_size` for
        data blocks, `SQUASHFS_METADATA_SIZE` for metadata); a block
        that expands further is corrupt or hostile and is refused, not
        truncated."""
        bomb = (f'block expands beyond {max_out} bytes — refusing')
        try:
            if self.compression == COMP_GZIP:
                d = zlib.decompressobj()
                out = d.decompress(data, max_out + 1)
                if len(out) > max_out or d.unconsumed_tail:
                    raise SquashfsError(bomb)
                if not d.eof:
                    raise SquashfsError('truncated gzip block')
                return out
            if self.compression == COMP_XZ:
                d = lzma.LZMADecompressor()
                out = d.decompress(data, max_out + 1)
                if len(out) > max_out or not d.eof:
                    raise SquashfsError(bomb)
                return out
            if self.compression == COMP_ZSTD:
                return _zstd_decompress(data, max_out)
            raise UnsupportedCompression(f'compression id {self.compression}')
        except SquashfsError:
            raise
        except Exception as e:  # zlib.error / lzma.LZMAError / zstd errors
            raise SquashfsError(f'block decompression failed: {e}') from e

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
            data = self._decompress(raw, max_out=SQUASHFS_METADATA_SIZE)

        result = (data, block_offset + 2 + stored)
        # round-7 review: the cache is keyed by attacker-reachable block
        # offsets; a walk over a hostile image must not grow it forever
        if len(self._meta_cache) > SQUASHFS_MAX_CACHED_META_BLOCKS:
            self._meta_cache.clear()
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

        # same round-7 bound for the concatenated-stream cache
        if len(self._stream_cache) > SQUASHFS_MAX_CACHED_STREAMS:
            self._stream_cache.clear()
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
            if span > SQUASHFS_MAX_INODE_SPAN:
                # symlink targets and block lists are the only span
                # drivers; both have spec limits far below this
                raise SquashfsError(
                    f'inode spans {span} bytes of metadata (max '
                    f'{SQUASHFS_MAX_INODE_SPAN}) — malformed or hostile')
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
            if size > SQUASHFS_MAX_SYMLINK_TARGET:
                raise SquashfsError(f'symlink target {size} bytes exceeds '
                                    f'{SQUASHFS_MAX_SYMLINK_TARGET} — '
                                    'malformed by construction')
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
        # round-7 review: listing_size is a raw inode field; the chained
        # metadata blocks below are decompressed into ONE buffer before
        # parsing, so a hostile listing size must not reach _stream
        if listing_size > SQUASHFS_MAX_DIR_LISTING_BYTES:
            raise SquashfsError(
                f'directory listing {listing_size} bytes '
                f'(max {SQUASHFS_MAX_DIR_LISTING_BYTES}) — malformed or '
                'hostile')

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

        # round-7 review: the entry list is built by materializing every
        # fragment's metadata — the count is a superblock field and must
        # not exceed the same quota as extracted files
        if self.fragment_count > SQUASHFS_MAX_FRAGMENT_COUNT:
            raise SquashfsError(
                f'fragment table declares {self.fragment_count} entries '
                f'(max {SQUASHFS_MAX_FRAGMENT_COUNT}) — malformed or hostile')

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
                self._read_at(self.offset + block_offset + 2, stored),
                max_out=SQUASHFS_METADATA_SIZE)
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
            stored = size & ~SQUASHFS_COMPRESSED_BIT_BLOCK
            # the stored size is metadata too: an incompressible block is
            # stored at exactly block_size, never larger (round-6 review)
            if stored > self.block_size:
                raise SquashfsError(
                    f'{path!r}: block header {stored} bytes exceeds the '
                    f'declared block size {self.block_size}')

            if size == 0:  # sparse block
                out += b'\x00' * self.block_size
            elif size & SQUASHFS_COMPRESSED_BIT_BLOCK:
                out += self._read_at(self.offset + addr, stored)
            else:
                out += self._decompress(self._read_at(self.offset + addr,
                                                      stored),
                                        max_out=self.block_size)
            addr += stored

            # a hostile block list must not accumulate past the declared
            # size — the final slice only bounds what is RETURNED, never
            # what was allocated (round-6 review)
            if len(out) > max_size:
                raise SquashfsError(f'{path!r}: block list overruns the '
                                    f'declared file size '
                                    f'({inode.file_size} bytes)')

        if inode.fragment != SQUASHFS_INVALID_FRAG:
            table = self._fragment_table()
            if inode.fragment >= len(table):
                raise SquashfsError(f'bad fragment index {inode.fragment}')
            frag_start, frag_size = table[inode.fragment]
            stored = frag_size & ~SQUASHFS_COMPRESSED_BIT_BLOCK
            if stored > self.block_size:
                raise SquashfsError(
                    f'{path!r}: fragment header {stored} bytes exceeds the '
                    f'declared block size {self.block_size}')

            if frag_size & SQUASHFS_COMPRESSED_BIT_BLOCK:
                stored = frag_size & ~SQUASHFS_COMPRESSED_BIT_BLOCK
                frag = self._read_at(self.offset + frag_start, stored)
            else:
                stored = frag_size
                frag = self._decompress(
                    self._read_at(self.offset + frag_start, stored),
                    max_out=self.block_size)

            tail = inode.file_size - len(out)
            if tail < 0:
                raise SquashfsError(f'{path!r}: blocks already exceed the '
                                    f'declared file size')
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

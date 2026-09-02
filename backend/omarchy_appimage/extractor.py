# extractor.py — metadata extraction from AppImages (stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Replaces AppImageProvider._extract_appimage / _load_appimage_metadata.
#
# Extraction strategy (the AppImage is NEVER executed):
#   1. pure-Python squashfs reader (squashfs.py) — works for gzip, xz and
#      zstd images without any external tool (PRD §7.3 wanted bsdtar, but
#      libarchive has no squashfs reader; verified with libarchive 3.8);
#   2. fallback: `unsquashfs -o <offset>` when installed;
#   3. fallback: `bsdtar` reading the squashfs stream at the offset
#      (kept for the day libarchive grows squashfs support).
#
# Marketplace security-review hardening: the external fallbacks fail
# closed — the listing is quota-checked before extraction, the extracted
# tree is sanitized afterwards (escaping symlinks unlinked, aggregate
# file-count/size quotas enforced) and _DirSource reads are
# containment-bound and never follow a final symlink.
#
# The discovery logic below (root *.desktop, .DirIcon, icon search order)
# mirrors GearLever's AppImageProvider._load_appimage_metadata.

import glob
import logging
import os
import re
import shutil
import stat
import struct
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from . import elf
from .desktop_entry import DesktopEntry
from .squashfs import SquashfsReader, SquashfsError
from .utils import get_file_hash, run_command

MAX_ICON_BYTES = 8 * 1024 * 1024

# Extraction quotas (aggregate, enforced on the pre-extraction listing
# and again as a post-extraction backstop while sanitizing).
MAX_EXTRACT_FILES = 20_000
MAX_EXTRACT_BYTES = 512 * 1024 * 1024

# Listing output bound: a 20k-entry listing with long paths can outgrow
# run_command's default 10 MiB cap, and a truncated listing must never
# be the reason a pre-extraction quota check silently passes.
_LISTING_OUTPUT_BYTES = 64 * 1024 * 1024

# The desktop entry's Icon value is untrusted: name-based icon candidates
# are only built for this strict shape (no separators, no traversal).
_ICON_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')

_TEMP_DIRS = []


def new_temp_dir(prefix: str = 'appimage-') -> str:
    d = tempfile.mkdtemp(prefix=prefix)
    _TEMP_DIRS.append(d)
    return d


def cleanup_temp_dirs():
    for d in _TEMP_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _TEMP_DIRS.clear()


def looks_like_desktop(data: bytes) -> bool:
    try:
        return b'[Desktop Entry]' in data[:4096]
    except Exception:
        return False


def image_format(data: bytes) -> Optional[str]:
    """Crude content-type probe for the formats we care about."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    head = data[:512].lstrip()
    if b'<svg' in head or head.startswith(b'<?xml'):
        return 'image/svg+xml'
    if looks_like_desktop(data):
        return 'application/x-desktop'
    try:
        text = data[:4096].decode('utf-8')
        if '\x00' not in text and '/' in text:
            return 'text/plain'  # possible symlink stored as a text path
    except UnicodeDecodeError:
        pass
    return None


class _VfsSource:
    """File source backed by the pure-Python squashfs reader."""

    def __init__(self, appimage_path: str):
        offset = elf.get_squashfs_offset(appimage_path)
        self.reader = SquashfsReader(appimage_path, offset)
        self.root = '/'

    def close(self):
        try:
            self.reader.close()
        except Exception:
            pass

    def root_entries(self):
        for name, _ref, _type in self.reader.list_dir(self.root):
            yield name

    def exists(self, path: str) -> bool:
        try:
            self.reader._resolve(path, follow_final_symlink=False)
            return True
        except (FileNotFoundError, SquashfsError):
            return False

    def is_symlink(self, path: str) -> bool:
        try:
            self.reader.readlink(path)
            return True
        except (SquashfsError, FileNotFoundError):
            return False

    def resolve_symlink(self, path: str) -> str:
        """Follow the symlink chain at `path` and return the final
        image-absolute target, or '' on failure."""
        current = path
        for _ in range(8):  # chain depth guard
            try:
                target = self.reader.readlink(current)
            except (SquashfsError, FileNotFoundError):
                return ''
            if not target.startswith('/'):
                current = os.path.normpath(
                    os.path.join(os.path.dirname(current), target))
            else:
                current = os.path.normpath(target)
            if not self.is_symlink(current):
                return current
        return ''

    def read(self, path: str, max_size: int = MAX_ICON_BYTES) -> Optional[bytes]:
        try:
            return self.reader.read_file(path, max_size=max_size)
        except (SquashfsError, FileNotFoundError) as e:
            logging.debug('vfs read %s failed: %s', path, e)
            return None


class _DirSource:
    """File source backed by an already-extracted directory on disk
    (unsquashfs / bsdtar fallbacks).

    Containment (marketplace review): the root is resolved once, every
    path is joined against it and its realpath must stay inside —
    violations read as nonexistent. Reads never follow a final symlink
    (O_NOFOLLOW); callers that want link resolution call resolve_symlink
    first, which is itself containment-bound."""

    def __init__(self, root: str):
        self.root = os.path.realpath(root)

    def close(self):
        pass  # temp dirs are cleaned up by cleanup_temp_dirs()

    def _raw(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip('/'))

    def _contained(self, path: str) -> Optional[str]:
        """realpath of `path` when it stays inside root, else None."""
        real = os.path.realpath(self._raw(path))
        if real != self.root and not real.startswith(self.root + os.sep):
            logging.debug('dir path escapes %s: %s', self.root, path)
            return None
        return real

    def root_entries(self):
        for name in os.listdir(self.root):
            yield name

    def exists(self, path: str) -> bool:
        if not os.path.lexists(self._raw(path)):
            return False
        # a path whose final symlink escapes root does not exist for us
        return self._contained(path) is not None

    def is_symlink(self, path: str) -> bool:
        return os.path.islink(self._raw(path))

    def resolve_symlink(self, path: str) -> str:
        real = self._contained(path)
        if real is None or not os.path.exists(real):
            return ''
        return '/' + os.path.relpath(real, self.root)

    def read(self, path: str, max_size: int = MAX_ICON_BYTES) -> Optional[bytes]:
        if self._contained(path) is None:
            return None
        try:
            # O_NOFOLLOW: never read *through* a final symlink (symlinked
            # paths are resolved via resolve_symlink + containment
            # instead); O_NONBLOCK keeps a planted FIFO from blocking the
            # open — the fstat regular-file check rejects it right after
            fd = os.open(self._raw(path),
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError as e:
            logging.debug('dir read %s failed: %s', path, e)
            return None
        with os.fdopen(fd, 'rb') as f:
            try:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode):
                    return None
                if st.st_size > max_size:
                    return None
                # read at most max_size even if the file races upward
                return f.read(max_size)
            except OSError as e:
                logging.debug('dir read %s failed: %s', path, e)
                return None


@dataclass
class ExtractedAppImage:
    extraction_folder: str = ''
    desktop_entry: Optional[DesktopEntry] = None
    appimage_file: str = ''
    desktop_file: Optional[str] = None      # path of copied .desktop
    icon_file: Optional[str] = None         # path of copied icon
    md5: str = ''


def _listing_totals(output: str, size_token: int) -> tuple:
    """(entry_count, regular_file_bytes) over a listing.

    Both `unsquashfs -ls` and `tar -tv` start entry lines with a 10-char
    mode string; the byte size sits at a tool-specific column (index
    `size_token`). Header/non-entry lines don't match and are ignored;
    the byte quota sums regular files only (symlinks count as entries,
    their targets are unreadable through _DirSource anyway)."""
    files = 0
    total_bytes = 0
    for line in output.splitlines():
        tokens = line.split()
        if len(tokens) <= size_token:
            continue
        mode = tokens[0]
        if len(mode) != 10 or mode[0] not in '-bcdlps':
            continue
        if mode[0] == 'd':
            continue
        files += 1
        if mode[0] == '-' and tokens[size_token].isdigit():
            total_bytes += int(tokens[size_token])
    return files, total_bytes


def _enforce_quotas(files: int, total_bytes: int):
    """Fail closed when the listing already shows an extraction bomb."""
    if files > MAX_EXTRACT_FILES:
        raise SquashfsError(f'extraction quota exceeded: listing shows '
                            f'{files} entries (max {MAX_EXTRACT_FILES})')
    if total_bytes > MAX_EXTRACT_BYTES:
        raise SquashfsError(f'extraction quota exceeded: listing shows '
                            f'{total_bytes} bytes '
                            f'(max {MAX_EXTRACT_BYTES})')


def _sanitize_extracted(root: str):
    """Post-extraction backstop: unlink symlinks whose target escapes
    `root` (file *and* directory symlinks — os.walk reports the latter
    in dirnames, which it never iterates itself) and enforce the
    aggregate quotas while walking (an image that busts either gets its
    tree removed and is rejected)."""
    def _bust(reason: str):
        shutil.rmtree(root, ignore_errors=True)
        raise SquashfsError('extraction quota exceeded ' + reason)

    files = 0
    total_bytes = 0

    def _count():
        nonlocal files
        files += 1
        if files > MAX_EXTRACT_FILES:
            _bust(f'(more than {MAX_EXTRACT_FILES} files)')

    def _is_escaping_link(path: str) -> bool:
        if not os.path.islink(path):
            return False
        real = os.path.realpath(path)
        if real == root or real.startswith(root + os.sep):
            return False
        logging.warning('removing symlink escaping %s: %s', root, path)
        try:
            os.unlink(path)
        except OSError as e:
            logging.warning('could not unlink %s: %s', path, e)
        return True

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept_dirs = []
        for name in dirnames:
            path = os.path.join(dirpath, name)
            _count()
            if not _is_escaping_link(path):
                kept_dirs.append(name)
        # walk descends into dirnames: prune the unlinked escapes
        dirnames[:] = kept_dirs
        for name in filenames:
            path = os.path.join(dirpath, name)
            _count()
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                _is_escaping_link(path)
            elif stat.S_ISREG(st.st_mode):
                total_bytes += st.st_size
                if total_bytes > MAX_EXTRACT_BYTES:
                    _bust(f'(more than {MAX_EXTRACT_BYTES} bytes)')


def _open_source(appimage_path: str) -> tuple:
    """Return (source, extracted_dir_or_None). Raises on total failure."""
    try:
        return _VfsSource(appimage_path), None
    except (SquashfsError, struct.error, ValueError) as e:
        logging.info('stdlib squashfs reader failed (%s); trying external '
                     'extractors', e)

    extracted_dir = None

    if shutil.which('unsquashfs'):
        extracted_dir = new_temp_dir('unsquashfs-')
        try:
            offset = elf.get_squashfs_offset(appimage_path)
            # fail closed: quota-check the listing before extracting
            listing = run_command(['unsquashfs', '-ls', '-o', str(offset),
                                   appimage_path], timeout=120,
                                  max_output_bytes=_LISTING_OUTPUT_BYTES)
            files, total_bytes = _listing_totals(
                listing.stdout.decode('utf-8', errors='replace'), 2)
            _enforce_quotas(files, total_bytes)
            run_command(['unsquashfs', '-no-progress', '-f',
                         '-o', str(offset), '-d', extracted_dir,
                         appimage_path], timeout=600)
            _sanitize_extracted(extracted_dir)
            return _DirSource(extracted_dir), extracted_dir
        except SquashfsError:
            shutil.rmtree(extracted_dir, ignore_errors=True)
            raise
        except Exception as e:
            logging.warning('unsquashfs failed: %s', e)
            shutil.rmtree(extracted_dir, ignore_errors=True)
            extracted_dir = None

    if shutil.which('bsdtar'):
        extracted_dir = new_temp_dir('bsdtar-')
        try:
            offset = elf.get_squashfs_offset(appimage_path)
            with open(appimage_path, 'rb') as f:
                f.seek(offset)
                listing = run_command(['bsdtar', '-tvf', '-'], stdin=f,
                                      timeout=120,
                                      max_output_bytes=_LISTING_OUTPUT_BYTES)
            files, total_bytes = _listing_totals(
                listing.stdout.decode('utf-8', errors='replace'), 4)
            _enforce_quotas(files, total_bytes)
            with open(appimage_path, 'rb') as f:
                f.seek(offset)
                run_command(['bsdtar', '-x', '-C', extracted_dir, '-f', '-'],
                            stdin=f, timeout=600)
            _sanitize_extracted(extracted_dir)
            return _DirSource(extracted_dir), extracted_dir
        except SquashfsError:
            shutil.rmtree(extracted_dir, ignore_errors=True)
            raise
        except Exception as e:
            logging.warning('bsdtar failed: %s', e)
            shutil.rmtree(extracted_dir, ignore_errors=True)
            extracted_dir = None

    raise SquashfsError('Could not extract the AppImage with any available '
                        'method (stdlib squashfs reader, unsquashfs, bsdtar); '
                        'the image uses an unsupported compression or is '
                        'not a type-2 AppImage')


def _find_desktop_file(source) -> tuple:
    """First parseable root-level *.desktop entry: (name, entry)."""
    for name in sorted(source.root_entries()):
        if not name.endswith('.desktop'):
            continue
        data = source.read('/' + name, max_size=256 * 1024)
        if data and looks_like_desktop(data):
            try:
                entry = DesktopEntry(content=data.decode('utf-8',
                                                         errors='replace'))
            except Exception:
                continue
            if entry.getName():
                return name, entry
    return None, None


def _safe_icon_base(icon_name: str) -> str:
    """Sanitized icon name from the desktop entry's untrusted Icon value
    ('' when unsafe): extension stripped, then a strict shape check —
    the value feeds theme-path construction and must never traverse or
    point at absolute paths."""
    base = re.sub(r'\.(png|svg)$', '', icon_name or '')
    if base in ('.', '..') or not _ICON_NAME_RE.match(base):
        return ''
    return base


def _find_icon(source, icon_name: str) -> Optional[bytes]:
    """Icon discovery, mirroring GearLever's order: .DirIcon first, then
    root <icon>.svg / <icon>.png, then hicolor theme paths."""
    candidates = []

    # 1) .DirIcon — may be a symlink (follow the chain) or a text file
    # holding a path to the real icon (GearLever's 'text/plain' case).
    diricon = source.read('/.DirIcon', max_size=64 * 1024)
    if diricon is not None:
        if source.is_symlink('/.DirIcon'):
            target = source.resolve_symlink('/.DirIcon')
            if target:
                candidates.append(target)
        else:
            fmt = image_format(diricon)
            if fmt in ('image/png', 'image/svg+xml'):
                candidates.append('/.DirIcon')
            elif fmt == 'text/plain':
                # the text path is untrusted: strip it to image-absolute
                # components and refuse any '..' traversal; as before it
                # must also exist inside the image
                possible = diricon.decode('utf-8', 'replace').strip()
                parts = [p for p in possible.split('/')
                         if p not in ('', '.')]
                if parts and '..' not in parts:
                    possible = '/' + '/'.join(parts)
                    if source.exists(possible):
                        candidates.append(possible)

    # 2) root-level <icon>.svg / <icon>.png (svg preferred); the Icon
    #    value is untrusted, so unsafe names skip the name-based
    #    candidates entirely
    base = _safe_icon_base(icon_name)
    if base:
        candidates += [f'/{base}.svg', f'/{base}.png']

        # 3) hicolor theme locations inside the image
        icons_prefix = '/usr/share/icons/hicolor'
        candidates += [
            f'{icons_prefix}/scalable/apps/{base}.svg',
            f'{icons_prefix}/512x512/apps/{base}.png',
            f'{icons_prefix}/256x256/apps/{base}.png',
            f'{icons_prefix}/128x128/apps/{base}.png',
            f'{icons_prefix}/96x96/apps/{base}.png',
        ]

    for path in candidates:
        if not source.exists(path):
            continue
        # name-based candidates may themselves be in-root symlinks (a
        # common layout: /icon.png -> usr/share/icons/app.png);
        # _DirSource.read never reads *through* a final symlink, so
        # resolve the chain first (containment enforced inside
        # resolve_symlink/read)
        read_path = path
        if source.is_symlink(path):
            target = source.resolve_symlink(path)
            if not target:
                continue
            read_path = target
        data = source.read(read_path)
        if data and image_format(data) in ('image/png', 'image/svg+xml'):
            return data

    return None


def load_appimage_metadata(appimage_path: str) -> ExtractedAppImage:
    """Extract .desktop + icon from an AppImage into a temp folder.

    Ported from AppImageProvider._load_appimage_metadata; the temp folder
    is keyed by the file's md5 like upstream (fresh per call here, since
    the CLI is a short-lived process)."""
    tmp_folder = new_temp_dir('gearlever_appimage_')

    result = ExtractedAppImage()
    result.extraction_folder = tmp_folder
    result.appimage_file = appimage_path
    result.md5 = get_file_hash(appimage_path, 'md5')

    source, _extracted = _open_source(appimage_path)
    try:
        desktop_name, entry = _find_desktop_file(source)
        if desktop_name:
            desktop_path = os.path.join(tmp_folder, 'app.desktop')
            with open(desktop_path, 'w', encoding='utf-8') as f:
                f.write(entry.get_text())
            result.desktop_entry = entry
            result.desktop_file = desktop_path

            icon_data = _find_icon(source, entry.getIcon())
            if icon_data:
                ext = '.svg' if b'<svg' in icon_data[:512] else '.png'
                icon_path = os.path.join(tmp_folder, 'icon' + ext)
                with open(icon_path, 'wb') as f:
                    f.write(icon_data)
                result.icon_file = icon_path
    finally:
        source.close()

    return result

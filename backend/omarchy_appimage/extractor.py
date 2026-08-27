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
# The discovery logic below (root *.desktop, .DirIcon, icon search order)
# mirrors GearLever's AppImageProvider._load_appimage_metadata.

import glob
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from . import elf
from .desktop_entry import DesktopEntry
from .squashfs import SquashfsReader, SquashfsError
from .utils import get_file_hash, run_command

MAX_ICON_BYTES = 8 * 1024 * 1024

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
    (unsquashfs / bsdtar fallbacks)."""

    def __init__(self, root: str):
        self.root = root

    def close(self):
        pass  # temp dirs are cleaned up by cleanup_temp_dirs()

    def _abs(self, path: str) -> str:
        return os.path.join(self.root, path.lstrip('/'))

    def root_entries(self):
        for name in os.listdir(self.root):
            yield name

    def exists(self, path: str) -> bool:
        return os.path.lexists(self._abs(path))

    def is_symlink(self, path: str) -> bool:
        return os.path.islink(self._abs(path))

    def resolve_symlink(self, path: str) -> str:
        real = os.path.realpath(self._abs(path))
        if not os.path.exists(real):
            return ''
        return '/' + os.path.relpath(real, self.root)

    def read(self, path: str, max_size: int = MAX_ICON_BYTES) -> Optional[bytes]:
        target = self._abs(path)
        try:
            if os.path.islink(target):
                real = os.path.realpath(target)
                if not os.path.exists(real):
                    return None
                target = real
            if not os.path.isfile(target):
                return None
            if os.path.getsize(target) > max_size:
                return None
            with open(target, 'rb') as f:
                return f.read()
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
            run_command(['unsquashfs', '-no-progress', '-f',
                         '-o', str(offset), '-d', extracted_dir,
                         appimage_path], timeout=600)
            return _DirSource(extracted_dir), extracted_dir
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
                result = subprocess.run(
                    ['bsdtar', '-x', '-C', extracted_dir, '-f', '-'],
                    stdin=f, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, timeout=600)
            if result.returncode == 0:
                return _DirSource(extracted_dir), extracted_dir
            raise RuntimeError('bsdtar exit code '
                               + str(result.returncode))
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
                possible = diricon.decode('utf-8', 'replace').strip()
                if not possible.startswith('/'):
                    possible = '/' + possible
                if source.exists(possible):
                    candidates.append(possible)

    # 2) root-level <icon>.svg / <icon>.png (svg preferred)
    if icon_name:
        base = re.sub(r'\.(png|svg)$', '', icon_name)
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
        data = source.read(path)
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

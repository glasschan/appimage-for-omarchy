# test_extractor.py — extraction hardening tests: icon-name
# sanitization, _DirSource containment, extraction quotas, DwarFS
# payload detection/extraction (faked dwarfsck/dwarfsextract). Hermetic.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omarchy_appimage import extractor
from omarchy_appimage.squashfs import SquashfsError

PNG = b'\x89PNG\r\n\x1a\n'


class SafeIconBaseTests(unittest.TestCase):
    """The desktop entry's Icon value is untrusted; only a strict shape
    produces name-based candidates."""

    def test_plain_names_accepted(self):
        self.assertEqual(extractor._safe_icon_base('Foo'), 'Foo')
        self.assertEqual(extractor._safe_icon_base('org.example.App'),
                         'org.example.App')
        self.assertEqual(extractor._safe_icon_base('app_name-2.5'),
                         'app_name-2.5')

    def test_extension_stripped_before_check(self):
        self.assertEqual(extractor._safe_icon_base('foo.png'), 'foo')
        self.assertEqual(extractor._safe_icon_base('foo.svg'), 'foo')

    def test_traversal_and_absolute_names_rejected(self):
        for name in ('../../etc/passwd.png', '/abs/x.png', 'a/b.png',
                     '..png', '.png', '.hidden.png', 'foo/..',
                     '..', '.', '', 'foo\\bar.png'):
            self.assertEqual(extractor._safe_icon_base(name), '',
                             f'{name!r} must produce no candidates')

    def test_dots_only_names_rejected(self):
        self.assertEqual(extractor._safe_icon_base('..png'), '')


class _FakeSource:
    """Records every path handed to exists()/read() so tests can assert
    no unsafe candidate is ever probed."""

    def __init__(self, files: dict = None, symlinks: dict = None):
        self.files = files or {}          # path -> bytes
        self.symlinks = symlinks or {}    # path -> target
        self.probed = []

    def root_entries(self):
        return iter([])

    def is_symlink(self, path):
        return path in self.symlinks

    def resolve_symlink(self, path):
        return self.symlinks.get(path, '')

    def exists(self, path):
        self.probed.append(path)
        return path in self.files or path in self.symlinks

    def read(self, path, max_size=None):
        self.probed.append(path)
        return self.files.get(path)


class FindIconTests(unittest.TestCase):
    def test_unsafe_icon_name_probes_nothing_name_based(self):
        source = _FakeSource({'/.DirIcon': b'not-an-image'})
        # image_format() on the .DirIcon body: text/plain candidate path
        data = extractor._find_icon(source, '../../etc/passwd.png')
        self.assertIsNone(data)
        for probed in source.probed:
            self.assertFalse('..' in probed or probed.startswith('/etc'),
                             f'unsafe candidate probed: {probed}')

    def test_diricon_text_path_with_traversal_rejected(self):
        # .DirIcon as a text file holding a path with '..' components:
        # the path must never even be probed
        source = _FakeSource({'/.DirIcon': b'../../etc/evil.png'})
        data = extractor._find_icon(source, '')
        self.assertIsNone(data)
        for probed in source.probed:
            self.assertNotIn('..', probed.split('/'))

    def test_diricon_text_path_inside_image_accepted(self):
        source = _FakeSource({
            '/.DirIcon': b'share/icons/app.png',
            '/share/icons/app.png': PNG,
        })
        data = extractor._find_icon(source, '')
        self.assertEqual(data, PNG)
        self.assertIn('/share/icons/app.png', source.probed)

    def test_dirsource_follows_inroot_symlinked_root_icon(self):
        # a root-level icon that is a symlink inside the image (the
        # common /icon.png -> usr/share/icons/app.png layout) must be
        # found on the _DirSource path too — _VfsSource follows it, and
        # reads never go *through* a final symlink, so the candidate is
        # resolved first (containment enforced inside resolve_symlink)
        root = tempfile.mkdtemp(prefix='iconlink-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, 'usr', 'share', 'icons'))
        Path(root, 'usr', 'share', 'icons', 'app.png').write_bytes(PNG)
        os.symlink('usr/share/icons/app.png', os.path.join(root, 'icon.png'))

        source = extractor._DirSource(root)
        self.assertEqual(extractor._find_icon(source, 'icon'), PNG)


class DirSourceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='dirsource-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(os.chdir, os.getcwd())
        Path(self.root, 'good.png').write_bytes(PNG)
        # a symlink pointing OUTSIDE the root
        os.symlink('/etc/passwd', os.path.join(self.root, 'evil.png'))
        # a symlink pointing INSIDE the root
        os.symlink('good.png', os.path.join(self.root, 'linked.png'))

    def _source(self):
        return extractor._DirSource(self.root)

    def test_symlink_escaping_root_does_not_exist(self):
        source = self._source()
        self.assertFalse(source.exists('/evil.png'))

    def test_symlink_escaping_root_read_is_none(self):
        source = self._source()
        self.assertIsNone(source.read('/evil.png'))

    def test_symlink_escaping_root_resolves_to_empty(self):
        source = self._source()
        self.assertEqual(source.resolve_symlink('/evil.png'), '')

    def test_traversal_path_is_nonexistent(self):
        source = self._source()
        self.assertFalse(source.exists('../outside.txt'))
        self.assertIsNone(source.read('../outside.txt'))
        self.assertIsNone(source.read('/../etc/passwd'))

    def test_regular_file_reads(self):
        source = self._source()
        self.assertTrue(source.exists('/good.png'))
        self.assertEqual(source.read('/good.png'), PNG)

    def test_symlink_inside_root_read_requires_explicit_resolution(self):
        # reads never follow a symlink: resolve_symlink + re-read instead
        source = self._source()
        self.assertTrue(source.is_symlink('/linked.png'))
        self.assertIsNone(source.read('/linked.png'))
        target = source.resolve_symlink('/linked.png')
        self.assertEqual(target, '/good.png')
        self.assertEqual(source.read(target), PNG)

    def test_max_size_is_enforced(self):
        source = self._source()
        self.assertIsNone(source.read('/good.png', max_size=2))

    def test_fifo_is_not_read(self):
        os.mkfifo(os.path.join(self.root, 'pipe'))
        source = self._source()
        self.assertTrue(source.exists('/pipe'))
        self.assertIsNone(source.read('/pipe'))


class SanitizeExtractedTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='sanitize-')
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_escaping_symlink_is_unlinked(self):
        os.symlink('/etc/passwd', os.path.join(self.root, 'evil.png'))
        Path(self.root, 'ok.png').write_bytes(PNG)

        extractor._sanitize_extracted(self.root)

        self.assertFalse(os.path.lexists(os.path.join(self.root,
                                                      'evil.png')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'ok.png')))

    def test_inside_symlink_is_kept(self):
        Path(self.root, 'ok.png').write_bytes(PNG)
        os.symlink('ok.png', os.path.join(self.root, 'linked.png'))

        extractor._sanitize_extracted(self.root)

        self.assertTrue(os.path.islink(os.path.join(self.root,
                                                    'linked.png')))

    def test_escaping_dir_symlink_is_unlinked(self):
        # os.walk reports directory symlinks in dirnames; they must not
        # survive sanitization (walk would otherwise descend out of root)
        os.makedirs(os.path.join(self.root, 'sub'))
        Path(self.root, 'sub', 'ok.png').write_bytes(PNG)
        os.symlink('/etc', os.path.join(self.root, 'sub', 'evil-dir'))

        extractor._sanitize_extracted(self.root)

        self.assertFalse(os.path.lexists(os.path.join(self.root, 'sub',
                                                      'evil-dir')))
        self.assertTrue(os.path.exists(os.path.join(self.root, 'sub',
                                                    'ok.png')))

    def test_directory_entries_count_toward_quota(self):
        # directories count as entries too: enough of them bust the
        # file-count quota even without any regular file
        for i in range(4):
            os.makedirs(os.path.join(self.root, f'd{i}'))
        with mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 2):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        self.assertFalse(os.path.exists(self.root))

    def test_file_count_quota_backstop(self):
        for i in range(5):
            Path(self.root, f'f{i}').write_bytes(b'x')
        with mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 3):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        # the over-quota tree was removed
        self.assertFalse(os.path.exists(self.root))

    def test_total_size_quota_backstop(self):
        for i in range(3):
            Path(self.root, f'f{i}').write_bytes(b'x' * 100)
        with mock.patch.object(extractor, 'MAX_EXTRACT_BYTES', 128):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._sanitize_extracted(self.root)
        self.assertFalse(os.path.exists(self.root))


class ListingQuotaTests(unittest.TestCase):
    UNSQUASHFS_LS = '''\
Parallel unsquashfs: Using 8 processors
4 inodes (6 blocks) to write

drwxr-xr-x root/root          46 2023-11-15 08:00 usr
-rw-r--r-- root/root        1234 2023-11-15 08:00 usr/bin/app
lrwxrwxrwx root/root          12 2023-11-15 08:00 .DirIcon
'''
    BSDAR_TVF = '''\
-rw-r--r--  0 root     root      1234 Jan 01  2023 usr/bin/app
drwxr-xr-x  0 root     root         0 Jan 01  2023 usr
lrwxr-xr-x  0 root     root        12 Jan 01  2023 .DirIcon -> ok.png
'''

    def test_parses_unsquashfs_listing(self):
        files, total = extractor._listing_totals(self.UNSQUASHFS_LS, 2)
        self.assertEqual(files, 2)          # regular file + symlink
        self.assertEqual(total, 1234)

    def test_parses_bsdtar_listing(self):
        files, total = extractor._listing_totals(self.BSDAR_TVF, 4)
        self.assertEqual(files, 2)
        self.assertEqual(total, 1234)

    def test_quota_enforcement_raises(self):
        with self.assertRaisesRegex(SquashfsError, 'quota'):
            extractor._enforce_quotas(extractor.MAX_EXTRACT_FILES + 1, 0)
        with self.assertRaisesRegex(SquashfsError, 'quota'):
            extractor._enforce_quotas(0, extractor.MAX_EXTRACT_BYTES + 1)
        # no raise within the bounds
        extractor._enforce_quotas(1, 1)

    def _garbage_appimage(self) -> str:
        """A file that fails the stdlib squashfs reader (so _open_source
        falls through to the external extractors)."""
        fd, path = tempfile.mkstemp(prefix='garbage-', suffix='.appimage')
        with os.fdopen(fd, 'wb') as f:
            f.write(b'\x00' * 64)
        self.addCleanup(os.unlink, path)
        return path

    def test_listing_failure_means_no_extraction(self):
        # fail closed: when the pre-extraction listing cannot be
        # produced, no extraction command may ever run
        path = self._garbage_appimage()
        calls = []

        def fake_run_command(command, **kwargs):
            calls.append(command)
            if '-ls' in command or '-tvf' in command:
                raise subprocess.CalledProcessError(1, command)
            return mock.Mock(returncode=0, stdout=b'', stderr=b'')

        with mock.patch.object(extractor.elf, 'get_squashfs_offset',
                               return_value=0), \
                mock.patch.object(extractor.shutil, 'which',
                                  return_value='/usr/bin/tool'), \
                mock.patch.object(extractor, 'run_command',
                                  side_effect=fake_run_command):
            with self.assertRaisesRegex(SquashfsError, 'Could not extract'):
                extractor._open_source(path)
        self.assertTrue(calls)
        # only listing commands were tried, never an extraction
        self.assertTrue(all('-ls' in c or '-tvf' in c for c in calls))

    def test_quota_exceeded_on_listing_refuses_extraction(self):
        path = self._garbage_appimage()
        calls = []
        bomb = ''.join(f'-rw-r--r-- root/root  {i} 2023-01-01 00:00 f{i}\n'
                       for i in range(4))

        def fake_run_command(command, **kwargs):
            calls.append(command)
            return mock.Mock(returncode=0,
                             stdout=bomb.encode(), stderr=b'')

        with mock.patch.object(extractor.elf, 'get_squashfs_offset',
                               return_value=0), \
                mock.patch.object(extractor.shutil, 'which',
                                  return_value='/usr/bin/unsquashfs'), \
                mock.patch.object(extractor, 'run_command',
                                  side_effect=fake_run_command), \
                mock.patch.object(extractor, 'MAX_EXTRACT_FILES', 2):
            with self.assertRaisesRegex(SquashfsError, 'quota'):
                extractor._open_source(path)
        # the listing ran, the extraction never did
        self.assertEqual([c for c in calls if '-ls' not in c
                          and '-tvf' not in c], [])


# --------------------------------------------------------------- DwarFS
#
# The real dwarfsck/dwarfsextract binaries are deliberately NOT used
# (they are not in the Arch repos and the tests must stay hermetic):
# FakeDwarfsTools stands in for them on PATH and records every call.

DWARFS_BOUNDARY = 0x1000


def _dwarfs_section(number=0, section_type=8, compression=2, length=0,
                    major=2, minor=5, data=True) -> bytes:
    """A synthetic 64-byte DwarFS section header followed by `length`
    bytes of section data (dwarfs' section_layout.h layout).

    `data=False` declares a length without appending it (the header is
    then the whole image: the "length points past EOF" case)."""
    raw = bytearray(64)
    raw[0:6] = b'DWARFS'
    raw[6] = major
    raw[7] = minor
    raw[8:40] = b'\x21' * 32          # sha-512/256
    raw[40:48] = b'\x42' * 8          # xxh3-64
    struct.pack_into('<I', raw, 0x30, number)
    struct.pack_into('<HH', raw, 0x34, section_type, compression)
    struct.pack_into('<Q', raw, 0x38, length)
    return bytes(raw) + (b'\xa5' * length if data else b'')


def dwarfs_appimage(payload: bytes, prefix: bytes = b'') -> bytes:
    """An ELF stub whose payload boundary lands exactly before `payload`.

    `prefix` is what a fake runtime keeps before the payload (its
    .rodata, full of DWARFS_* strings, in the false-positive tests)."""
    header = bytearray(64)
    header[0:4] = b'\x7fELF'
    header[4] = 2                     # 64-bit
    header[5] = 1                     # little-endian
    header[6] = 1                     # EV_CURRENT
    header[8:11] = b'\x41\x49\x02'    # AppImage type-2 magic
    struct.pack_into('<H', header, 0x12, 0x3E)      # EM_X86_64
    struct.pack_into('<Q', header, 0x28, 0x40)      # e_shoff
    struct.pack_into('<HH', header, 0x3A, 64,
                     (DWARFS_BOUNDARY - 0x40) // 64)
    blob = bytearray(header) + prefix
    blob += b'\x00' * (DWARFS_BOUNDARY - len(blob))
    return bytes(blob) + payload


def _write_appimage(test, blob: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix='dwarfs-', suffix='.appimage')
    with os.fdopen(fd, 'wb') as f:
        f.write(blob)
    test.addCleanup(os.unlink, path)
    return path


def _dwarfs_image(test) -> str:
    """An AppImage with a valid DwarFS header pair at the boundary."""
    return _write_appimage(test, dwarfs_appimage(
        _dwarfs_section(length=96) + _dwarfs_section(number=1,
                                                     section_type=9)))


# the runtime's own strings: measured in goverlay-1.9.4 there are 14
# 'DWARFS' false positives before the payload (DWARFS_BLOCKSIZE and
# friends in .rodata) and one 'hsqs' immediate inside .text
URUNTIME_RODATA = (b'DWARFS_BLOCKSIZE=1048576\x00DWARFS_CACHESIZE=256\x00'
                   * 7)


class FakeDwarfsTools:
    """Puts fake dwarfsck/dwarfsextract — and, by default, failing
    unsquashfs/bsdtar sentinels — first on PATH, recording every
    invocation as one `name`, one argument per line, in `self.log`."""

    # One template for every fake tool: it records its argv, one
    # argument per line, and — for dwarfsextract — writes the files
    # GearLever's patterns select, at their image paths (AppImages keep
    # .desktop / .DirIcon at the root and the icons under usr/share/
    # icons), plus one escaping symlink like a hostile archive plants.
    _TOOL = '''#!{shebang}
import os
import sys

name = {name!r}
with open({log!r}, 'a') as log:
    log.write(name + '\\n')
    for arg in sys.argv[1:]:
        log.write(arg + '\\n')

if name == 'dwarfsextract':
    out = ''
    for arg in sys.argv[1:]:
        if arg.startswith('--output='):
            out = arg[len('--output='):]
    icons = os.path.join(out, 'usr', 'share', 'icons', 'hicolor',
                         '256x256', 'apps')
    os.makedirs(icons, exist_ok=True)
    with open(os.path.join(out, 'com.example.DwarfApp.desktop'), 'w') as f:
        f.write('[Desktop Entry]\\nName=Dwarf App\\nExec=dwarfapp\\n'
                'Type=Application\\nIcon=dwarfapp\\nTerminal=false\\n')
    for path in (os.path.join(out, '.DirIcon'),
                 os.path.join(icons, 'dwarfapp.png')):
        with open(path, 'wb') as f:
            f.write(b'\\x89PNG\\r\\n\\x1a\\n')
    os.symlink('/etc/passwd', os.path.join(out, 'evil.png'))
sys.exit({code})
'''

    def _tool(self, name: str, code: int) -> str:
        return self._TOOL.format(shebang=sys.executable, log=self.log,
                                 name=name, code=code)

    def __init__(self, dwarfsck_exit=0, dwarfsextract_exit=0,
                 include_dwarfs=True, sentinels=True):
        self.base = tempfile.mkdtemp(prefix='fake-dwarfs-')
        self.bindir = os.path.join(self.base, 'bin')
        os.makedirs(self.bindir)
        self.log = os.path.join(self.base, 'calls.log')
        tools = {}
        if include_dwarfs:
            if dwarfsck_exit is not None:
                tools['dwarfsck'] = self._tool('dwarfsck', dwarfsck_exit)
            tools['dwarfsextract'] = self._tool('dwarfsextract',
                                                dwarfsextract_exit)
        if sentinels:
            for name in ('unsquashfs', 'bsdtar'):
                tools[name] = self._tool(name, 1)
        for name, body in tools.items():
            path = os.path.join(self.bindir, name)
            Path(path).write_text(body)
            os.chmod(path, 0o755)

    def __enter__(self):
        self._path = os.environ.get('PATH')
        os.environ['PATH'] = (self.bindir + os.pathsep +
                              (self._path or ''))
        return self

    def __exit__(self, *exc):
        if self._path is None:
            os.environ.pop('PATH', None)
        else:
            os.environ['PATH'] = self._path
        shutil.rmtree(self.base, ignore_errors=True)
        return False

    def calls(self):
        """[(tool name, [argument, ...]), ...] in invocation order."""
        try:
            lines = Path(self.log).read_text().splitlines()
        except OSError:
            return []
        names = ('dwarfsck', 'dwarfsextract', 'unsquashfs', 'bsdtar')
        result = []
        for line in lines:
            if line in names:
                result.append((line, []))
            elif result:
                result[-1][1].append(line)
        return result


class DwarfsDetectionTests(unittest.TestCase):
    """The probe is anchored at the ELF boundary and validated by the
    second section header; the runtime's own strings are never a
    payload."""

    def test_payload_is_detected_at_the_elf_boundary(self):
        path = _dwarfs_image(self)
        payload = extractor._find_dwarfs_payload(path)
        self.assertIsNotNone(payload)
        self.assertEqual(payload['offset'], DWARFS_BOUNDARY)
        self.assertEqual(payload['version'], '2.5')
        self.assertEqual(payload['length'], 96)

    def test_runtime_rodata_strings_before_the_boundary_are_ignored(self):
        self.assertEqual(URUNTIME_RODATA.count(b'DWARFS'), 14)
        # 'DWARFS' only counts at the boundary: this payload is not one
        path = _write_appimage(self, dwarfs_appimage(
            b'hsqs' + b'\x00' * 60, prefix=URUNTIME_RODATA))
        self.assertIsNone(extractor._find_dwarfs_payload(path))

    def test_first_header_without_a_second_header_is_rejected(self):
        # a lone DWARFS magic at the boundary (truncated archive, or any
        # blob that happens to start like one) is not a payload
        path = _write_appimage(self, dwarfs_appimage(
            _dwarfs_section(length=96) + b'\x00' * 96))
        self.assertIsNone(extractor._find_dwarfs_payload(path))

    def test_length_pointing_past_the_end_of_file_is_rejected(self):
        # the second-header read can never be aimed past EOF, whatever
        # length the image declares
        path = _write_appimage(self, dwarfs_appimage(
            _dwarfs_section(length=1 << 40, data=False)))
        self.assertIsNone(extractor._find_dwarfs_payload(path))

    def test_non_elf_file_is_not_a_dwarfs_payload(self):
        path = _write_appimage(self, b'not an elf at all' + b'DWARFS' * 8)
        self.assertIsNone(extractor._find_dwarfs_payload(path))

    def test_file_too_short_for_one_header_is_rejected(self):
        path = _write_appimage(self, b'\x7fELF\x02\x01\x01\x00\x41\x49\x02')
        self.assertIsNone(extractor._find_dwarfs_payload(path))


class DwarfsExtractionTests(unittest.TestCase):

    def setUp(self):
        # the DwarFS branches hand out _DirSource trees that only
        # cleanup_temp_dirs removes: drop the ones this class leaves
        self.addCleanup(extractor.cleanup_temp_dirs)

    def test_dwarfs_extraction_is_pattern_restricted_quota_contained_and_sanitized(self):
        path = _dwarfs_image(self)
        with FakeDwarfsTools() as tools:
            source, extracted = extractor._open_source(path)
            self.assertIsInstance(source, extractor._DirSource)
            self.assertTrue(extracted)

            name, entry = extractor._find_desktop_file(source)
            self.assertEqual(name, 'com.example.DwarfApp.desktop')
            self.assertEqual(entry.getName(), 'Dwarf App')
            self.assertEqual(extractor._find_icon(source, entry.getIcon()),
                             PNG)
            # nested icon paths survive: that is what the hicolor
            # candidate list in _find_icon needs
            self.assertTrue(source.exists(
                '/usr/share/icons/hicolor/256x256/apps/dwarfapp.png'))

            # dwarfsck probes first, then dwarfsextract extracts — and
            # only with GearLever's four patterns
            probe, extract = tools.calls()
            self.assertEqual(probe[0], 'dwarfsck')
            self.assertIn('--no-check', probe[1])
            self.assertEqual(extract[0], 'dwarfsextract')
            self.assertEqual(
                [a for a in extract[1] if a.startswith('--pattern=')],
                ['--pattern=**.desktop', '--pattern=**.png',
                 '--pattern=**.svg', '--pattern=.DirIcon'])

            # the escaping symlink the extractor planted was unlinked
            self.assertFalse(source.exists('/evil.png'))

        # the aggregate quota backstop still covers this path, and the
        # over-quota tree is removed
        with FakeDwarfsTools():
            with mock.patch.object(extractor, 'MAX_EXTRACT_BYTES', 4):
                with self.assertRaisesRegex(SquashfsError, 'quota'):
                    extractor._open_source(path)

    def test_rodata_false_positives_never_reach_the_dwarfs_tools(self):
        # the important regression: an image whose runtime is full of
        # DWARFS strings must be handled as an ordinary (unreadable)
        # squashfs image — no DwarFS branch, and the squashfs chain is
        # still tried before giving up
        path = _write_appimage(self, dwarfs_appimage(
            b'hsqs' + b'\x00' * 60, prefix=URUNTIME_RODATA))
        with FakeDwarfsTools():
            with self.assertRaisesRegex(SquashfsError, 'Could not extract'):
                extractor._open_source(path)

    def test_failure_without_a_dwarfs_payload_keeps_the_old_message(self):
        path = _write_appimage(self, dwarfs_appimage(b'hsqs' + b'\x00' * 60))
        with FakeDwarfsTools(include_dwarfs=False):
            with self.assertRaises(SquashfsError) as cm:
                extractor._open_source(path)
        message = str(cm.exception)
        self.assertIn('unsupported compression', message)
        self.assertIn('not a type-2 AppImage', message)
        self.assertNotIn('DwarFS', message)

    def test_missing_dwarfs_tools_fail_closed_name_the_format(self):
        # dwarfs is AUR-only: without it the image must still fail closed
        # with a message that names DwarFS and where the tools come from,
        # after letting the (doomed but cheap) squashfs fallbacks run
        path = _dwarfs_image(self)
        with FakeDwarfsTools(include_dwarfs=False) as tools:
            with self.assertRaises(SquashfsError) as cm:
                extractor._open_source(path)
            # no DwarFS tool was invoked, and the (doomed but cheap)
            # squashfs fallbacks were left to finish the job
            self.assertEqual([name for name, _ in tools.calls()],
                             ['unsquashfs', 'bsdtar'])
        message = str(cm.exception)
        self.assertIn('DwarFS', message)
        self.assertIn('dwarfsck/dwarfsextract', message)
        self.assertIn('AUR', message)

    def test_dwarfsextract_failure_names_dwarfs_and_skips_squashfs(self):
        path = _dwarfs_image(self)
        with FakeDwarfsTools(dwarfsextract_exit=1) as tools:
            with self.assertRaises(SquashfsError) as cm:
                extractor._open_source(path)
            self.assertIn('DwarFS', str(cm.exception))
            # the tools are installed: feeding DwarFS to unsquashfs and
            # bsdtar would only waste two calls and muddle the log
            self.assertNotIn('unsquashfs',
                             [name for name, _ in tools.calls()])
            # and the half-extracted tree was removed
            out = [a for a in tools.calls()[1][1]
                   if a.startswith('--output=')]
            self.assertEqual(len(out), 1)
            self.assertFalse(os.path.exists(out[0][len('--output='):]))

    def test_dwarfsck_rejection_falls_back_to_the_squashfs_extractors(self):
        # dwarfsck judges an image more strictly than the header-pair
        # check (hashes, section index): its "not DwarFS" verdict sends
        # us through the squashfs chain instead of failing outright
        path = _dwarfs_image(self)
        with FakeDwarfsTools(dwarfsck_exit=1) as tools:
            with self.assertRaisesRegex(SquashfsError, 'Could not extract'):
                extractor._open_source(path)
            names = [name for name, _ in tools.calls()]
        self.assertEqual(names[0], 'dwarfsck')
        self.assertNotIn('dwarfsextract', names)
        self.assertIn('unsquashfs', names)
        self.assertIn('bsdtar', names)


if __name__ == '__main__':
    unittest.main()

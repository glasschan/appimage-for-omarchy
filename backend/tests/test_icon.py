# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
"""Icon sanitisation: provider bytes never reach the shell's decoder unbounded."""

import errno
import os
import shutil
import struct
import tempfile
import unittest
import zlib
from unittest import mock

import helpers  # noqa: F401  — puts backend/ on sys.path (as the other suites do)
from helpers import FakeXDGTestCase

from omarchy_appimage import icon, janitor
from omarchy_appimage.extractor import _find_icon, image_format


def _chunk(ctype: bytes, body: bytes) -> bytes:
    crc = zlib.crc32(ctype + body) & 0xFFFFFFFF
    return struct.pack('>I', len(body)) + ctype + body + struct.pack('>I', crc)


def _scanline(width: int, bits_per_pixel: int) -> bytes:
    return b'\x00' + b'\x00' * ((width * bits_per_pixel + 7) // 8)


def _pixels(width, height, depth, color_type, interlace):
    bits = icon._CHANNELS[color_type] * depth
    if not interlace:
        return b''.join(_scanline(width, bits) for _ in range(height))
    raw = b''
    for xs, ys, dx, dy in icon._ADAM7:
        pw = 0 if width <= xs else -(-(width - xs) // dx)
        ph = 0 if height <= ys else -(-(height - ys) // dy)
        if pw and ph:
            raw += b''.join(_scanline(pw, bits) for _ in range(ph))
    return raw


def build_png(width=8, height=8, *, depth=8, color_type=6, interlace=0,
              comp=0, filt=0, extra=(), plte=None, trns=None, idat=None,
              trailing=b''):
    ihdr = struct.pack('>IIBBBBB', width, height, depth, color_type, comp,
                       filt, interlace)
    out = icon._PNG_SIG + _chunk(b'IHDR', ihdr)
    if plte is not None:
        out += _chunk(b'PLTE', plte)
    if trns is not None:
        out += _chunk(b'tRNS', trns)
    for ctype, body in extra:
        out += _chunk(ctype, body)
    if idat is None:
        assert width * height <= 200_000, 'pass an explicit idat for big frames'
        idat = zlib.compress(_pixels(width, height, depth, color_type,
                                     interlace), 9)
    out += _chunk(b'IDAT', idat)
    out += _chunk(b'IEND', b'')
    return out + trailing


def chunk_types(data: bytes):
    seen, pos = [], 8
    while pos + 12 <= len(data):
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        seen.append(data[pos + 4:pos + 8])
        pos += 12 + length
    return seen


class PngSanitizeTests(unittest.TestCase):

    def test_metadata_chunks_are_dropped(self):
        # each zTXt inflates to 1 MiB; libpng keeps every decoded text chunk
        # in memory and only limits a single one, so 50 of them in a file
        # smaller than 2 kB are the amplification the extractor's 8 MiB
        # *stored*-byte cap cannot bound
        bomb = zlib.compress(b'A' * (1024 * 1024), 9)
        extra = [(b'zTXt', b'Comment\x00\x00' + bomb)] * 50
        extra += [(b'tEXt', b'Software\x00x' * 1000),
                  (b'iCCP', b'p\x00\x00' + bomb),
                  (b'eXIf', b'MM\x00*' + b'\x00' * 64)]
        data = build_png(extra=extra)
        self.assertLess(len(data), 64 * 1024)
        safe = icon.sanitize_icon(data, 'image/png')

        self.assertIsNotNone(safe)
        self.assertEqual(chunk_types(safe), [b'IHDR', b'IDAT', b'IEND'])
        self.assertNotIn(b'zTXt', safe)
        self.assertLess(len(safe), len(data))

    def test_dimension_bomb_refused(self):
        # 60000x60000 = 14.4 GB at 4 bytes/pixel, declared by 8 bytes of
        # IHDR in a file of a few hundred bytes (the IDAT is deliberately
        # tiny: the refusal must not depend on inflating anything)
        data = build_png(60000, 60000, idat=zlib.compress(b''))
        self.assertLess(len(data), 1024)
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_idat_bomb_refused(self):
        # honest small frame, pixel stream inflates far past it
        bomb = zlib.compress(b'\x00' * (8 * 1024 * 1024), 9)
        self.assertIsNone(icon.sanitize_icon(build_png(idat=bomb),
                                             'image/png'))

    def test_truncated_pixel_stream_refused(self):
        self.assertIsNone(icon.sanitize_icon(
            build_png(idat=zlib.compress(b'\x00' * 4)), 'image/png'))

    def test_palette_and_transparency_are_kept(self):
        # palette images need PLTE (and usually tRNS) to render at all
        data = build_png(color_type=3, plte=b'\x00' * 3 * 8,
                         trns=b'\xff' * 8)
        safe = icon.sanitize_icon(data, 'image/png')
        self.assertIsNotNone(safe)
        self.assertEqual(chunk_types(safe),
                         [b'IHDR', b'PLTE', b'tRNS', b'IDAT', b'IEND'])

    def test_interlaced_png_accepted(self):
        safe = icon.sanitize_icon(build_png(8, 8, interlace=1), 'image/png')
        self.assertIsNotNone(safe)

    def test_trailing_bytes_after_iend_are_dropped(self):
        data = build_png(trailing=b'\x00' * 512)
        safe = icon.sanitize_icon(data, 'image/png')
        self.assertEqual(chunk_types(safe)[-1], b'IEND')
        self.assertEqual(len(safe), len(data) - 512)

    def test_not_a_png_refused(self):
        self.assertIsNone(icon.sanitize_icon(b'GIF89a' + b'\x00' * 32,
                                             'image/png'))
        self.assertIsNone(icon.sanitize_icon(b'', 'image/png'))


class IhdrFieldTests(unittest.TestCase):
    """Ticket #63: an IHDR a conforming decoder rejects must be refused,
    not re-emitted for the row to fail to decode (and fall through to the
    next, unverified candidate). Compression/filter methods other than 0,
    interlace other than 0/1, and any (colour type, bit depth) pair off
    the spec table are each a refusal."""

    def test_sanitizer_version_is_3(self):
        # the strict IHDR gate is verdict logic: the bump forces one
        # re-sweep and one re-verification of every retained icon
        self.assertEqual(icon.SANITIZER_VERSION, 3)

    def test_unknown_compression_method_refused(self):
        self.assertIsNone(icon.sanitize_icon(
            build_png(comp=1), 'image/png'))

    def test_unknown_filter_method_refused(self):
        self.assertIsNone(icon.sanitize_icon(
            build_png(filt=1), 'image/png'))

    def test_unknown_interlace_method_refused(self):
        self.assertIsNone(icon.sanitize_icon(
            build_png(interlace=2), 'image/png'))

    def test_invalid_depth_colour_pairs_refused(self):
        bad_pairs = [(0, 32),          # depth off the spec table
                     (2, 1), (2, 2), (2, 4), (2, 32),
                     (3, 16),
                     (4, 1), (4, 2), (4, 4),
                     (6, 1), (6, 2), (6, 4),
                     (5, 8)]          # colour type 5 does not exist
        for color_type, depth in bad_pairs:
            with self.subTest(color_type=color_type, depth=depth):
                # an unknown colour type has no channel table, so the
                # helper cannot synthesise pixels for it: an empty idat
                # still reaches the IHDR check (which must fire first)
                data = build_png(color_type=color_type, depth=depth,
                                 plte=b'\x00' * 3 * 4,
                                 idat=zlib.compress(b''))
                self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_valid_depth_colour_pairs_accepted(self):
        good_pairs = [(0, 1), (0, 2), (0, 4), (0, 8), (0, 16),
                      (2, 8), (2, 16),
                      (3, 1), (3, 2), (3, 4), (3, 8),
                      (4, 8), (4, 16),
                      (6, 8), (6, 16)]
        for color_type, depth in good_pairs:
            with self.subTest(color_type=color_type, depth=depth):
                data = build_png(color_type=color_type, depth=depth,
                                 plte=(b'\x00' * 3 * 4
                                       if color_type == 3 else None))
                self.assertIsNotNone(icon.sanitize_icon(data, 'image/png'))

    def test_palette_required_iff_colour_type_3(self):
        # colour 3 without PLTE: refused; every other colour type needs
        # no PLTE at all
        self.assertIsNone(icon.sanitize_icon(
            build_png(1, 1, color_type=3), 'image/png'))
        self.assertIsNotNone(icon.sanitize_icon(
            build_png(1, 1, color_type=6), 'image/png'))


class SvgSanitizeTests(unittest.TestCase):

    def test_valid_svg_passes(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"/>'
        self.assertEqual(icon.sanitize_icon(svg, 'image/svg+xml'), svg)

    def test_viewbox_bounds_are_enough(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48"/>'
        self.assertEqual(icon.sanitize_icon(svg, 'image/svg+xml'), svg)

    def test_entity_declaration_refused(self):
        svg = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "aaaa">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg" width="16" '
               b'height="16"/&a;>')
        self.assertIsNone(icon.sanitize_icon(svg, 'image/svg+xml'))

    def test_oversized_dimensions_refused(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="60000" height="60000"/>'
        self.assertIsNone(icon.sanitize_icon(svg, 'image/svg+xml'))

    def test_unbounded_dimensions_refused(self):
        for attrs in (b'width="100%" height="100%"',
                      b'',  # no width/height and no viewBox
                      b'width="10em" height="10em"'):
            svg = b'<svg xmlns="http://www.w3.org/2000/svg" ' + attrs + b'/>'
            with self.subTest(attrs=attrs):
                self.assertIsNone(icon.sanitize_icon(svg, 'image/svg+xml'))

    def test_oversized_file_refused(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="8" '
               b'height="8"/>' + b' ' * icon.MAX_SVG_BYTES)
        self.assertIsNone(icon.sanitize_icon(svg, 'image/svg+xml'))


class _StubSource:
    """Just enough of _DirSource for _find_icon."""

    def __init__(self, files):
        self.files = files

    def exists(self, path):
        return path in self.files

    def read(self, path, max_size=None):
        return self.files.get(path)

    def is_symlink(self, path):
        return False

    def resolve_symlink(self, path):
        return None


class ExtractorWiringTests(unittest.TestCase):
    """The extractor must hand the shell sanitised bytes, not raw ones."""

    def test_find_icon_sanitises_the_chosen_candidate(self):
        bomb = zlib.compress(b'B' * (1024 * 1024), 9)
        hostile = build_png(extra=[(b'zTXt', b'Comment\x00\x00' + bomb)] * 20)
        source = _StubSource({'/.DirIcon': hostile})
        self.assertEqual(image_format(hostile), 'image/png')

        safe = _find_icon(source, 'whatever')

        self.assertIsNotNone(safe)
        self.assertEqual(chunk_types(safe), [b'IHDR', b'IDAT', b'IEND'])

    def test_find_icon_refuses_a_hostile_icon_and_falls_through(self):
        source = _StubSource({'/.DirIcon': build_png(60000, 60000,
                                                     idat=zlib.compress(b''))})
        self.assertIsNone(_find_icon(source, 'whatever'))


class PngChunkLengthTests(unittest.TestCase):
    """PLTE/tRNS are file-declared sizes, not geometry: bound them too."""

    def test_oversized_plte_refused(self):
        data = build_png(1, 1, color_type=2, plte=b'\x00' * 900)
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_palette_image_without_plte_refused(self):
        data = build_png(1, 1, color_type=3)
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_palette_image_with_plte_accepted(self):
        data = build_png(1, 1, color_type=3, plte=b'\x00\x00\x00')
        self.assertIsNotNone(icon.sanitize_icon(data, 'image/png'))

    def test_trns_larger_than_the_palette_refused(self):
        data = build_png(1, 1, color_type=3, plte=b'\x00\x00\x00',
                         trns=b'\x00' * 4)
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_trns_on_an_image_with_alpha_refused(self):
        data = build_png(1, 1, color_type=6, trns=b'\x00\x00')
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_trailing_bytes_inside_idat_refused(self):
        idat = zlib.compress(_pixels(1, 1, 8, 2, 0), 9) + b'JUNKJUNK'
        data = build_png(1, 1, color_type=2, idat=idat)
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))


class SvgStructureTests(unittest.TestCase):
    """The SVG gate must bound the whole tree, not just the root's
    numbers: Qt renders <image> at the embedded raster's natural size."""

    def _svg(self, body: str, attrs: str = 'width="16" height="16"'):
        return ('<svg xmlns="http://www.w3.org/2000/svg" '
                'xmlns:xlink="http://www.w3.org/1999/xlink" '
                f'{attrs}>{body}</svg>').encode()

    def test_embedded_raster_refused(self):
        data = self._svg('<image width="16" height="16" '
                         'xlink:href="data:image/png;base64,AAAA"/>')
        self.assertIsNone(icon.sanitize_icon(data, 'image/svg+xml'))

    def test_script_style_and_filter_refused(self):
        for tag in ('script', 'style', 'filter'):
            data = self._svg(f'<{tag}></{tag}>')
            self.assertIsNone(icon.sanitize_icon(data, 'image/svg+xml'), tag)

    def test_filter_primitive_refused(self):
        data = self._svg('<feGaussianBlur stdDeviation="1"/>')
        self.assertIsNone(icon.sanitize_icon(data, 'image/svg+xml'))

    def test_external_href_refused(self):
        for href in ('http://example.test/x.svg#a',
                     'file:///etc/passwd',
                     'data:image/png;base64,AAAA'):
            data = self._svg(f'<use xlink:href="{href}"/>')
            self.assertIsNone(icon.sanitize_icon(data, 'image/svg+xml'), href)

    def test_same_document_href_accepted(self):
        data = self._svg('<defs><g id="a"><rect width="4" height="4"/>'
                         '</g></defs><use xlink:href="#a"/>')
        self.assertIsNotNone(icon.sanitize_icon(data, 'image/svg+xml'))

    def test_cdata_is_not_a_markup_declaration(self):
        data = self._svg('<text><![CDATA[hi]]></text>')
        self.assertIsNotNone(icon.sanitize_icon(data, 'image/svg+xml'))

if __name__ == '__main__':
    unittest.main()


class CanonicalChunkOrderTests(unittest.TestCase):
    """The gate emits documents libpng accepts: spec order, no duplicates.

    libpng fails fatally on PLTE before IHDR or on a second PLTE, so a
    rebuilt file that kept the attacker's chunk order would silently leave
    the user without an icon.
    """

    def _palette_png(self):
        plte = bytes(range(3 * 4))
        return build_png(8, 8, depth=8, color_type=3, plte=plte,
                         trns=b'\x00\x80',
                         idat=zlib.compress(_pixels(8, 8, 8, 3, 0), 9))

    def test_palette_png_round_trips_in_spec_order(self):
        safe = icon.sanitize_icon(self._palette_png(), 'image/png')
        self.assertIsNotNone(safe)
        self.assertEqual(chunk_types(safe),
                         [b'IHDR', b'PLTE', b'tRNS', b'IDAT', b'IEND'])

    def test_plte_before_ihdr_is_refused(self):
        plte = bytes(range(3 * 4))
        data = (icon._PNG_SIG + _chunk(b'PLTE', plte)
                + _chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 3, 0, 0, 0))
                + _chunk(b'IDAT', zlib.compress(_pixels(8, 8, 8, 3, 0), 9))
                + _chunk(b'IEND', b''))
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_duplicate_plte_is_refused(self):
        plte = bytes(range(3 * 4))
        body = self._palette_png()
        dup = (body[:33] + _chunk(b'PLTE', plte) + body[33:])
        self.assertIsNone(icon.sanitize_icon(dup, 'image/png'))

    def test_duplicate_trns_is_refused(self):
        plte = bytes(range(3 * 4))
        data = (icon._PNG_SIG
                + _chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 3, 0, 0, 0))
                + _chunk(b'PLTE', plte) + _chunk(b'tRNS', b'\x00')
                + _chunk(b'tRNS', b'\x00')
                + _chunk(b'IDAT', zlib.compress(_pixels(8, 8, 8, 3, 0), 9))
                + _chunk(b'IEND', b''))
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))

    def test_missing_iend_is_refused(self):
        data = (icon._PNG_SIG
                + _chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 6, 0, 0, 0))
                + _chunk(b'IDAT', zlib.compress(_pixels(8, 8, 8, 6, 0), 9)))
        self.assertIsNone(icon.sanitize_icon(data, 'image/png'))


class SvgEncodingTests(unittest.TestCase):
    """The byte scans must agree with the parser's reading of the bytes."""

    def test_utf16_svg_is_refused(self):
        # a UTF-16 document writes '<','!' as NUL-prefixed pairs, so it
        # would carry a DOCTYPE/ENTITY past the raw byte scans
        doc = ('<?xml version="1.0" encoding="UTF-16"?>'
               '<!DOCTYPE svg [<!ENTITY a "aaaa">]>'
               '<svg xmlns="http://www.w3.org/2000/svg" width="16" '
               'height="16"/>')
        data = doc.encode('utf-16')
        self.assertIn(b'\x00', data)
        self.assertIsNone(icon.sanitize_icon(data, 'image/svg+xml'))

    def test_stylesheet_instruction_is_refused(self):
        data = (b'<?xml version="1.0"?>'
                b'<?xml-stylesheet type="text/css" href="http://x/y.css"?>'
                b'<svg xmlns="http://www.w3.org/2000/svg" width="16" '
                b'height="16"/>')
        self.assertIsNone(icon.sanitize_icon(data, 'image/svg+xml'))

    def test_plain_utf8_svg_is_still_accepted(self):
        data = (b'<svg xmlns="http://www.w3.org/2000/svg" width="16" '
                b'height="16"><rect width="16" height="16" fill="#000"/>'
                b'</svg>')
        self.assertIsNotNone(icon.sanitize_icon(data, 'image/svg+xml'))


class InstalledIconFormatTests(unittest.TestCase):
    """The format of a retained icon: the file name decides, because the
    shell probes the extension; the content decides when the name says
    nothing."""

    def test_the_extension_decides_the_format(self):
        self.assertEqual(icon.installed_icon_format('app.png', b''),
                         'image/png')
        self.assertEqual(icon.installed_icon_format('app.svg', b''),
                         'image/svg+xml')
        self.assertEqual(icon.installed_icon_format('app.PNG', b''),
                         'image/png')

    def test_the_content_decides_when_the_name_says_nothing(self):
        self.assertEqual(icon.installed_icon_format('app', build_png(8, 8)),
                         'image/png')
        self.assertIsNone(icon.installed_icon_format('app', b'not an image'))
        self.assertIsNone(icon.installed_icon_format('app.txt', b'x' * 16))

    def test_a_named_file_is_judged_as_its_name_claims(self):
        # the shell decodes <stem>.png as a PNG whatever the bytes say,
        # so the name decides and a mismatch is refused, never re-typed
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="16" '
               b'height="16"/>')
        self.assertIsNone(icon.sanitize_icon(svg, 'image/png'))


class SanitizeFixedPointTests(unittest.TestCase):
    """The sanitiser is a fixed point: sanitising an already-sanitised
    file returns it unchanged. That is what lets the sweep compare the
    bytes and leave a clean icon alone instead of rewriting it."""

    def test_png_is_unchanged_by_a_second_pass(self):
        once = icon.sanitize_icon(build_png(8, 8), 'image/png')
        self.assertIsNotNone(once)
        self.assertEqual(icon.sanitize_icon(once, 'image/png'), once)

    def test_svg_is_unchanged_by_a_second_pass(self):
        svg = (b'<svg xmlns="http://www.w3.org/2000/svg" width="16" '
               b'height="16"/>')
        self.assertEqual(icon.sanitize_icon(svg, 'image/svg+xml'), svg)


class RetainedIconRegistryTests(FakeXDGTestCase):
    """The sanitised-icon registry + the retained-icon verdict
    (janitor.verify_retained_icon lives on janitor, but its primitives —
    the registry path, load/save, the capped O_NOFOLLOW read and the
    atomic rewrite — are this module's, so they are pinned here).

    FakeXDGTestCase sandboxes $XDG_STATE_HOME, so every load/save below
    resolves inside the sandbox — these tests must never write the real
    user state dir.

    The verdict is the display gate: the listing reports True only for a
    file THIS gate version has proved, so the shell never opens a
    retained icon during a throttle window, past the sweep cap, or after
    a failed rewrite."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='icon-registry-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.registry = icon.sanitized_registry_path()
        os.makedirs(os.path.dirname(self.registry), exist_ok=True)

    def _write_icon(self, name='icon.png', data=None):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as handle:
            handle.write(data if data is not None else build_png(8, 8))
        return path

    def _entry(self, path):
        st = os.lstat(path)
        return {'v': icon.SANITIZER_VERSION, 's': st.st_size,
                'm': st.st_mtime_ns}

    def test_registry_round_trips(self):
        reg = {'/a/icon.png': self._entry('/a/icon.png')
               if os.path.exists('/a/icon.png')
               else {'v': 2, 's': 10, 'm': 20}}
        icon.save_sanitized_registry(reg)
        self.assertEqual(icon.load_sanitized_registry(), reg)

    def test_corrupt_registry_loads_as_empty(self):
        for content in ('', 'not json', '[1, 2]', '{"a": 3}'):
            with open(self.registry, 'w') as handle:
                handle.write(content)
            with self.subTest(content=content):
                self.assertEqual(icon.load_sanitized_registry(), {})

    def test_malformed_entries_are_filtered_not_fatal(self):
        with open(self.registry, 'w') as handle:
            handle.write('{"a": "str", "b": {"v": 1, "s": 2, "m": 3}}')
        self.assertEqual(icon.load_sanitized_registry(),
                         {'b': {'v': 1, 's': 2, 'm': 3}})

    def test_missing_registry_loads_as_empty(self):
        self.assertEqual(icon.load_sanitized_registry(), {})

    def test_save_never_raises_on_an_unwritable_path(self):
        # a directory squatting on the registry path: the save is
        # best-effort by contract
        os.makedirs(os.path.dirname(self.registry), exist_ok=True)
        os.mkdir(self.registry)
        self.addCleanup(os.rmdir, self.registry)
        icon.save_sanitized_registry({'/a.png': {'v': 2, 's': 1, 'm': 1}})

    def test_save_only_writes_well_formed_entries(self):
        icon.save_sanitized_registry({
            '/good.png': {'v': icon.SANITIZER_VERSION, 's': 12, 'm': 34},
            '/junk.png': 'not a dict',
            42: {'v': 2, 's': 1, 'm': 1},
            '/half.png': {'v': 'two', 's': 1, 'm': 1},
        })
        self.assertEqual(icon.load_sanitized_registry(),
                         {'/good.png': {'v': icon.SANITIZER_VERSION,
                                        's': 12, 'm': 34}})


class RetainedIconVerdictTests(FakeXDGTestCase):
    """janitor.verify_retained_icon: the per-file display verdict.
    Sandboxed like the registry tests — a verdict that mutates the map
    must never be able to persist into the real user state dir."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='icon-verdict-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write_icon(self, data=None, name='icon.png'):
        path = os.path.join(self.tmp, name)
        with open(path, 'wb') as handle:
            handle.write(data if data is not None else build_png(8, 8))
        return path

    def _entry(self, path):
        st = os.lstat(path)
        return {'v': icon.SANITIZER_VERSION, 's': st.st_size,
                'm': st.st_mtime_ns}

    def test_safe_file_is_proved_and_recorded(self):
        path = self._write_icon()
        reg = {}
        self.assertTrue(janitor.verify_retained_icon(path, reg))
        self.assertEqual(reg[path], self._entry(path))

    def test_fast_path_answers_without_reading_or_decoding_the_file(self):
        # poising BOTH the read and the sanitiser proves the fast path
        # end to end: if the verdict so much as opened the file, the
        # retained_bytes raise would fail the call
        path = self._write_icon()
        reg = {path: self._entry(path)}
        with mock.patch.object(icon, 'retained_bytes',
                               side_effect=AssertionError('read')), \
                mock.patch.object(icon, 'sanitize_icon',
                                  side_effect=AssertionError('decode')):
            self.assertTrue(janitor.verify_retained_icon(path, reg))
        self.assertEqual(reg[path], self._entry(path))

    def test_a_clean_verdict_reports_no_mutation(self):
        # the listing skips the registry save when nothing changed, so
        # the verdict must keep its mutation signal exact
        path = self._write_icon()
        reg = {path: self._entry(path)}
        dirty = []
        self.assertTrue(janitor.verify_retained_icon(path, reg, dirty))
        self.assertEqual(dirty, [])

    def test_a_slow_path_verdict_reports_its_mutation(self):
        path = self._write_icon()
        dirty = []
        self.assertTrue(janitor.verify_retained_icon(path, {}, dirty))
        self.assertEqual(dirty, [path])

    def test_touched_file_drops_the_entry_and_reproves(self):
        # the recorded mtime_ns no longer matches: the fast path must
        # not fire (proven by the refusal-poison) — a stale stat can
        # only earn a fresh verdict
        path = self._write_icon()
        reg = {path: self._entry(path)}
        st = os.lstat(path)
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
        with mock.patch.object(icon, 'sanitize_icon', return_value=None):
            self.assertFalse(janitor.verify_retained_icon(path, reg))
        self.assertNotIn(path, reg)

    def test_touched_safe_file_is_reproved_and_re_recorded(self):
        path = self._write_icon()
        reg = {path: dict(self._entry(path), m=1)}
        self.assertTrue(janitor.verify_retained_icon(path, reg))
        self.assertEqual(reg[path], self._entry(path))

    def test_entry_from_an_older_gate_version_is_not_trusted(self):
        path = self._write_icon()
        reg = {path: dict(self._entry(path), v=icon.SANITIZER_VERSION - 1)}
        with mock.patch.object(icon, 'sanitize_icon', return_value=None):
            self.assertFalse(janitor.verify_retained_icon(path, reg))
        self.assertNotIn(path, reg)

    def test_refused_bytes_drop_the_entry_and_keep_the_file(self):
        # truncated PNG: the sanitiser refuses it, the verdict is False,
        # and the FILE SURVIVES — deletion belongs to the sweep; a
        # refusal costs a glyph, never a file
        data = build_png(8, 8)[:-4]
        path = self._write_icon(data)
        reg = {}
        self.assertFalse(janitor.verify_retained_icon(path, reg))
        self.assertNotIn(path, reg)
        self.assertTrue(os.path.exists(path))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), data)

    def test_failed_rewrite_drops_the_entry_and_keeps_the_bytes(self):
        bomb = zlib.compress(b'A' * (1024 * 1024), 9)
        data = build_png(8, 8, extra=[(b'zTXt', b'C\x00\x00' + bomb)])
        path = self._write_icon(data)
        reg = {}
        with mock.patch.object(icon, 'replace_icon_file',
                               side_effect=OSError(errno.ENOSPC,
                                                   'no space')):
            self.assertFalse(janitor.verify_retained_icon(path, reg))
        self.assertNotIn(path, reg)
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), data)

    def test_missing_path_is_false(self):
        reg = {'/gone/icon.png': {'v': icon.SANITIZER_VERSION,
                                  's': 1, 'm': 1}}
        self.assertFalse(janitor.verify_retained_icon(
            os.path.join(self.tmp, 'absent.png'), reg))
        self.assertFalse(janitor.verify_retained_icon(
            os.path.join(self.tmp, 'absent.png'), {}))

    def test_symlink_is_never_followed(self):
        outside = os.path.join(self.tmp, 'outside.png')
        with open(outside, 'wb') as handle:
            handle.write(build_png(8, 8))
        link = os.path.join(self.tmp, 'link.png')
        os.symlink(outside, link)
        reg = {}
        self.assertFalse(janitor.verify_retained_icon(link, reg))
        self.assertNotIn(link, reg)
        self.assertTrue(os.path.islink(link))

    def test_rewritten_file_records_the_new_stat(self):
        bomb = zlib.compress(b'A' * (1024 * 1024), 9)
        path = self._write_icon(
            build_png(8, 8, extra=[(b'zTXt', b'C\x00\x00' + bomb)]))
        reg = {}
        self.assertTrue(janitor.verify_retained_icon(path, reg))
        self.assertEqual(reg[path], self._entry(path))
        with open(path, 'rb') as handle:
            rewritten = handle.read()
        self.assertNotIn(b'zTXt', rewritten)


class BothCandidatesVerdictTests(FakeXDGTestCase):
    """Ticket #63: the listing verifies EACH existing candidate, not just
    the first — a .png that fails to DECODE makes the row's Image fall
    through to the .svg, so the .svg needs its own verdict (and its own
    registry entry)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix='icon-candidates-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.png = os.path.join(self.tmp, 'app.png')
        self.svg = os.path.join(self.tmp, 'app.svg')

    def _write(self, path, data):
        with open(path, 'wb') as handle:
            handle.write(data)

    def _valid_svg(self):
        return (b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<svg xmlns="http://www.w3.org/2000/svg" '
                b'width="8" height="8" viewBox="0 0 8 8">'
                b'<rect width="8" height="8" fill="#123456"/></svg>')

    def test_both_candidates_verified_with_two_registry_entries(self):
        self._write(self.png, build_png(8, 8))
        self._write(self.svg, self._valid_svg())
        reg, dirty = {}, []
        self.assertTrue(janitor.verify_retained_icon(self.png, reg, dirty))
        self.assertTrue(janitor.verify_retained_icon(self.svg, reg, dirty))
        self.assertIn(self.png, reg)
        self.assertIn(self.svg, reg)
        self.assertEqual(sorted(dirty), sorted([self.png, self.svg]))

    def test_refusing_the_svg_drops_only_the_svg_entry(self):
        self._write(self.png, build_png(8, 8))
        self._write(self.svg, b'<svg xmlns="http://www.w3.org/2000/svg"'
                              b' width="8" height="8"><script/></svg>')
        reg, dirty = {}, []
        self.assertTrue(janitor.verify_retained_icon(self.png, reg, dirty))
        self.assertFalse(janitor.verify_retained_icon(self.svg, reg, dirty))
        self.assertIn(self.png, reg)
        self.assertNotIn(self.svg, reg)
        self.assertEqual(dirty, [self.png])
        # the refused svg file itself survives — deletion is the sweep's
        self.assertTrue(os.path.exists(self.svg))


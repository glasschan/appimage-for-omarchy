"""Icon sanitisation: provider bytes never reach the shell's decoder unbounded."""

import struct
import unittest
import zlib

import helpers  # noqa: F401  — puts backend/ on sys.path (as the other suites do)

from omarchy_appimage import icon
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
              extra=(), plte=None, trns=None, idat=None, trailing=b''):
    ihdr = struct.pack('>IIBBBBB', width, height, depth, color_type, 0, 0,
                       interlace)
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

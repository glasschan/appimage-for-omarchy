# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
"""Provider-supplied icon bytes are made safe to hand to the shell decoder.

An icon is read out of an AppImage the plugin downloaded from a release
host, written next to the installed AppImage and then decoded by the
omarchy shell's QML ``Image`` element (Panel.qml, ``.icons/<base>.png``).
The decoders are libpng and Qt's SVG renderer, and neither is given a
memory budget by us:

* PNG dimensions are taken from IHDR, and libpng allocates the frame
  before it has seen any pixel. A small file can therefore ask for
  gigabytes (``struct``: 60000x60000 needs 14.4 GB at 4 bytes/pixel).
* Every textual/ICC/EXIF chunk (``tEXt``/``zTXt``/``iTXt``/``iCCP``/``eXIf``)
  is inflated and kept in memory as metadata. libpng limits a *single*
  chunk's inflation, not the sum, so many tiny chunks in one 8 MiB file —
  the extractor's ``MAX_ICON_BYTES`` cap, which counts *stored* bytes only
  — expand to an arbitrary total.
* An SVG declares its own size, and Qt rasterises at the intrinsic size.

``sanitize_icon()`` is the single gate on that boundary. For PNG it
rebuilds the file from scratch, keeping only the chunks a decoder needs to
paint pixels (IHDR, PLTE, tRNS, IDAT, IEND) and dropping every ancillary
chunk, then proves the declared geometry against the actual inflated pixel
stream (exact size, including Adam7 interlacing) so neither a lying IHDR
nor an IDAT bomb can pass. For SVG it refuses every markup declaration
(DOCTYPE/ENTITY and friends), requires a bounded root size, and walks the
whole tree: <image>, <script>, <style>, <filter> and filter primitives are
refused because Qt renders an embedded raster at its own natural size, and
only same-document `href="#id"` references are allowed. Anything that
cannot be bounded is refused: the panel then falls back to the plugin's
own Tabler glyph, so a refusal costs an icon, never a feature.

The same gate covers the icons that are ALREADY on disk: an icon
installed by an older plugin version never went through it, and the
shell decodes whatever sits in the icons folder on every panel open.
janitor.sweep_icons runs this sanitiser over those retained icons and
removes the ones it refuses — and the listing verifies each icon a row
would load (`janitor.verify_retained_icon`, recorded in the
``sanitized-icons.json`` registry), so the frontend only ever opens a
file the CURRENT sanitiser has proved.
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import tempfile
import zlib
from typing import Optional, Tuple
from xml.etree import ElementTree

from . import constants, utils

# Ceilings a rail: 1024x1024 is far above any real app icon (the extractor
# looks for 512/256/128/96 hicolor sizes) and caps a decode at 4 MiB RGBA.
MAX_SIDE = 1024
MAX_PIXELS = MAX_SIDE * MAX_SIDE
MAX_SVG_BYTES = 512 * 1024

_PNG_SIG = b'\x89PNG\r\n\x1a\n'
# Chunks a decoder needs to paint pixels. Everything else is metadata that
# would be materialised in the decoder's memory, so it is dropped rather
# than trusted.
_KEEP = frozenset({b'IHDR', b'PLTE', b'tRNS', b'IDAT', b'IEND'})
_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_DEPTHS = (1, 2, 4, 8, 16)
# Adam7: (x start, y start, x step, y step)
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
          (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))

# Only unitless or px lengths are bounded pixel sizes; percentages, em and
# friends are relative to something we do not control, so they are refused.
_DIM_RE = re.compile(r'^\s*([0-9]+(?:\.[0-9]+)?)\s*(?:px)?\s*$')


class _Unsafe(ValueError):
    """Raised internally when icon bytes cannot be bounded."""


def sanitize_icon(data: bytes, fmt: Optional[str]) -> Optional[bytes]:
    """Return icon bytes that are safe to decode, or None to refuse.

    `fmt` is the result of extractor.image_format() for `data`; only PNG and
    SVG are accepted (they are the two formats `_find_icon` returns).
    """
    try:
        if fmt == 'image/png':
            return _sanitize_png(data)
        if fmt == 'image/svg+xml':
            return _sanitize_svg(data)
    except (_Unsafe, zlib.error, struct.error, ValueError):
        return None
    return None


def _sanitize_png(data: bytes) -> bytes:
    ihdr = None
    plte = None
    trns = None
    plte_len = None
    trns_len = None
    idats = []
    iend = None
    for index, (ctype, raw) in enumerate(_chunks(data)):
        if not index and ctype != b'IHDR':
            # the spec pins IHDR as the first chunk; anything else is a
            # file libpng rejects, and this module must never emit one
            raise _Unsafe('IHDR is not the first chunk')
        if ctype == b'IHDR':
            if ihdr is not None:
                raise _Unsafe('duplicate IHDR')
            ihdr = raw
        elif ctype not in _KEEP:
            continue
        elif ctype == b'PLTE':
            if plte is not None:
                raise _Unsafe('duplicate PLTE')
            plte_len = len(raw) - 12
            if plte_len % 3 or not 3 <= plte_len <= 768:
                raise _Unsafe('bad PLTE length')
            plte = raw
        elif ctype == b'tRNS':
            if trns is not None:
                raise _Unsafe('duplicate tRNS')
            trns_len = len(raw) - 12
            trns = raw
        elif ctype == b'IDAT':
            idats.append(raw)
        elif ctype == b'IEND':
            iend = raw
            break
    if ihdr is None:
        raise _Unsafe('no IHDR')
    if not idats:
        raise _Unsafe('no IDAT')
    if iend is None:
        raise _Unsafe('no IEND')
    width, height, depth, color_type, interlace = _parse_ihdr(ihdr[8:-4])
    # The palette and the transparency table are part of the file, not of
    # the geometry, so libpng allocates them as written: bound them to
    # what the spec allows instead of trusting the length field.
    if color_type == 3:
        if plte_len is None:
            raise _Unsafe('palette image without PLTE')
    elif color_type in (0, 4) and plte_len is not None:
        raise _Unsafe('PLTE on a greyscale image')
    if trns_len is not None:
        if color_type == 0 and trns_len != 2:
            raise _Unsafe('bad tRNS length')
        if color_type == 2 and trns_len != 6:
            raise _Unsafe('bad tRNS length')
        if color_type == 3 and trns_len > plte_len // 3:
            raise _Unsafe('tRNS exceeds the palette')
        if color_type in (4, 6):
            raise _Unsafe('tRNS on an image with alpha')
    expected = _raw_size(width, height, depth, color_type, interlace)
    inflater = zlib.decompressobj()
    out = inflater.decompress(b''.join(raw[8:-4] for raw in idats),
                              expected + 1)
    if (len(out) != expected or not inflater.eof
            or inflater.unconsumed_tail or inflater.unused_data):
        raise _Unsafe('pixel stream does not match the declared geometry')
    del out
    # Emit the canonical chunk order, never the attacker's: libpng
    # fatally rejects PLTE before IHDR, and a duplicated palette, which
    # would silently leave the user without an icon.
    parts = [ihdr]
    if plte is not None:
        parts.append(plte)
    if trns is not None:
        parts.append(trns)
    parts.extend(idats)
    parts.append(iend)
    return _PNG_SIG + b''.join(parts)


def _chunks(data: bytes):
    if data[:8] != _PNG_SIG:
        raise _Unsafe('not a PNG')
    pos = 8
    while True:
        if pos + 12 > len(data):
            raise _Unsafe('truncated chunk')
        (length,) = struct.unpack('>I', data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        if not ctype.isalpha():
            raise _Unsafe('bad chunk type')
        end = pos + 12 + length
        if end > len(data):
            raise _Unsafe('truncated chunk')
        yield ctype, data[pos:end]
        if ctype == b'IEND':
            return
        pos = end


def _parse_ihdr(body: bytes):
    if len(body) != 13:
        raise _Unsafe('bad IHDR length')
    width, height, depth, color_type, _comp, _filt, interlace = \
        struct.unpack('>IIBBBBB', body)
    if depth not in _DEPTHS or color_type not in _CHANNELS:
        raise _Unsafe('unsupported depth/colour type')
    if color_type == 3 and depth > 8:
        raise _Unsafe('palette depth')
    if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE):
        raise _Unsafe(f'{width}x{height} exceeds {MAX_SIDE}')
    if width * height > MAX_PIXELS:
        raise _Unsafe(f'{width}x{height} exceeds {MAX_PIXELS} pixels')
    if interlace not in (0, 1):
        raise _Unsafe('unsupported interlace method')
    return width, height, depth, color_type, interlace


def _raw_size(width: int, height: int, depth: int, color_type: int,
              interlace: int) -> int:
    """Exact decompressed size the PNG spec predicts for this geometry."""
    bits = _CHANNELS[color_type] * depth
    if not interlace:
        return height * _scanline(width, bits)
    total = 0
    for xs, ys, dx, dy in _ADAM7:
        pass_w = 0 if width <= xs else -(-(width - xs) // dx)
        pass_h = 0 if height <= ys else -(-(height - ys) // dy)
        if pass_w and pass_h:
            total += pass_h * _scanline(pass_w, bits)
    return total


def _scanline(width: int, bits_per_pixel: int) -> int:
    # one filter-type byte per scanline, then the packed pixels
    return 1 + (width * bits_per_pixel + 7) // 8


# The two '<!' forms an SVG icon may legally contain. Everything else is
# a markup declaration (DOCTYPE, ENTITY, ELEMENT, ...) and is refused: an
# ENTITY lets the parser expand a few bytes into gigabytes, and the bytes
# that matter are the ones the shell's decoder will read.
_COMMENT_OR_CDATA = re.compile(rb'<!--.*?-->|<!\[CDATA\[.*?\]\]>', re.S)

# Elements that turn a picture into a program or an unbounded
# rasterisation. Qt renders <image> at the embedded raster's NATURAL size,
# not at the element's width/height: measured with Qt 6.12, a 243 KB SVG
# whose single <image> carried an 8000x8000 PNG in a data: URI was
# accepted by the dimension gate and took the decoding process from 17 MB
# to 276 MB VmHWM. A real app icon needs none of these.
_SVG_FORBIDDEN = frozenset({'image', 'script', 'style', 'filter',
                            'foreignobject'})

def _local_name(name: str) -> str:
    """Local part of a possibly namespaced XML name, lower-cased."""
    return name.rsplit('}', 1)[-1].lower()

def _assert_svg_tree_safe(root) -> None:
    """Every element and every reference, not just the root's numbers."""
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        name = _local_name(element.tag)
        if name in _SVG_FORBIDDEN or name.startswith('fe'):
            raise _Unsafe(f'SVG <{name}> refused')
        for attr, value in element.attrib.items():
            if _local_name(attr) != 'href':
                continue
            # Same-document references only (xlink:href="#id"): an
            # external or data: URI is fetched/decoded by the renderer,
            # outside every bound this module can enforce.
            if not value.strip().startswith('#'):
                raise _Unsafe('SVG external reference refused')

def _sanitize_svg(data: bytes) -> bytes:
    if len(data) > MAX_SVG_BYTES:
        raise _Unsafe('SVG too large')
    # The checks below scan bytes, so the encoding must be one where a
    # byte scan and the parser agree: UTF-16/32 writes '<', '!' and '?' as
    # '\x00<'-style pairs, which would carry a DOCTYPE and its ENTITY
    # declarations straight past them.
    if b'\x00' in data[:64]:
        raise _Unsafe('SVG encoding must be UTF-8')
    if b'<!' in _COMMENT_OR_CDATA.sub(b'', data):
        raise _Unsafe('SVG markup declaration refused')
    # A processing instruction is the one external-URI form that is not
    # an attribute, so the href rule in _assert_svg_tree_safe never sees
    # it (Qt ignores PIs today; this keeps the rule total).
    if b'<?xml-stylesheet' in data:
        raise _Unsafe('SVG stylesheet instruction refused')
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise _Unsafe(f'bad SVG XML ({exc})')
    if _local_name(root.tag if isinstance(root.tag, str) else '') != 'svg':
        raise _Unsafe('SVG not rooted at <svg>')
    _assert_svg_tree_safe(root)
    width = _dim(root.get('width'))
    height = _dim(root.get('height'))
    if width is None or height is None:
        viewbox = (root.get('viewBox') or '').replace(',', ' ').split()
        if len(viewbox) == 4:
            width = width if width is not None else _dim(viewbox[2])
            height = height if height is not None else _dim(viewbox[3])
    if not width or not height:
        raise _Unsafe('SVG has no bounded size')
    if (width > MAX_SIDE or height > MAX_SIDE
            or width * height > MAX_PIXELS):
        raise _Unsafe(f'{width}x{height} exceeds the icon ceiling')
    return data
def _dim(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = _DIM_RE.match(value)
    if not match:
        return None  # percentages, em, "auto": not a bounded pixel size
    number = float(match.group(1))
    return number or None


def installed_icon_format(name: str, data: bytes) -> Optional[str]:
    """The format of an icon already on disk, or None when it is unknown.

    The file name is the primary signal, because the shell probes the
    extension (Panel.qml tries `.png`, then `.svg`): the panel decodes
    whatever sits at `<stem>.png`, so that name decides how the bytes
    must be judged. A name that says nothing is decided by the content
    (the PNG magic). Anything else is unsanitisable — the sanitiser has
    no format to work with, and the shell would not decode it either.

    `installed_icon_is_unsafe` used to live here: a header-only backstop
    that judged a retained icon from its IHDR alone. It is gone because
    the sweep now runs the FULL sanitiser over the retained icons
    (janitor.sweep_icons), and a header check cannot see the metadata
    chunks that made a retained icon unbounded. Nothing else used it.
    """
    ext = os.path.splitext(name)[1].lower()
    if ext == '.png':
        return 'image/png'
    if ext == '.svg':
        return 'image/svg+xml'
    if data[:8] == _PNG_SIG:
        return 'image/png'
    return None


# --------------------------------------------------------------------------
# Retained icons: read, atomic replace, and the sanitisation registry.
#
# The sweep (janitor.sweep_icons) and the listing verdict
# (janitor.verify_retained_icon, reported as the app object's additive
# `icon_safe`) share the primitives below, so the sweep and the listing
# read and rewrite a retained icon identically — and the frontend only
# opens a file whose sanitisation the CURRENT gate has proved.

# Bump on ANY change to sanitize_icon's verdict logic. Stored in the
# registry (and in the sweep-throttle marker) so icons proven by an older
# gate re-verify under the new one. 1 = the v0.9.9 extract-time gate,
# 2 = this display gate (the retained-icon verdict + registry).
SANITIZER_VERSION = 2


def sanitized_registry_path() -> str:
    """The sanitised-icon registry: plugin state dir, never /tmp."""
    return os.path.join(constants.plugin_state_dir(), 'sanitized-icons.json')


def load_sanitized_registry() -> dict:
    """The persisted `{path: {v, s, m}}` verdicts, or {} on ANY failure.

    A registry is bookkeeping, never a source of truth: a missing,
    unreadable, corrupt, or wrongly-shaped file costs a re-verification
    (one sanitiser pass per icon), never an error."""
    try:
        with open(sanitized_registry_path(), 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items()
            if isinstance(key, str) and isinstance(value, dict)}


def save_sanitized_registry(reg: dict):
    """Persist the registry atomically, best-effort; never raises.

    Only well-formed entries are written (string path, dict entry with
    int v/s/m), so a hand-edited or programmatically polluted map cannot
    poison the file a later load would have to trust-shape."""
    clean = {}
    for key, entry in (reg or {}).items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            continue
        v, s, m = entry.get('v'), entry.get('s'), entry.get('m')
        if all(isinstance(x, int) and not isinstance(x, bool)
               for x in (v, s, m)):
            clean[key] = {'v': v, 's': s, 'm': m}
    try:
        utils.atomic_write(sanitized_registry_path(),
                           json.dumps(clean).encode('utf-8'))
    except OSError as e:
        logging.debug('icon: could not save the sanitised-icon registry: %s',
                      e)


def retained_bytes(path: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Read one retained icon the way the sweep and the verdict both do.

    lstat size check against extractor.MAX_ICON_BYTES (oversized is
    unusable, never read), then an O_NOFOLLOW open capped at
    MAX_ICON_BYTES + 1 (a symlink planted in place is never followed),
    then the format from the file name (`installed_icon_format`). Returns
    (data, fmt); (None, None) when the file is unusable — the caller
    decides skip vs. remove. Never raises."""
    # imported here, not at module level: extractor imports this module's
    # sanitiser, so a top-level import would be circular
    from . import extractor
    try:
        st = os.lstat(path)
        if st.st_size > extractor.MAX_ICON_BYTES:
            return None, None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            data = handle.read(extractor.MAX_ICON_BYTES + 1)
    except OSError:
        return None, None
    return data, installed_icon_format(os.path.basename(path), data)


# Prefix of the temporary file an atomic icon rewrite writes first. It is
# hidden (the sweep skips hidden names) and lives in the icons folder
# itself, because os.replace is only atomic within one filesystem.
ICON_TMP_PREFIX = '.icon-sweep-'


def replace_icon_file(path: str, data: bytes, mode: int):
    """Replace an icon with `data` atomically.

    The new bytes land in a temporary file in the SAME directory
    (os.replace is only atomic within one filesystem) and are renamed
    over the old file, so a crash never leaves a half-written icon for
    the shell to decode. The temporary name is hidden and carries this
    module's own prefix, so a concurrent sweep never mistakes it for an
    icon. Raises on failure (OSError); callers decide what that costs."""
    directory = os.path.dirname(path) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix=ICON_TMP_PREFIX, dir=directory)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


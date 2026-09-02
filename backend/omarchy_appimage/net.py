# net.py — hardened HTTP helpers on top of urllib (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# The `requests` calls scattered across GearLever's updaters
# (models/*Updater.py) are funnelled through this module so every request
# shares one User-Agent, one timeout policy and one error shape.
#
# Security model (marketplace security-review hardening):
#   * https-only egress — http://, file://, ftp:// and everything else is
#     rejected before a socket is opened;
#   * SSRF guard — every hostname is resolved locally and *every* returned
#     address must be a global (public) IP, so loopback, RFC1918,
#     link-local, CGNAT, multicast and reserved targets are unreachable;
#   * DNS-rebinding-safe dialing — the raw socket is pinned to one of the
#     validated addresses (TLS still verifies the original hostname via
#     SNI/cert checks), so the resolve-then-connect TOCTOU window is gone;
#   * manual redirects — max 5 hops, each hop re-validated;
#   * byte caps — metadata responses are capped at MAX_METADATA_BYTES,
#     downloads at MAX_DOWNLOAD_BYTES (a running cap aborts mid-stream
#     even when the server lies about or omits Content-Length).

import http.client
import ipaddress
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import constants

USER_AGENT = (f'{constants.APP_ID}/{constants.BACKEND_VERSION} '
              '(+https://github.com/glasschan/appimage-for-omarchy)')

DEFAULT_TIMEOUT = 20

# Body caps: metadata responses (release JSON, zsync control files) are
# tiny; AppImage downloads are the only legitimately large payloads.
MAX_METADATA_BYTES = 4 * 1024 * 1024            # 4 MiB
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024     # 4 GiB

_MAX_REDIRECTS = 5
_CHUNK = 64 * 1024

# Test seam: the guard makes loopback http.server fixture servers
# (http://127.0.0.1:port — plain http on a private address) unreachable.
# In-process tests flip this module flag via unittest.mock; subprocess
# tests can set OMARCHY_APPIMAGE_ALLOW_LOCAL_HTTP=1 in the environment.
# Nothing in production ever sets either: with the seam off, BOTH
# relaxations are inactive (https-only egress, public addresses only).
_TEST_ALLOW_LOCAL = bool(os.environ.get('OMARCHY_APPIMAGE_ALLOW_LOCAL_HTTP'))
if _TEST_ALLOW_LOCAL:
    # loud so an accidentally exported variable never silences the guard
    logging.warning(
        '%s is set: the https-only and public-address transport guards '
        'are DISABLED in this process (test seam — unset it outside the '
        'test suite)', 'OMARCHY_APPIMAGE_ALLOW_LOCAL_HTTP')

# Small, fast endpoints used only to answer "is there a network at all"
# (mirrors GearLever's lib/utils.check_internet).
_CONNECTIVITY_PROBES = (
    'https://api.github.com/',
    'https://fedoraproject.org/static/hotspot.txt',
)


class NetworkError(Exception):
    """A request failed (DNS, timeout, HTTP error status, SSRF guard,
    response over the size cap)."""


def _addr_allowed(ip) -> bool:
    """True when the resolved address may be dialed. `is_global` covers
    loopback, RFC1918, link-local, CGNAT, unspecified and reserved
    ranges; multicast is rejected explicitly (Python flags globally
    routed multicast scopes as is_global)."""
    if _TEST_ALLOW_LOCAL:
        return True
    return ip.is_global and not ip.is_multicast


def validate_remote_host(host: str) -> None:
    """Resolve `host` and require every returned address to be public.

    Raises NetworkError for loopback, private, link-local, CGNAT,
    multicast, unspecified and reserved addresses, and for names that do
    not resolve. This is the eager half of the SSRF guard;
    _PinnedHTTPSConnection enforces the same check again at dial time."""
    try:
        infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except OSError as e:
        raise NetworkError(f'{host}: cannot resolve ({e})') from e
    if not infos:
        raise NetworkError(f'{host}: resolves to no addresses')
    for *_family, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not _addr_allowed(ip):
            raise NetworkError(f'{host}: {ip} is not a public address '
                               '(SSRF guard)')


def _validate_url(url: str) -> None:
    """Eager pre-flight check for request and redirect URLs: https-only
    transport, no userinfo in the authority, host resolvable to public
    addresses only (raises NetworkError)."""
    parts = urlsplit(url)
    if parts.scheme != 'https' \
            and not (_TEST_ALLOW_LOCAL and parts.scheme == 'http'):
        raise NetworkError(f'{url}: only https:// URLs are allowed')
    if parts.username or parts.password:
        raise NetworkError(f'{url}: userinfo in URLs is not allowed')
    if not parts.hostname:
        raise NetworkError(f'{url}: URL has no host')
    validate_remote_host(parts.hostname)


def _dial_validated(host: str, port: int, timeout) -> socket.socket:
    """Resolve `host`, validate every address, then connect a raw socket
    to one of the validated IPs — NOT to the hostname (that is what makes
    a rebinding TOCTOU moot). Returns the connected, unwrapped socket."""
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except OSError as e:
        raise NetworkError(f'{host}: cannot resolve ({e})') from e
    if not infos:
        raise NetworkError(f'{host}: resolves to no addresses')

    last_error = None
    for family, socktype, proto, _canonname, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not _addr_allowed(ip):
            raise NetworkError(f'{host}: {ip} is not a public address '
                               '(SSRF guard)')
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(timeout)
            sock.connect(sockaddr)
            return sock
        except OSError as e:
            last_error = e
            try:
                sock.close()
            except OSError:
                pass
    raise NetworkError(f'{host}: connection failed ({last_error})')


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that dials a validated IP (see _dial_validated)
    and then TLS-wraps with the original hostname, so SNI and certificate
    verification keep working against the name the user asked for."""

    def connect(self):
        self.sock = self._context.wrap_socket(
            _dial_validated(self.host, self.port, self.timeout),
            server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """Routes every https request through _PinnedHTTPSConnection (no
    global urlopen: one hardened opener lives at module level)."""

    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req,
                            context=self._context)


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Manual redirect handling: the chain is capped at _MAX_REDIRECTS
    hops and every hop is re-validated eagerly (https-only, no userinfo,
    public addresses) — the pinned connection re-checks at dial time, the
    eager check just makes the failure message point at the right hop."""

    max_hops = _MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        hops = getattr(req, 'redirect_hops', 0) + 1
        if hops > self.max_hops:
            raise NetworkError(f'{req.full_url}: more than '
                               f'{self.max_hops} redirects')
        _validate_url(newurl)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.redirect_hops = hops
        return new


_opener = urllib.request.build_opener(_ValidatingRedirectHandler(),
                                      _PinnedHTTPSHandler())


def _open(url: str, method: str = 'GET', timeout: float = DEFAULT_TIMEOUT):
    _validate_url(url)
    request = urllib.request.Request(
        url, method=method, headers={'User-Agent': USER_AGENT})
    return _opener.open(request, timeout=timeout)


def _content_length(response):
    """The declared Content-Length as int, or None (absent/garbage)."""
    try:
        length = int(response.headers.get('Content-Length') or 0)
    except ValueError:
        return None
    return length or None


def _unlink_quietly(path: str):
    try:
        os.unlink(path)
    except OSError:
        pass


def fetch_text(url: str, timeout: float = DEFAULT_TIMEOUT,
               max_bytes: int = MAX_METADATA_BYTES) -> str:
    """GET `url` and return the body as text (raises NetworkError).

    The body is capped at `max_bytes`: an over-long Content-Length is
    rejected before reading, a streamed/chunked body is cut off as soon
    as the running total would exceed the cap."""
    try:
        with _open(url, timeout=timeout) as response:
            declared = _content_length(response)
            if declared is not None and declared > max_bytes:
                raise NetworkError(f'{url}: response too large '
                                   f'({declared} > {max_bytes} bytes)')
            chunks = []
            total = 0
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise NetworkError(f'{url}: response exceeds '
                                       f'{max_bytes} bytes')
                chunks.append(chunk)
    except NetworkError:
        raise
    except Exception as e:
        raise NetworkError(f'{url}: {e}') from e
    return b''.join(chunks).decode('utf-8', errors='replace')


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT):
    """GET `url` and parse the body as JSON (raises NetworkError)."""
    try:
        return json.loads(fetch_text(url, timeout=timeout))
    except json.JSONDecodeError as e:
        raise NetworkError(f'{url}: invalid JSON ({e})') from e


def head_headers(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Headers for `url` via HEAD, falling back to an un-read GET stream
    (some servers reject HEAD), like GearLever's StaticFileUpdater.

    Keys are lower-cased so callers can look them up without worrying
    about the server's header casing (a plain dict lookup would be
    case-sensitive). Returns {} when both attempts fail."""
    for method in ('HEAD', 'GET'):
        try:
            with _open(url, method=method, timeout=timeout) as response:
                return {k.lower(): v
                        for k, v in response.headers.items()}
        except Exception as e:
            logging.debug('%s %s failed: %s', method, url, e)
    return {}


def download_to_file(url: str, dest_path: str, progress_cb=None,
                     timeout: float = DEFAULT_TIMEOUT,
                     max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
    """Stream `url` to `dest_path` in 1 MiB chunks; returns the byte count.

    progress_cb(fraction) fires at most every ~1% with a value in [0, 1]
    (or not at all when the server sends no Content-Length). Raises
    NetworkError on transport failure, a short body, or a body over
    `max_bytes`; the partial `dest_path` is always removed on failure."""
    done = 0
    declared = None
    try:
        with _open(url, timeout=timeout) as response:
            declared = _content_length(response)
            if declared is not None and declared > max_bytes:
                # rejected before the local file is created or appended to
                raise NetworkError(f'{url}: download too large '
                                   f'({declared} > {max_bytes} bytes)')
            with open(dest_path, 'wb') as out:
                next_notify = 0.01
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > max_bytes:
                        raise NetworkError(f'{url}: download exceeds '
                                           f'{max_bytes} bytes')
                    out.write(chunk)
                    if declared and progress_cb \
                            and done / declared >= next_notify:
                        next_notify += 0.01
                        try:
                            progress_cb(done / declared)
                        except Exception:
                            progress_cb = None
    except NetworkError:
        _unlink_quietly(dest_path)
        raise
    except Exception as e:
        _unlink_quietly(dest_path)
        raise NetworkError(f'{url}: {e}') from e

    if declared and done < declared:
        _unlink_quietly(dest_path)
        raise NetworkError(f'{url}: connection closed early '
                           f'({done} of {declared} bytes)')
    return done


def check_internet(timeout: float = 3) -> bool:
    """True when any connectivity probe answers (best-effort, no raise)."""
    for url in _CONNECTIVITY_PROBES:
        try:
            with _open(url, method='HEAD', timeout=timeout):
                return True
        except urllib.error.HTTPError:
            # Any HTTP answer still proves there is a network path.
            return True
        except Exception:
            continue
    return False

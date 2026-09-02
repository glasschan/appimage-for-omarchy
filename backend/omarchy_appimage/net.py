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
#     even when the server lies about or omits Content-Length);
#   * absolute deadlines — timeouts are whole-operation wall-clock
#     budgets, not just per-socket-operation stall guards: a slow-drip
#     server cannot keep a request (or a multi-GiB download) alive
#     indefinitely.

import http.client
import ipaddress
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from . import constants

USER_AGENT = (f'{constants.APP_ID}/{constants.BACKEND_VERSION} '
              '(+https://github.com/glasschan/appimage-for-omarchy)')

DEFAULT_TIMEOUT = 20

# A single stalled read on a download may outlive a metadata request's
# whole budget, but must not hang forever either.
DOWNLOAD_TIMEOUT = 30
# Absolute wall-clock budget for one download: a slow-drip server must
# not be able to keep a multi-GiB transfer alive indefinitely.
DOWNLOAD_DEADLINE_SECONDS = 3600

# Body caps: metadata responses (release JSON, zsync control files) are
# tiny; AppImage downloads are the only legitimately large payloads.
MAX_METADATA_BYTES = 4 * 1024 * 1024            # 4 MiB
MAX_DOWNLOAD_BYTES = 4 * 1024 * 1024 * 1024     # 4 GiB

_MAX_REDIRECTS = 5
_CHUNK = 64 * 1024

# Test-only seam: loopback/private targets for the loopback test servers.
# NEVER set from the environment — tests patch this attribute directly
# (mock.patch.object). Production processes cannot disable the guards.
_ALLOW_LOCAL_FOR_TESTS = False

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
    if _ALLOW_LOCAL_FOR_TESTS:
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
            and not (_ALLOW_LOCAL_FOR_TESTS and parts.scheme == 'http'):
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


def _remaining_or_raise(url: str, start: float, budget: float,
                        what: str) -> float:
    """Seconds left of an absolute `budget` (seconds) that started at
    `start` (time.monotonic()); raises NetworkError once the budget is
    spent. This is what makes timeouts whole-operation deadlines: a
    slow-drip server that delivers each chunk just within the socket
    timeout still runs out of budget here."""
    remaining = budget - (time.monotonic() - start)
    if remaining <= 0:
        raise NetworkError(f'{url}: {what} deadline exceeded')
    return remaining


def _arm_socket(response, remaining: float) -> None:
    """Cap the NEXT blocking read at `remaining` seconds so a single
    socket operation cannot outrun the absolute deadline (best-effort:
    degrades to the elapsed check alone if the stdlib's
    response -> socket path ever changes)."""
    fp = getattr(response, 'fp', None)
    sock = getattr(getattr(fp, 'raw', None), '_sock', None)
    if isinstance(sock, socket.socket):
        try:
            sock.settimeout(remaining)
        except OSError:
            pass


def fetch_text(url: str, timeout: float = DEFAULT_TIMEOUT,
               max_bytes: int = MAX_METADATA_BYTES) -> str:
    """GET `url` and return the body as text (raises NetworkError).

    The body is capped at `max_bytes`: an over-long Content-Length is
    rejected before reading, a streamed/chunked body is cut off as soon
    as the running total would exceed the cap. `timeout` is an absolute
    deadline for the whole request (connect + headers + body), not a
    per-socket-operation guard."""
    chunks = []
    total = 0
    start = time.monotonic()
    try:
        with _open(url, timeout=timeout) as response:
            declared = _content_length(response)
            if declared is not None and declared > max_bytes:
                raise NetworkError(f'{url}: response too large '
                                   f'({declared} > {max_bytes} bytes)')
            while True:
                _arm_socket(response,
                            _remaining_or_raise(url, start, timeout,
                                                'request'))
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
            start = time.monotonic()
            with _open(url, method=method, timeout=timeout) as response:
                # absolute deadline for the whole attempt (the socket
                # timeout passed to _open bounds connect + headers)
                _remaining_or_raise(url, start, timeout, 'request')
                return {k.lower(): v
                        for k, v in response.headers.items()}
        except Exception as e:
            logging.debug('%s %s failed: %s', method, url, e)
    return {}


def download_to_file(url: str, dest_path: str, progress_cb=None,
                     timeout: float = DOWNLOAD_TIMEOUT,
                     max_bytes: int = MAX_DOWNLOAD_BYTES,
                     deadline: float = DOWNLOAD_DEADLINE_SECONDS) -> int:
    """Stream `url` to `dest_path` in 1 MiB chunks; returns the byte count.

    Two independent time bounds: `timeout` is the per-read stall guard
    (a single read that outlives it fails the download), `deadline` the
    absolute wall-clock budget for the whole download — a slow-drip
    server that never stalls long enough to trip the stall guard still
    runs out of deadline.

    progress_cb(fraction) fires at most every ~1% with a value in [0, 1]
    (or not at all when the server sends no Content-Length). Raises
    NetworkError on transport failure, a short body, a body over
    `max_bytes`, or a spent deadline; the partial `dest_path` is always
    removed on failure."""
    done = 0
    declared = None
    start = time.monotonic()
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
                    remaining = _remaining_or_raise(url, start, deadline,
                                                    'download')
                    _arm_socket(response, min(timeout, remaining))
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

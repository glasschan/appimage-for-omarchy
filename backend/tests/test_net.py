# test_net.py — hardened transport tests (SSRF guard, byte caps,
# redirect validation, absolute deadlines). Hermetic: no external network.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# The production guard rejects loopback http.server fixtures (private
# address, plain http), so tests open net.py's single test-only seam
# (net._ALLOW_LOCAL_FOR_TESTS, patched with mock.patch.object — there is
# deliberately no environment variable; round 2 removed it) for requests
# that must reach them; guard behaviour itself is tested with the seam
# OFF.

import email.message
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from helpers import BACKEND_DIR, FakeXDGTestCase

from omarchy_appimage import net


def _addr_info(ip: str, port: int = 0) -> list:
    """A getaddrinfo-shaped single answer for `ip` (mock seam)."""
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, '', (ip, port))]


class ValidateRemoteHostTests(unittest.TestCase):
    """The eager SSRF check: every resolved address must be public
    (seam OFF — the real production behaviour)."""

    def test_rejects_loopback(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               return_value=_addr_info('127.0.0.1')):
            with self.assertRaises(net.NetworkError):
                net.validate_remote_host('evil.example')

    def test_rejects_private(self):
        for ip in ('10.0.0.5', '192.168.1.1', '172.16.0.9', '100.64.0.1'):
            with mock.patch.object(socket, 'getaddrinfo',
                                   return_value=_addr_info(ip)):
                with self.assertRaises(net.NetworkError):
                    net.validate_remote_host('evil.example')

    def test_rejects_link_local(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               return_value=_addr_info('169.254.1.1')):
            with self.assertRaises(net.NetworkError):
                net.validate_remote_host('evil.example')

    def test_rejects_multicast_and_unspecified(self):
        for ip in ('224.0.0.1', '0.0.0.0'):
            with mock.patch.object(socket, 'getaddrinfo',
                                   return_value=_addr_info(ip)):
                with self.assertRaises(net.NetworkError):
                    net.validate_remote_host('evil.example')

    def test_accepts_public_name(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               return_value=_addr_info('93.184.216.34')):
            # no raise
            net.validate_remote_host('example.com')

    def test_rejects_unresolvable(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               side_effect=socket.gaierror('nope')):
            with self.assertRaises(net.NetworkError):
                net.validate_remote_host('void.example')


class GuardedRequestTests(FakeXDGTestCase):
    """URL-level guard checks (seam OFF, no sockets opened)."""

    def test_http_scheme_rejected(self):
        with self.assertRaises(net.NetworkError):
            net.fetch_text('http://example.com/f.appimage')

    def test_file_scheme_rejected(self):
        with self.assertRaises(net.NetworkError):
            net.fetch_text('file:///etc/passwd')

    def test_userinfo_rejected(self):
        with self.assertRaises(net.NetworkError):
            net.fetch_text('https://user:pass@example.com/f.appimage')


class _RawHandler(BaseHTTPRequestHandler):
    """Serves one fixed body with no Content-Length (close-delimited,
    like a chunked/streamed response) — byte caps must still apply."""

    body = b''

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, format, *args):  # silence the test output
        pass


class _ShortBodyHandler(_RawHandler):
    """Declares a Content-Length much larger than the bytes it sends."""

    declared = '999999'

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', type(self).declared)
        self.end_headers()
        self.wfile.write(type(self).body)


class LocalServerTests(FakeXDGTestCase):
    """fetch_text / download_to_file against a loopback fixture server
    with the seam patched ON (the only way tests can reach it)."""

    def setUp(self):
        super().setUp()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), _RawHandler)
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.addCleanup(self._close_server)

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

    def _serve(self, handler_cls) -> str:
        self.server.RequestHandlerClass = handler_cls
        host, port = self.server.server_address[:2]
        return f'http://{host}:{port}/f.appimage'

    def test_fetch_text_happy_path(self):
        url = self._serve(type('H', (_RawHandler,), {'body': b'hello'}))
        self.assertEqual(net.fetch_text(url), 'hello')

    def test_fetch_text_chunked_over_cap_raises(self):
        # no Content-Length at all: the running cap must abort the read
        url = self._serve(type('H', (_RawHandler,), {'body': b'x' * 8192}))
        with self.assertRaises(net.NetworkError):
            net.fetch_text(url, max_bytes=1024)

    def test_fetch_text_declared_over_cap_raises(self):
        url = self._serve(type('H', (_ShortBodyHandler,),
                               {'body': b'x' * 4096,
                                'declared': '4096'}))
        with self.assertRaises(net.NetworkError):
            net.fetch_text(url, max_bytes=1024)

    def test_download_content_length_over_cap_no_file_created(self):
        url = self._serve(type('H', (_ShortBodyHandler,),
                               {'body': b'y' * 4096,
                                'declared': '4096'}))

        dest = os.path.join(self.sandbox, 'never-created.appimage')
        with self.assertRaises(net.NetworkError):
            net.download_to_file(url, dest, max_bytes=1024)
        # rejected before the local file was created or appended to
        self.assertFalse(os.path.exists(dest))

    def test_download_running_cap_aborts_and_removes_partial(self):
        url = self._serve(type('H', (_RawHandler,), {'body': b'y' * 8192}))

        dest = os.path.join(self.sandbox, 'partial.appimage')
        with self.assertRaises(net.NetworkError):
            net.download_to_file(url, dest, max_bytes=1024)
        # the partial download was cleaned up
        self.assertFalse(os.path.exists(dest))

    def test_download_short_body_raises_and_removes_partial(self):
        # the server declares 999999 bytes but sends 100: the short-body
        # error is kept and the partial file is removed
        url = self._serve(type('H', (_ShortBodyHandler,),
                               {'body': b'z' * 100,
                                'declared': '999999'}))

        dest = os.path.join(self.sandbox, 'short.appimage')
        with self.assertRaises(net.NetworkError):
            net.download_to_file(url, dest)
        self.assertFalse(os.path.exists(dest))

    def test_download_happy_path(self):
        body = b'\x7fELF\x02\x01\x01\x00\x41\x49\x02' + b'data' * 100
        url = self._serve(type('H', (_RawHandler,), {'body': body}))

        dest = os.path.join(self.sandbox, 'ok.appimage')
        done = net.download_to_file(url, dest)
        self.assertEqual(done, len(body))
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), body)


class EnvKillSwitchTests(unittest.TestCase):
    """Round 2: the old OMARCHY_APPIMAGE_ALLOW_LOCAL_HTTP environment
    kill switch is gone — an inherited environment must not be able to
    open the transport guards (tests patch net._ALLOW_LOCAL_FOR_TESTS
    with mock.patch.object instead). Proven in a fresh interpreter with
    the variable set."""

    def test_env_var_is_dead_in_a_fresh_process(self):
        child = (
            'import sys\n'
            f'sys.path.insert(0, {BACKEND_DIR!r})\n'
            'from omarchy_appimage import net\n'
            'assert net._ALLOW_LOCAL_FOR_TESTS is False, \\\n'
            '    "seam opened: %r" % net._ALLOW_LOCAL_FOR_TESTS\n'
            'try:\n'
            '    net.validate_remote_host("127.0.0.1")\n'
            'except net.NetworkError:\n'
            '    print("GUARDS-UP")\n'
            'else:\n'
            '    raise SystemExit("loopback accepted despite the guard")\n'
        )
        env = dict(os.environ)
        env['OMARCHY_APPIMAGE_ALLOW_LOCAL_HTTP'] = '1'
        result = subprocess.run(
            [sys.executable, '-B', '-c', child], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('GUARDS-UP', result.stdout)


class _TrickleHandler(BaseHTTPRequestHandler):
    """Writes `chunk_size` bytes every `interval` seconds (no
    Content-Length: close-delimited), capped at `max_chunks` — a steady
    slow drip whose individual reads all finish quickly but whose total
    runs into the absolute whole-operation deadline."""
    chunk_size = 65536
    interval = 0.05
    max_chunks = 200

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        try:
            for _ in range(type(self).max_chunks):
                self.wfile.write(b'x' * type(self).chunk_size)
                self.wfile.flush()
                time.sleep(type(self).interval)
        except OSError:
            pass  # the client gave up (deadline); stop dripping

    def log_message(self, format, *args):  # silence the test output
        pass


class _StallHandler(BaseHTTPRequestHandler):
    """Sends one byte, then stalls longer than the client's timeout —
    the per-read stall guard must abort."""
    stall = 2.5

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        try:
            self.wfile.write(b'x')
            self.wfile.flush()
            time.sleep(type(self).stall)
            self.wfile.write(b'y')
        except OSError:
            pass

    def log_message(self, format, *args):  # silence the test output
        pass


class DeadlineTests(FakeXDGTestCase):
    """Round-2 hardening: timeouts are whole-operation absolute
    deadlines, not only per-socket-operation stall guards (seam ON for
    the loopback fixtures; every test aborts in well under 5s)."""

    def setUp(self):
        super().setUp()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)
        self._servers = []
        self._threads = []
        self.addCleanup(self._close_servers)

    def _close_servers(self):
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)

    def _serve(self, handler_cls) -> str:
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
        self._servers.append(server)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._threads.append(thread)
        host, port = server.server_address[:2]
        return f'http://{host}:{port}/f.appimage'

    def test_spent_budget_raises(self):
        # the absolute-deadline primitive itself: a budget that started
        # in the past raises (deterministic half of the trickle tests,
        # whose final read can race the armed socket timeout)
        with self.assertRaises(net.NetworkError) as caught:
            net._remaining_or_raise('https://x/f', time.monotonic() - 10,
                                    5, 'request')
        self.assertIn('deadline exceeded', str(caught.exception))
        self.assertGreater(
            net._remaining_or_raise('https://x/f', time.monotonic(), 5,
                                    'request'), 0)

    def test_fetch_text_slow_drip_hits_deadline(self):
        # max_bytes is deliberately far above anything the trickle can
        # deliver within the deadline: this isolates the absolute-deadline
        # abort from the byte-cap abort
        url = self._serve(_TrickleHandler)
        start = time.monotonic()
        with self.assertRaises(net.NetworkError):
            net.fetch_text(url, timeout=1,
                           max_bytes=64 * 1024 * 1024)
        self.assertLess(time.monotonic() - start, 5)

    def test_fetch_text_stall_aborts(self):
        url = self._serve(_StallHandler)
        start = time.monotonic()
        with self.assertRaises(net.NetworkError):
            net.fetch_text(url, timeout=1)
        self.assertLess(time.monotonic() - start, 5)

    def test_download_deadline_exceeded_no_partial_file(self):
        # download reads come in 1 MiB chunks: pace the trickle to the
        # read size so each read completes quickly while the total runs
        # into the absolute download deadline
        url = self._serve(type('T', (_TrickleHandler,),
                               {'chunk_size': 1 << 20, 'interval': 0.05,
                                'max_chunks': 100}))
        dest = os.path.join(self.sandbox, 'slow.appimage')
        start = time.monotonic()
        with self.assertRaises(net.NetworkError):
            net.download_to_file(url, dest, timeout=30, deadline=1)
        self.assertLess(time.monotonic() - start, 5)
        # the partial artifact was cleaned up
        self.assertFalse(os.path.exists(dest))

    def test_download_stall_aborts_no_partial_file(self):
        url = self._serve(_StallHandler)
        dest = os.path.join(self.sandbox, 'stalled.appimage')
        start = time.monotonic()
        with self.assertRaises(net.NetworkError):
            net.download_to_file(url, dest, timeout=1)
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(os.path.exists(dest))


class RedirectValidationTests(unittest.TestCase):
    """Each redirect hop is re-validated (seam OFF: the production
    behaviour — checked directly on redirect_request, no sockets)."""

    def _redirect(self, location: str, hops: int = 0):
        req = urllib.request.Request(
            'https://releases.example/app.appimage')
        req.redirect_hops = hops
        headers = email.message.Message()
        headers['Location'] = location
        handler = net._ValidatingRedirectHandler()
        return handler.redirect_request(req, fp=None, code=302,
                                        msg='Found', headers=headers,
                                        newurl=location)

    def test_redirect_to_private_ip_rejected(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               return_value=_addr_info('192.168.1.1')):
            with self.assertRaises(net.NetworkError):
                self._redirect('https://192.168.1.1/app.appimage')

    def test_redirect_to_loopback_rejected(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               return_value=_addr_info('127.0.0.1')):
            with self.assertRaises(net.NetworkError):
                self._redirect('https://127.0.0.1:8080/app.appimage')

    def test_redirect_downgrade_to_http_rejected(self):
        with self.assertRaises(net.NetworkError):
            self._redirect('http://releases.example/app.appimage')

    def test_redirect_with_userinfo_rejected(self):
        with self.assertRaises(net.NetworkError):
            self._redirect('https://evil@releases.example/app.appimage')

    def test_too_many_hops_rejected(self):
        with mock.patch.object(socket, 'getaddrinfo',
                               return_value=_addr_info('93.184.216.34')):
            with self.assertRaises(net.NetworkError):
                self._redirect('https://releases.example/5',
                               hops=net._MAX_REDIRECTS)


if __name__ == '__main__':
    unittest.main()

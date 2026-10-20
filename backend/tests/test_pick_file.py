# test_pick_file.py — orchestration tests for the --pick-file verb (#16).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin.
#
# Everything is FAKED: no real gdbus/zenity process is ever spawned. The
# one-shot commands go through a fake `pick_file.run_command` (same seam
# as the notify-send tests in test_cli.py); the long-running
# `gdbus monitor` goes through a fake process whose stdout is a REAL
# os.pipe, so the monitor session's select/deadline machinery runs for
# real against canned gdbus output.

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import FakeXDGTestCase

from omarchy_appimage import cli, pick_file


PORTAL_HANDLE = ('/org/freedesktop/portal/desktop/request/'
                 '1_42/omarchypick')

PORTAL_INTROSPECT = '''
org.freedesktop.portal.FileChooser:
interface org.freedesktop.portal.FileChooser {
  methods:
    OpenFile(in  s parent_window, s title, a{sv} options, o handle);
};
'''

CALL_REPLY_STDOUT = ("(objectpath '%s',)\n" % PORTAL_HANDLE).encode()

PORTAL_CALL_ARGV = [
    'gdbus', 'call', '--session', '--dest',
    'org.freedesktop.portal.Desktop',
    '--object-path', '/org/freedesktop/portal/desktop',
    '--method', 'org.freedesktop.portal.FileChooser.OpenFile',
    '', 'Open AppImage',
    "{'handle_token': <'omarchypick'>, 'multiple': <false>, "
    "'filters': <[('AppImage', [(0, '*.AppImage'), (0, '*.appimage')])], "
    "'current_filter': <('AppImage', [(0, '*.AppImage'), "
    "(0, '*.appimage')])>}"]

MONITOR_ARGV = ['gdbus', 'monitor', '--session', '--dest',
                'org.freedesktop.portal.Desktop']


def response_line(code: int, uri: str = None) -> bytes:
    """A canned `gdbus monitor` Response signal line (GVariant text)."""
    if uri is None:
        results = '{}'
    else:
        results = "{'uris': <['%s']>}" % uri
    line = ('%s: org.freedesktop.portal.Request.Response '
            '(uint32 %d, %s)\n' % (PORTAL_HANDLE, code, results))
    return line.encode()


class FakeOneShot:
    """Fake pick_file.run_command: dispatches on argv[0], records calls.

    Each slot is a dict of CompletedProcess fields or an exception
    instance to raise; a None slot means "this command must not run"."""

    def __init__(self):
        self.introspect = None
        self.call = None
        self.zenity = None
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        argv = list(command)
        if argv[:2] == ['gdbus', 'introspect']:
            return self._resolve(self.introspect, argv)
        if argv[:2] == ['gdbus', 'call']:
            return self._resolve(self.call, argv)
        if argv[:1] == ['zenity']:
            return self._resolve(self.zenity, argv)
        raise AssertionError('unexpected command: %r' % (argv,))

    def _resolve(self, slot, argv):
        if isinstance(slot, Exception):
            raise slot
        assert slot is not None, 'unexpected command ran: %r' % (argv,)
        return subprocess.CompletedProcess(
            argv, slot.get('returncode', 0),
            stdout=slot.get('stdout', b''), stderr=slot.get('stderr', b''))

    def of_kind(self, kind: str) -> list:
        prefix = {'introspect': ['gdbus', 'introspect'],
                  'call': ['gdbus', 'call'],
                  'zenity': ['zenity']}[kind]
        return [c for c in self.calls if c[:len(prefix)] == prefix]


class FakeMonitorProc:
    """Popen stand-in for `gdbus monitor`: stdout is a real pipe so the
    _MonitorSession select/deadline code runs against canned bytes. pid is
    -1 so close()'s killpg path falls back to kill() (never signals a real
    process group). Every pipe fd is closed exactly once."""

    def __init__(self, data: bytes = b'', hang: bool = False):
        r, w = os.pipe()
        sr, sw = os.pipe()
        os.close(sw)
        self.stdout = os.fdopen(r, 'rb')
        self.stderr = os.fdopen(sr, 'rb')
        self._w = w
        if data:
            os.write(w, data)
        if not hang:
            os.close(w)
            self._w = None
        self.pid = -1
        self.killed = False

    def kill(self):
        self.killed = True
        if self._w is not None:
            os.close(self._w)
            self._w = None

    def wait(self, timeout=None):
        return 0


class PickTestCase(FakeXDGTestCase):
    """Base: every subprocess seam of pick_file is faked."""

    def use(self, one_shot: FakeOneShot, monitor: FakeMonitorProc = None,
            available: tuple = ('gdbus', 'zenity')):
        """Patch the module seams; returns (one_shot, spawned_monitor_args)."""
        spawned = []

        def fake_spawn(argv):
            assert monitor is not None, 'monitor spawned unexpectedly'
            spawned.append(list(argv))
            return pick_file._MonitorSession(monitor)

        def fake_which(name):
            return '/usr/bin/' + name if name in available else None

        for patch in (
                mock.patch.object(pick_file, 'run_command', one_shot),
                mock.patch.object(pick_file, '_which', fake_which),
                mock.patch.object(pick_file, '_spawn_monitor', fake_spawn)):
            patch.start()
            self.addCleanup(patch.stop)
        return one_shot, spawned

    def portal_ready(self, oneshot: FakeOneShot):
        oneshot.introspect = {'returncode': 0,
                              'stdout': PORTAL_INTROSPECT.encode()}
        oneshot.call = {'returncode': 0, 'stdout': CALL_REPLY_STDOUT}


# ---------------------------------------------------------- orchestration

class PortalOrchestrationTests(PickTestCase):

    def test_portal_pick_success_returns_validated_local_path(self):
        oneshot, spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(0, 'file:///home/u/AppImages/nvim.AppImage')))
        self.portal_ready(oneshot)

        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'picked')
        self.assertEqual(payload['backend'], 'portal')
        self.assertEqual(payload['path'], '/home/u/AppImages/nvim.AppImage')
        self.assertEqual(payload['schema_version'], 1)
        # monitor-then-call: exactly one monitor, started BEFORE the call;
        # the introspect probe is the availability check (no side effects)
        self.assertEqual(spawned, [MONITOR_ARGV])
        self.assertEqual(oneshot.of_kind('introspect'), [
            ['gdbus', 'introspect', '--session', '--dest',
             'org.freedesktop.portal.Desktop',
             '--object-path', '/org/freedesktop/portal/desktop']])
        self.assertEqual(oneshot.of_kind('call'), [PORTAL_CALL_ARGV])

    def test_portal_call_argv_is_constant_and_filtered(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(0, 'file:///home/u/x.AppImage')))
        self.portal_ready(oneshot)
        pick_file.run_pick()

        argv = oneshot.of_kind('call')[0]
        # constant argv, zero interpolation (the title is not user data);
        # single file, AppImage filter
        self.assertEqual(argv, PORTAL_CALL_ARGV)
        self.assertIn("'multiple': <false>", argv[-1])
        self.assertIn("'*.AppImage'", argv[-1])
        self.assertIn("'*.appimage'", argv[-1])

    def test_portal_error_falls_back_to_zenity(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(b''))
        self.portal_ready(oneshot)
        # the portal call itself fails -> zenity answers
        oneshot.call = {'returncode': 1,
                        'stdout': b'', 'stderr': b'no portal\n'}
        oneshot.zenity = {'returncode': 0,
                          'stdout': b'/home/u/x.AppImage\n'}

        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'picked')
        self.assertEqual(payload['backend'], 'zenity')
        self.assertEqual(payload['path'], '/home/u/x.AppImage')
        self.assertEqual(len(oneshot.of_kind('zenity')), 1)

    def test_portal_cancel_does_not_reopen_a_dialog(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(1)))
        self.portal_ready(oneshot)
        oneshot.zenity = {'returncode': 0, 'stdout': b'/home/u/y.AppImage\n'}

        payload = pick_file.run_pick()
        self.assertEqual(payload, {
            'schema_version': 1, 'result': 'cancelled',
            'backend': 'portal'})
        # an explicit user cancel must not fall through to a second dialog
        self.assertEqual(oneshot.of_kind('zenity'), [])

    def test_percent_encoded_uri_is_decoded_to_a_plain_path(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(0, 'file:///home/u/My%20Apps/nvim%21.AppImage')))
        self.portal_ready(oneshot)
        payload = pick_file.run_pick()
        self.assertEqual(payload['path'], '/home/u/My Apps/nvim!.AppImage')

    def test_monitor_ended_without_response_is_typed_backend_failed(self):
        # simulated dialog crash: the monitor stream ends before any
        # Response signal arrives — a typed error, never a hang
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(b''),
                                     available=('gdbus',))
        self.portal_ready(oneshot)
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'backend-failed')

    def test_dialog_deadline_timeout_is_typed_error(self):
        # the monitor stays silent forever (hang=True): the whole-operation
        # deadline must fire and yield a typed timeout error
        monitor = FakeMonitorProc(b'', hang=True)
        oneshot, _spawned = self.use(FakeOneShot(), monitor)
        self.portal_ready(oneshot)

        payload = pick_file.run_pick(deadline_seconds=0.3)
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'timeout')
        # the monitor process was reaped, not left behind
        self.assertTrue(monitor.killed)

    def test_deadline_exhausted_before_start_typed_timeout(self):
        oneshot, spawned = self.use(FakeOneShot())
        self.portal_ready(oneshot)
        payload = pick_file.run_pick(deadline_seconds=0)
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'timeout')
        # an exhausted budget spawns nothing at all
        self.assertEqual(spawned, [])
        self.assertEqual(oneshot.calls, [])

    def test_non_file_uri_rejected(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(0, 'https://example.com/evil.AppImage')),
            available=('gdbus',))
        self.portal_ready(oneshot)
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'not-local')
        self.assertIn('https://example.com/evil.AppImage', payload['error'])

    def test_file_uri_with_remote_host_rejected(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(0, 'file://evilhost/share/x.AppImage')),
            available=('gdbus',))
        self.portal_ready(oneshot)
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'not-local')

    def test_missing_filechooser_interface_not_available(self):
        oneshot, _spawned = self.use(FakeOneShot())
        oneshot.introspect = {'returncode': 0,
                              'stdout': b'interface org.freedesktop.portal.Mail {}'}
        oneshot.zenity = {'returncode': 0, 'stdout': b'/home/u/x.AppImage\n'}
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'picked')
        self.assertEqual(payload['backend'], 'zenity')
        # the dialog call never ran: the probe already said no
        self.assertEqual(oneshot.of_kind('call'), [])


class ZenityOrchestrationTests(PickTestCase):

    def test_zenity_success_when_portal_unavailable(self):
        oneshot, _spawned = self.use(FakeOneShot(), available=('zenity',))
        oneshot.zenity = {'returncode': 0,
                          'stdout': b'/home/u/AppImages/z.AppImage\n'}
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'picked')
        self.assertEqual(payload['backend'], 'zenity')
        self.assertEqual(payload['path'], '/home/u/AppImages/z.AppImage')
        # no portal probe, no monitor
        self.assertEqual(oneshot.of_kind('introspect'), [])

    def test_zenity_argv_is_single_file_and_appimage_filtered(self):
        oneshot, _spawned = self.use(FakeOneShot(), available=('zenity',))
        oneshot.zenity = {'returncode': 0, 'stdout': b'/home/u/z.AppImage\n'}
        pick_file.run_pick()

        calls = oneshot.of_kind('zenity')
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[0], 'zenity')
        self.assertIn('--file-selection', argv)
        # single file: the --multiple flag is never passed
        self.assertNotIn('--multiple', argv)
        self.assertIn('--file-filter', argv)
        self.assertIn('*.AppImage', argv[argv.index('--file-filter') + 1])
        self.assertIn('*.appimage', argv[argv.index('--file-filter') + 1])

    def test_zenity_error_typed_backend_failed(self):
        oneshot, _spawned = self.use(FakeOneShot(), available=('zenity',))
        oneshot.zenity = {'returncode': 3, 'stdout': b'',
                          'stderr': b'gtk init failed\n'}
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'backend-failed')
        self.assertIn('3', payload['error'])

    def test_zenity_cancelled(self):
        oneshot, _spawned = self.use(FakeOneShot(), available=('zenity',))
        oneshot.zenity = {'returncode': 1, 'stdout': b''}
        payload = pick_file.run_pick()
        self.assertEqual(payload, {'schema_version': 1,
                                   'result': 'cancelled',
                                   'backend': 'zenity'})

    def test_dialog_crash_process_gone_is_typed_error(self):
        # simulated crash: the dialog binaries vanish mid-flight
        oneshot, _spawned = self.use(FakeOneShot(), available=('zenity',))
        oneshot.zenity = FileNotFoundError('zenity')
        payload = pick_file.run_pick()
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['reason'], 'backend-failed')

    def test_neither_backend_typed_unavailable(self):
        oneshot, spawned = self.use(FakeOneShot(), available=())
        payload = pick_file.run_pick()
        self.assertEqual(payload, {
            'schema_version': 1, 'result': 'unavailable',
            'message': pick_file.UNAVAILABLE_MESSAGE})
        # nothing was spawned at all
        self.assertEqual(spawned, [])
        self.assertEqual(oneshot.calls, [])


# ------------------------------------------------------------- CLI wiring

class InProcessCliMixin:
    def run_cmd(self, func, args: list):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = func(args)
        return out.getvalue(), err.getvalue(), code


class PickCliTests(InProcessCliMixin, PickTestCase):

    def _picked_portal(self):
        oneshot = FakeOneShot()
        self.portal_ready(oneshot)
        monitor = FakeMonitorProc(
            response_line(0, 'file:///home/u/AppImages/nvim.AppImage'))
        return self.use(oneshot, monitor)

    def test_cli_json_document_matches_contract(self):
        self._picked_portal()
        out, err, code = self.run_cmd(cli.cmd_pick_file, ['--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(err, '')
        # stdout is exactly one JSON document with a single newline
        self.assertEqual(out.count('\n'), 1)
        payload = json.loads(out)
        self.assertEqual(payload, {
            'schema_version': 1, 'result': 'picked',
            'path': '/home/u/AppImages/nvim.AppImage',
            'backend': 'portal'})

    def test_cli_plain_output_prints_path(self):
        self._picked_portal()
        out, err, code = self.run_cmd(cli.cmd_pick_file, [])
        self.assertEqual(code, 0, err)
        self.assertEqual(out, '/home/u/AppImages/nvim.AppImage\n')

    def test_cli_json_typed_error_exit_1(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(b''),
                                     available=('gdbus',))
        self.portal_ready(oneshot)
        out, err, code = self.run_cmd(cli.cmd_pick_file, ['--json'])
        self.assertEqual(code, 1)
        self.assertIn('Error', err)
        payload = json.loads(out)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('reason', payload)

    def test_cli_unavailable_exits_0_both_modes(self):
        self.use(FakeOneShot(), available=())
        out, err, code = self.run_cmd(cli.cmd_pick_file, ['--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['result'], 'unavailable')

        self.use(FakeOneShot(), available=())
        out, err, code = self.run_cmd(cli.cmd_pick_file, [])
        self.assertEqual(code, 0)
        # plain mode prints the user-facing message (keep the path field)
        self.assertEqual(out, pick_file.UNAVAILABLE_MESSAGE + '\n')

    def test_cli_cancelled_exits_0(self):
        oneshot, _spawned = self.use(FakeOneShot(), FakeMonitorProc(
            response_line(1)))
        self.portal_ready(oneshot)
        out, err, code = self.run_cmd(cli.cmd_pick_file, ['--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['result'], 'cancelled')


class PickUsageTests(FakeXDGTestCase):
    """The verb is wired into the real CLI (safe: --help never dialogs)."""

    def test_pick_file_help_via_subprocess(self):
        result = self.run_cli('--pick-file', '--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('Usage:', result.stdout)
        self.assertIn('--pick-file', result.stdout)

    def test_usage_lists_pick_file(self):
        result = self.run_cli('--help')
        self.assertEqual(result.returncode, 0)
        self.assertIn('--pick-file', result.stdout)


if __name__ == '__main__':
    unittest.main()

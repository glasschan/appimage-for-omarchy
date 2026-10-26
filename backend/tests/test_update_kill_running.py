# test_update_kill_running.py — ticket #26: --kill-running, its TTY consent
# question and the pre-replace re-check (cli.run_update + the job document).
#
# Test suite written for this plugin. Every process in here is a fake: the
# CLI-level tests swap procs._PROBE for test_procs.FakeProbe, so not one
# real pid is ever signalled.
#
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.

import io
import json
import os
import signal
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from helpers import FakeXDGTestCase, FixtureHTTPServer

from omarchy_appimage import cli, jobs, procs, utils
from omarchy_appimage.ini_config import Config
from test_procs import FakeProbe

MOUNT = '/tmp/.mount_fakeXq8zW'

# The fakes' pids. Two processes are the normal case: the type-2 runtime
# parent (exe == the AppImage path) and one helper out of the FUSE mount.
PARENT_PID = 700
CHILD_PID = 701
LATE_PID = 702

# What quit_app sends first and what it escalates to.
_TERM = signal.SIGTERM
_KILL = signal.SIGKILL


class _RunningAppTestCase(FakeXDGTestCase):
    """An installed fake app plus a fake process view of it running."""

    def setUp(self):
        super().setUp()
        self.appimage, _desktop, _icon = self.install_fake_app()

    def _probe(self, parents=True, children=True, **kwargs) -> FakeProbe:
        processes, mounts = [], []
        if parents:
            processes.append((PARENT_PID, self.appimage))
        if children:
            processes.append((CHILD_PID, f'{MOUNT}/usr/bin/fakeapp'))
        mounts.append(('fakeapp.appimage', MOUNT, 'fuse.fakeapp.appimage'))
        return FakeProbe(processes=processes, mounts=mounts, **kwargs)

    @staticmethod
    def _acted_on(probe) -> list:
        """Pids the probe was asked to TERM or KILL (signal 0 is the
        liveness poll, not an action)."""
        return [pid for pid, sig in probe.sent if sig in (_TERM, _KILL)]

    @staticmethod
    def _signals_sent(probe) -> list:
        """(pid, signal) pairs for the real actions only."""
        return [(pid, sig) for pid, sig in probe.sent
                if sig in (_TERM, _KILL)]


class RunUpdateKillTests(_RunningAppTestCase):
    """The two guards inside run_update: at job start and immediately
    before the atomic replace. update_from_url is stubbed, so nothing
    here downloads, verifies or installs."""

    def setUp(self):
        super().setUp()
        self.manager = mock.Mock(download_size=10)
        self.manager.is_update_available.return_value = True
        self.probe = self._probe()
        self.installs = []

    def _install(self, manager, el, keep_both=False, progress_cb=None,
                 before_replace=None):
        # snapshot the signals that already landed: this is the download
        # step, so anything in here came from the job-start guard
        self.installs.append({'before_replace': before_replace,
                              'sent_before_download':
                                  self._signals_sent(self.probe)})
        return mock.Mock(version='v2.0', size=11)

    def _run(self, running=True, **kwargs) -> cli.UpdateResult:
        with mock.patch.object(cli.UpdateManagerChecker,
                               'check_url_for_app',
                               return_value=self.manager), \
                mock.patch.object(cli.appimage_provider, 'is_app_running',
                                  return_value=running), \
                mock.patch.object(cli.appimage_provider, 'update_from_url',
                                  self._install):
            return cli.run_update('fakeapp', probe=self.probe, **kwargs)

    def test_nothing_signalled_without_kill_running_grant(self):
        result = self._run()
        self.assertEqual(result.status, 'skipped-running')
        self.assertEqual(self.probe.sent, [])
        self.assertEqual(self.installs, [])
        self.assertIsNone(result.force_quit)

    def test_skipped_message_names_both_escape_hatches(self):
        message = self._run().message
        self.assertIn('--kill-running', message)
        self.assertIn('--force', message)

    def test_consent_closes_the_app_before_the_download(self):
        result = self._run(confirm_close=lambda el: True)
        self.assertEqual(result.status, 'updated')
        # both processes, TERM only, and already done when the download
        # started (no download for an app that cannot be replaced)
        self.assertEqual(self._acted_on(self.probe),
                         [PARENT_PID, CHILD_PID])
        self.assertEqual(self.installs[0]['sent_before_download'],
                         [(PARENT_PID, _TERM), (CHILD_PID, _TERM)])
        self.assertEqual(result.force_quit, {'count': 2, 'sigkill': False})

    def test_consent_declined_skips_and_signals_nothing(self):
        result = self._run(confirm_close=lambda el: False)
        self.assertEqual(result.status, 'skipped-running')
        self.assertEqual(self.probe.sent, [])
        self.assertIsNone(result.force_quit)

    def test_kill_running_never_asks_and_always_quits(self):
        asked = mock.Mock(return_value=False)
        result = self._run(kill_running=True, confirm_close=asked)
        self.assertEqual(result.status, 'updated')
        self.assertEqual(asked.call_count, 0)
        self.assertEqual(self._acted_on(self.probe),
                         [PARENT_PID, CHILD_PID])
        self.assertEqual(result.force_quit, {'count': 2, 'sigkill': False})

    def test_kill_running_rechecks_before_the_replace(self):
        # a process that appeared during the download is still closed,
        # because the grant covers the whole job
        def install(manager, el, keep_both=False, progress_cb=None,
                    before_replace=None):
            self.installs.append({'before_replace': before_replace,
                                  'sent_before_download':
                                      self._signals_sent(self.probe)})
            self.probe._processes.append((LATE_PID, f'{MOUNT}/usr/bin/new'))
            before_replace()
            return mock.Mock(version='v2.0', size=11)

        self._install = install
        result = self._run(kill_running=True)
        self.assertEqual(result.status, 'updated')
        self.assertIn(LATE_PID, self._acted_on(self.probe))
        # the start guard closed 2, the pre-replace check closed the 1
        # that came back; the count is the whole job's
        self.assertEqual(result.force_quit, {'count': 3, 'sigkill': False})

    def test_sigkill_escalation_is_recorded(self):
        self.probe = self._probe(
            reactions={PARENT_PID: 'survive-term', CHILD_PID: 'survive-term'})
        result = self._run(kill_running=True)
        self.assertEqual(result.force_quit, {'count': 2, 'sigkill': True})

    def test_force_quit_absent_when_nothing_was_closed(self):
        # granted, but the app is not actually there: the grant still
        # arms the pre-replace check, and the document stays additive
        self.probe = self._probe(parents=False, children=False)
        result = self._run(kill_running=True)
        self.assertEqual(result.status, 'updated')
        self.assertIsNone(result.force_quit)
        self.assertIsNotNone(self.installs[0]['before_replace'])

    def test_force_alone_keeps_its_meaning(self):
        # --force: proceed silently, never signal, no seam handed down
        result = self._run(force=True)
        self.assertEqual(result.status, 'updated')
        self.assertEqual(self.probe.sent, [])
        self.assertIsNone(self.installs[0]['before_replace'])
        self.assertIsNone(result.force_quit)

    def test_no_grant_means_no_seam_for_a_running_app(self):
        self._run()          # skipped: update_from_url never ran
        self.assertEqual(self.installs, [])

    def test_not_running_without_grant_installs_without_seam(self):
        result = self._run(running=False)
        self.assertEqual(result.status, 'updated')
        self.assertIsNone(self.installs[0]['before_replace'])
        self.assertEqual(self.probe.sent, [])

    # -------------------------------------------------- finding A / G2

    def test_up_to_date_never_touches_a_process(self):
        # no update, no quit: the availability decision comes first, so
        # --kill-running must not close an app for a no-op update
        self.manager.is_update_available.return_value = False
        result = self._run(kill_running=True)
        self.assertEqual(result.status, 'up-to-date')
        self.assertEqual(self.probe.sent, [])
        self.assertEqual(self.installs, [])
        self.assertIsNone(result.force_quit)

    def test_up_to_date_does_not_ask_either(self):
        self.manager.is_update_available.return_value = False
        asked = mock.Mock(return_value=True)
        result = self._run(confirm_close=asked)
        self.assertEqual(result.status, 'up-to-date')
        self.assertEqual(asked.call_count, 0)
        self.assertEqual(self.probe.sent, [])

    def test_an_unreachable_source_still_answers_skipped_running(self):
        # the refusal needs no knowledge of the update, so a failing
        # availability check must not turn it into a network error
        self.manager.is_update_available.side_effect = \
            cli.OperationError('could not reach the release feed')
        result = self._run()
        self.assertEqual(result.status, 'skipped-running')
        self.assertEqual(self.probe.sent, [])
        self.assertEqual(self.installs, [])

    def test_an_unreachable_source_still_raises_when_it_matters(self):
        # with a grant (or a question to ask) the check is load-bearing:
        # the error must not be swallowed into a silent skip
        self.manager.is_update_available.side_effect = \
            cli.OperationError('could not reach the release feed')
        with self.assertRaises(cli.OperationError):
            self._run(kill_running=True)
        self.assertEqual(self.probe.sent, [])
        self.assertEqual(self.installs, [])

    # -------------------------------------------------- finding C / G5

    def test_keep_both_with_kill_running_never_signals(self):
        # keep-both installs beside the running file, so os.replace never
        # touches the running app's path and closing it is gratuitous
        result = self._run(kill_running=True, keep_both=True)
        self.assertEqual(result.status, 'updated')
        self.assertEqual(self.probe.sent, [])
        self.assertIsNone(self.installs[0]['before_replace'])
        self.assertIsNone(result.force_quit)

    # -------------------------------------------------- finding F / 12

    def test_survivor_refuses_the_update_before_the_download(self):
        # a target that survives (signal denied or ignored) must stop the
        # job: the replace would swap the file under a live process while
        # the banner claims it was closed
        self.probe = self._probe(unpermitted={PARENT_PID, CHILD_PID})
        with self.assertRaises(cli.OperationError) as ctx:
            self._run(kill_running=True)
        message = str(ctx.exception)
        self.assertIn(str(PARENT_PID), message)
        self.assertIn(str(CHILD_PID), message)
        self.assertIn('the update start', message)
        self.assertIn('refusing', message)
        self.assertEqual(self.installs, [])      # nothing downloaded

    def test_survivor_refuses_the_replace_at_the_hook(self):
        def install(manager, el, keep_both=False, progress_cb=None,
                    before_replace=None):
            self.installs.append({'before_replace': before_replace})
            # a process that appeared during the download and cannot be
            # signalled: the seam is the last line of defence
            self.probe._processes.append((LATE_PID, f'{MOUNT}/usr/bin/new'))
            self.probe.unpermitted.add(LATE_PID)
            before_replace()
            return mock.Mock(version='v2.0', size=11)

        self._install = install
        with self.assertRaises(cli.OperationError) as ctx:
            self._run(kill_running=True)
        message = str(ctx.exception)
        self.assertIn(str(LATE_PID), message)
        self.assertIn('the atomic replace', message)

    def test_a_closed_app_that_still_dies_is_not_refused(self):
        # the normal case stays untouched: SIGKILL's target is gone, so
        # nothing survives and the update proceeds
        self.probe = self._probe(
            reactions={PARENT_PID: 'survive-term',
                       CHILD_PID: 'survive-term'})
        result = self._run(kill_running=True)
        self.assertEqual(result.status, 'updated')
        self.assertEqual(result.force_quit, {'count': 2, 'sigkill': True})


class AtomicInstallSeamTests(unittest.TestCase):
    """The seam itself: _atomic_install calls before_replace after the
    temp file is complete and before os.replace, and defaults to None so
    every existing caller is unchanged."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix='omarchy-appimage-seam-')
        self.addCleanup(lambda: __import__('shutil').rmtree(
            self.dir, ignore_errors=True))
        self.src = os.path.join(self.dir, 'src')
        Path(self.src).write_bytes(b'NEW')
        self.dest = os.path.join(self.dir, 'dest')
        Path(self.dest).write_bytes(b'OLD')

    def _seam(self, before_replace):
        from omarchy_appimage.provider import _atomic_install
        _atomic_install(self.src, self.dest, 0o755,
                        before_replace=before_replace)
        return Path(self.dest).read_bytes()

    def test_before_replace_runs_before_the_replace(self):
        seen = []

        def before_replace():
            seen.append(Path(self.dest).read_bytes())

        self.assertEqual(self._seam(before_replace), b'NEW')
        # the destination was still the old file when the hook ran
        self.assertEqual(seen, [b'OLD'])

    def test_no_hook_replaces_exactly_as_before(self):
        self.assertEqual(self._seam(None), b'NEW')


class JobDocumentForceQuitTests(FakeXDGTestCase):
    """`force_quit` is additive: the status vocabulary and the phase walk
    are untouched, and the key only exists when something was closed."""

    def _finish(self, desktop_id, force_quit):
        writer = jobs.JobWriter(desktop_id, pid=4242)
        el = mock.Mock(version='v2.0', file_path='/x/fakeapp.appimage')
        result = cli.UpdateResult('updated', el=el, message='updated',
                                  downloaded_bytes=12, force_quit=force_quit)
        code = cli._finish_job_document(writer, result)
        self.assertEqual(code, 0)
        return jobs.read_job(desktop_id)

    def test_force_quit_record_rides_the_updated_document(self):
        doc = self._finish('fakeapp.desktop',
                           {'count': 3, 'sigkill': False})
        self.assertEqual(doc['schema_version'], 1)
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'updated')
        self.assertEqual(doc['force_quit'], {'count': 3, 'sigkill': False})

    def test_force_quit_is_absent_when_nothing_was_closed(self):
        doc = self._finish('otherapp.desktop', None)
        self.assertEqual(doc['status'], 'updated')
        self.assertNotIn('force_quit', doc)

    def test_initial_document_never_carries_force_quit(self):
        jobs.JobWriter('thirdapp.desktop', pid=4242)
        self.assertNotIn('force_quit', jobs.read_job('thirdapp.desktop'))

    def test_up_to_date_document_drops_a_stale_force_quit(self):
        # finding B: an `up-to-date` document applied nothing, so it must
        # never carry a close record — the banner would claim the app was
        # closed to apply an update that did not exist
        writer = jobs.JobWriter('fifthapp.desktop', pid=4242)
        el = mock.Mock(version='v1.0', file_path='/x/fakeapp.appimage')
        result = cli.UpdateResult('up-to-date', el=el, message='up to date',
                                  force_quit={'count': 2, 'sigkill': False})
        self.assertEqual(cli._finish_job_document(writer, result), 0)
        doc = jobs.read_job('fifthapp.desktop')
        self.assertEqual(doc['status'], 'up-to-date')
        self.assertNotIn('force_quit', doc)

    def test_finish_error_threads_the_force_quit_record(self):
        # finding G: the writer takes the record too, so an error document
        # can say the app was closed before the update failed
        writer = jobs.JobWriter('sixthapp.desktop', pid=4242)
        writer.finish_error('the download failed',
                            force_quit={'count': 1, 'sigkill': True})
        doc = jobs.read_job('sixthapp.desktop')
        self.assertEqual(doc['phase'], 'error')
        self.assertEqual(doc['status'], 'error')
        self.assertEqual(doc['error'], 'the download failed')
        self.assertEqual(doc['force_quit'], {'count': 1, 'sigkill': True})


class AsyncGrantTests(_RunningAppTestCase):
    """The grant has to survive the detach: --async forwards the flag to
    the grandchild, and the grandchild hands it to run_update."""

    def test_job_entrypoint_carries_the_kill_running_grant(self):
        result = cli.UpdateResult('up-to-date', el=mock.Mock(file_path='/x'))
        with mock.patch.object(cli, 'run_update',
                               return_value=result) as run:
            code = cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                       keep_both=False, force=False,
                                       kill_running=True)
        self.assertEqual(code, 0)
        self.assertTrue(run.call_args.kwargs['kill_running'])

    def test_job_entrypoint_defaults_to_no_grant(self):
        result = cli.UpdateResult('up-to-date', el=mock.Mock(file_path='/x'))
        with mock.patch.object(cli, 'run_update',
                               return_value=result) as run:
            cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                keep_both=False, force=False)
        self.assertFalse(run.call_args.kwargs['kill_running'])

    def test_async_spawn_forwards_the_flag(self):
        captured = {}

        def spawn(desktop_id, target, keep_both, force, log_fd,
                  kill_running=False):
            captured.update(desktop_id=desktop_id, target=target,
                            kill_running=kill_running)
            return 4242, '/tmp/job.json', '/tmp/job.log'

        with mock.patch.object(cli, '_spawn_update_job', side_effect=spawn), \
                redirect_stdout(io.StringIO()):
            code = cli._cmd_update_async('fakeapp', keep_both=False,
                                         force=False, kill_running=True,
                                         json_output=True)
        self.assertEqual(code, 0)
        self.assertEqual(captured['desktop_id'], 'fakeapp.desktop')
        self.assertTrue(captured['kill_running'])

    def test_cmd_update_parses_the_flag(self):
        captured = {}
        with mock.patch.object(cli, '_cmd_update_async',
                               side_effect=lambda target, keep_both, force,
                               kill_running, json_output, assume_yes=False:
                               captured.update(kill_running=kill_running,
                                               assume_yes=assume_yes)
                               or 0):
            code = cli.cmd_update(['fakeapp', '--async', '--json', '--yes',
                                   '--kill-running'])
        self.assertEqual(code, 0)
        self.assertTrue(captured['kill_running'])

    def test_cmd_update_hands_yes_down_to_the_async_branch(self):
        captured = {}
        with mock.patch.object(cli, '_cmd_update_async',
                               side_effect=lambda target, keep_both, force,
                               kill_running, json_output, assume_yes=False:
                               captured.update(assume_yes=assume_yes) or 0):
            cli.cmd_update(['fakeapp', '--async', '--yes'])
        self.assertTrue(captured['assume_yes'])

    # -------------------------------------------------- finding D / G6

    def _spy(self):
        captured = {}

        def spawn(desktop_id, target, keep_both, force, log_fd,
                  kill_running=False):
            captured.update(desktop_id=desktop_id, target=target,
                            kill_running=kill_running, spawned=True)
            return 4242, '/tmp/job.json', '/tmp/job.log'
        return captured, spawn

    def _async(self, tty, answer, running=True, keep_both=False,
               assume_yes=False, json_output=False):
        """Run the async parent in-process; returns (code, captured,
        asks, stdout)."""
        captured, spawn = self._spy()
        asks = mock.Mock(return_value=answer)
        out = io.StringIO()
        with mock.patch.object(cli, '_spawn_update_job', side_effect=spawn), \
                mock.patch.object(cli, '_stdin_is_tty', return_value=tty), \
                mock.patch.object(cli.appimage_provider, 'is_app_running',
                                  return_value=running), \
                mock.patch('builtins.input', asks), \
                redirect_stdout(out):
            code = cli._cmd_update_async('fakeapp', keep_both=keep_both,
                                         force=False, json_output=json_output,
                                         assume_yes=assume_yes)
        return code, captured, asks, out.getvalue()

    def test_async_asks_on_a_tty_and_forwards_the_grant(self):
        code, captured, asks, _out = self._async(tty=True, answer='y')
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 1)
        self.assertTrue(captured['spawned'])
        self.assertTrue(captured['kill_running'])

    def test_async_decline_starts_no_job(self):
        code, captured, asks, out = self._async(tty=True, answer='n')
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 1)
        self.assertNotIn('spawned', captured)
        self.assertIn('--kill-running', out)

    def test_async_never_asks_without_a_tty(self):
        code, captured, asks, _out = self._async(tty=False, answer=None)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertFalse(captured['kill_running'])

    def test_async_never_asks_with_yes(self):
        code, captured, asks, _out = self._async(tty=True, answer=None,
                                                 assume_yes=True)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertFalse(captured['kill_running'])

    def test_async_never_asks_with_keep_both(self):
        # finding C: nothing is replaced in place, so nothing is closed
        code, captured, asks, _out = self._async(tty=True, answer=None,
                                                 keep_both=True)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertFalse(captured['kill_running'])

    def test_async_does_not_ask_when_the_app_is_not_running(self):
        code, captured, asks, _out = self._async(tty=True, answer=None,
                                                 running=False)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertFalse(captured['kill_running'])

    def test_async_with_the_flag_never_asks(self):
        captured, spawn = self._spy()
        asks = mock.Mock(return_value=AssertionError)
        with mock.patch.object(cli, '_spawn_update_job', side_effect=spawn), \
                mock.patch.object(cli, '_stdin_is_tty', return_value=True), \
                mock.patch.object(cli.appimage_provider, 'is_app_running',
                                  return_value=True), \
                mock.patch('builtins.input', asks), \
                redirect_stdout(io.StringIO()):
            code = cli._cmd_update_async('fakeapp', keep_both=False,
                                         force=False, kill_running=True,
                                         json_output=False)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertTrue(captured['kill_running'])


class ErrorDocumentForceQuitTests(_RunningAppTestCase):
    """Finding G / spec G4: a job that closed the app and then failed must
    publish the close on its `error` document — that is the difference
    between "the update failed, the app is still there" and "the update
    failed and the app was closed for nothing"."""

    def setUp(self):
        super().setUp()
        self.probe = self._probe()
        probe_patch = mock.patch.object(procs, '_PROBE', self.probe)
        probe_patch.start()
        self.addCleanup(probe_patch.stop)
        manager = mock.Mock(download_size=10, available_version='v2.0',
                            embedded=False)
        manager.name = 'StaticFileUpdater'
        manager.is_update_available.return_value = True
        self.manager = manager

    def _run_failing(self, kill_running=True, sigkill=False, running=True):
        reactions = ({PARENT_PID: 'survive-term',
                      CHILD_PID: 'survive-term'} if sigkill else {})
        self.probe = self._probe(parents=running, children=running,
                                 reactions=reactions)
        with mock.patch.object(procs, '_PROBE', self.probe), \
                mock.patch.object(cli.UpdateManagerChecker,
                                  'check_url_for_app',
                                  return_value=self.manager), \
                mock.patch.object(cli.appimage_provider, 'update_from_url',
                                  side_effect=cli.OperationError(
                                      'the download failed')):
            code = cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                      keep_both=False, force=False,
                                      kill_running=kill_running)
        return code, jobs.read_job('fakeapp.desktop')

    def test_error_document_carries_the_close_that_happened(self):
        code, doc = self._run_failing()
        self.assertEqual(code, 1)
        self.assertEqual(doc['status'], 'error')
        self.assertEqual(doc['error'], 'the download failed')
        self.assertEqual(doc['force_quit'], {'count': 2, 'sigkill': False})

    def test_error_document_keeps_the_sigkill_bit(self):
        code, doc = self._run_failing(sigkill=True)
        self.assertEqual(code, 1)
        self.assertEqual(doc['force_quit'], {'count': 2, 'sigkill': True})

    def test_error_document_without_a_close_stays_clean(self):
        # nothing was closed (the app was not running), so the document
        # carries no trace of a close
        code, doc = self._run_failing(kill_running=True, running=False)
        self.assertEqual(code, 1)
        self.assertEqual(doc['status'], 'error')
        self.assertNotIn('force_quit', doc)


class RunningGuardCliTests(_RunningAppTestCase):
    """cmd_update's flag parsing and the one interactive question, run
    in-process against a source that HAS an update (the guard now runs
    after the availability check, so an up-to-date source would stop the
    flow before the question — finding A). update_from_url is stubbed, so
    nothing downloads."""

    def run_cmd(self, func, args: list):
        """Run a cmd_* function in-process (same shape as test_cli's
        InProcessCliMixin, so argv handling is covered, not just the
        core)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = func(args)
            except cli.UsageError as e:
                err.write(f'Error: {e}\n')
                code = 2
            except (cli.OperationError, cli.InternalError) as e:
                err.write(f'Error: {e}\n')
                code = 1
        return out.getvalue(), err.getvalue(), code

    def setUp(self):
        super().setUp()
        with open(self.appimage, 'rb') as f:
            body = f.read()
        server = FixtureHTTPServer({'/f.appimage': (200, {}, body)})
        self.addCleanup(server.close)
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage')})
        self.mounts = os.path.join(self.sandbox, 'mounts')
        # the measured shape of a live type-2 runtime's entry: the SOURCE
        # field is the file's basename, never the absolute path (that is
        # what 0.9.0's basename matching exists for)
        Path(self.mounts).write_text(
            f'fakeapp.appimage {MOUNT} fuse.fakeapp.appimage rw 0 0\n')
        self.probe = self._probe()
        probe_patch = mock.patch.object(procs, '_PROBE', self.probe)
        probe_patch.start()
        self.addCleanup(probe_patch.stop)
        # an update IS available: otherwise the availability check answers
        # first and the guard is never reached
        # every attribute the JSON document reads must be a real value
        manager = mock.Mock(download_size=10, available_version='v2.0',
                            embedded=False)
        manager.name = 'StaticFileUpdater'      # `name=` would christen it
        manager.is_update_available.return_value = True
        for target, kwargs in (
                ((cli.UpdateManagerChecker, 'check_url_for_app'),
                 {'return_value': manager}),
                ((cli.appimage_provider, 'update_from_url'),
                 {'side_effect': self._install})):
            patcher = mock.patch.object(*target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _install(manager, el, keep_both=False, progress_cb=None,
                 before_replace=None):
        # the seam is not exercised here (RunUpdateKillTests owns it): the
        # job-start guard is what these tests are about. The returned
        # element is read for its identity by the state record (issue
        # #53), so it carries the real attributes a real install element
        # has — a Mock's auto-created ones are truthy non-paths.
        return mock.Mock(version='v2.0', size=11,
                         file_path='/tmp/fakeapp.appimage',
                         desktop_file_path='/tmp/fakeapp.desktop')

    def _run(self, args, tty, answer=None, runner=None):
        runner = runner or (lambda: self.run_cmd(cli.cmd_update, args))
        asks = mock.Mock(return_value=answer)
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', self.mounts), \
                mock.patch.object(cli, '_stdin_is_tty', return_value=tty), \
                mock.patch('builtins.input', asks):
            out, err, code = runner()
        return out, err, code, asks

    def test_non_tty_without_the_flag_skips_and_names_it(self):
        out, _err, code, asks = self._run(['fakeapp'], tty=False)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertIn('--kill-running', out)
        self.assertIn('--force', out)
        self.assertEqual(self.probe.sent, [])

    def test_tty_accept_closes_the_app_and_continues(self):
        out, err, code, asks = self._run(['fakeapp'], tty=True, answer='y')
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 1)
        self.assertIn('is running', err)
        self.assertIn('was updated successfully', out)
        self.assertEqual(self._acted_on(self.probe),
                         [PARENT_PID, CHILD_PID])

    def test_tty_accept_is_case_and_space_insensitive(self):
        _out, _err, _code, asks = self._run(['fakeapp'], tty=True,
                                            answer=' Yes \n')
        self.assertEqual(asks.call_count, 1)
        self.assertEqual(self._acted_on(self.probe),
                         [PARENT_PID, CHILD_PID])

    def test_tty_decline_skips_and_signals_nothing(self):
        out, _err, code, asks = self._run(['fakeapp'], tty=True, answer='n')
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 1)
        self.assertIn('--kill-running', out)
        self.assertEqual(self.probe.sent, [])

    def test_eof_on_the_question_declines(self):
        asks = mock.Mock(side_effect=EOFError)
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', self.mounts), \
                mock.patch.object(cli, '_stdin_is_tty', return_value=True), \
                mock.patch('builtins.input', asks):
            out, _err, code = self.run_cmd(cli.cmd_update, ['fakeapp'])
        self.assertEqual(code, 0)
        self.assertIn('--kill-running', out)
        self.assertEqual(self.probe.sent, [])

    def test_non_tty_with_the_flag_never_asks(self):
        out, _err, code, asks = self._run(['fakeapp', '--kill-running'],
                                          tty=False)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertIn('was updated successfully', out)
        self.assertEqual(self._acted_on(self.probe),
                         [PARENT_PID, CHILD_PID])

    def test_yes_never_asks_even_on_a_tty(self):
        out, _err, code, asks = self._run(['fakeapp', '--yes'], tty=True,
                                          answer=AssertionError)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertIn('--kill-running', out)
        self.assertEqual(self.probe.sent, [])

    def test_json_mode_never_asks_even_on_a_tty(self):
        out, _err, code, asks = self._run(['fakeapp', '--json', '--yes'],
                                          tty=True, answer=AssertionError)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        payload = json.loads(out)          # stdout stayed a document
        self.assertEqual(payload['result'], 'skipped-running')
        self.assertIn('--kill-running', payload['message'])

    def test_force_still_replaces_silently_without_signalling(self):
        out, _err, code, asks = self._run(['fakeapp', '--force'], tty=True,
                                          answer=AssertionError)
        self.assertEqual(code, 0)
        self.assertEqual(asks.call_count, 0)
        self.assertIn('was updated successfully', out)
        self.assertEqual(self.probe.sent, [])

    def test_ctrl_c_on_the_question_declines(self):
        # finding 9: an interrupt at the prompt degrades to
        # `skipped-running`, exactly like a plain "no"
        asks = mock.Mock(side_effect=KeyboardInterrupt)
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', self.mounts), \
                mock.patch.object(cli, '_stdin_is_tty', return_value=True), \
                mock.patch('builtins.input', asks):
            out, _err, code = self.run_cmd(cli.cmd_update, ['fakeapp'])
        self.assertEqual(code, 0)
        self.assertIn('--kill-running', out)
        self.assertEqual(self.probe.sent, [])

    def test_json_requires_yes_with_the_flag(self):
        out, _err, code, asks = self._run(['fakeapp', '--json',
                                          '--kill-running'], tty=False)
        self.assertEqual(code, 2)
        self.assertEqual(asks.call_count, 0)
        self.assertEqual(json.loads(out)['result'], 'error')
        self.assertEqual(self.probe.sent, [])


if __name__ == '__main__':
    unittest.main()
# test_jobs.py — background update-job tests (--update --async).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Test suite written for this plugin. Covers: the job-document store and
# its liveness classification (unit level), the JobWriter phase walk, the
# grandchild entrypoint against the loopback fixture server (in-process,
# seams patched), the parent-side --async CLI behaviour (spawn mocked or
# refused pre-fork — the test process never forks), and one real
# subprocess detach run through main.py in the fake-XDG sandbox.

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import FakeXDGTestCase, FixtureHTTPServer, download_fixture
from cli_support import InProcessCliMixin

from omarchy_appimage import cli, extractor, jobs, net, updaters, utils
from omarchy_appimage.ini_config import Config

BACKEND_DIR = helpers.BACKEND_DIR


class _DocPoller(threading.Thread):
    """Samples a job document while the job runs; every existing file
    must parse (atomic rewrites under a concurrent reader)."""

    def __init__(self, path: str, interval: float = 0.005):
        super().__init__(daemon=True)
        self.path = path
        self.interval = interval
        self.samples = []          # (phase, progress, heartbeat)
        self.registry_samples = [] # extractor.temp_dirs_snapshot() over time
        self.parse_errors = 0
        self.first_sample = threading.Event()  # set on the first parse
        self._stop = threading.Event()

    def _sample_once(self):
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                doc = json.load(f)
        except FileNotFoundError:
            return
        except ValueError:
            # a rewrite exposed a partial document: atomicity broken
            self.parse_errors += 1
            return
        self.samples.append((doc.get('phase'), doc.get('progress'),
                             doc.get('heartbeat')))
        self.registry_samples.append(extractor.temp_dirs_snapshot())
        self.first_sample.set()

    def run(self):
        while not self._stop.wait(self.interval):
            self._sample_once()

    def stop(self):
        self._stop.set()
        self.join(timeout=5)
        # the writer may have published its terminal rewrite after the
        # loop's last tick: sample once more, synchronously, so the
        # recorded walk always ends at the document the entrypoint left
        self._sample_once()

    def phases(self) -> list:
        seen = []
        for phase, _progress, _heartbeat in self.samples:
            if not seen or seen[-1] != phase:
                seen.append(phase)
        return seen


def _zsync_control(server, body: bytes) -> bytes:
    return (f'URL: {server.url("/update.appimage")}\n'
            f'SHA-1: {hashlib.sha1(body).hexdigest()}\n\n'
            'zsync: 0.6.2\n').encode()


class JobStoreTests(FakeXDGTestCase):
    """jobs.py unit behaviour: paths, atomic store, liveness."""

    def test_job_document_path_strips_desktop_suffix(self):
        path = jobs.job_document_path('fakeapp.desktop')
        self.assertEqual(path,
                         os.path.join(self.plugin_jobs_dir, 'fakeapp.json'))
        self.assertEqual(jobs.job_document_path('fakeapp'), path)
        self.assertEqual(jobs.job_log_path('fakeapp.desktop'),
                         os.path.join(self.plugin_jobs_dir, 'fakeapp.log'))

    def test_unsafe_desktop_id_is_refused(self):
        for bad in ('../evil.desktop', 'a/b', '.hidden', '', None):
            with self.assertRaises(ValueError):
                jobs.job_document_path(bad)

    def test_desktop_id_usability_rule_is_probeable(self):
        # the same rule _job_stem enforces, exposed so the CLI can refuse
        # an unusable id with the contract's error shape BEFORE it writes
        # anything (a legal .desktop filename may hold a space)
        for good in ('fakeapp', 'fakeapp.desktop',
                     'org.gnome.Calculator.desktop',
                     'a_b-c.1.desktop'):
            self.assertTrue(jobs.is_usable_desktop_id(good), good)
        for bad in ('my app.desktop', 'caf\u00e9.desktop', '.hidden',
                    '', '../evil.desktop', 'a/b', None, 42):
            self.assertFalse(jobs.is_usable_desktop_id(bad), bad)

    def test_write_read_roundtrip_and_no_temp_residue(self):
        doc = {'schema_version': jobs.JOB_SCHEMA_VERSION,
               'desktop_id': 'fakeapp.desktop', 'phase': 'starting'}
        jobs.write_job('fakeapp.desktop', doc)
        self.assertEqual(jobs.read_job('fakeapp.desktop'), doc)
        # every rewrite is atomic: no .tmp-* leftovers next to the document
        self.assertEqual(os.listdir(self.plugin_jobs_dir),
                         ['fakeapp.json'])

    def test_read_missing_or_corrupt_document_is_none(self):
        self.assertIsNone(jobs.read_job('ghost.desktop'))
        os.makedirs(self.plugin_jobs_dir, exist_ok=True)
        Path(self.plugin_jobs_dir, 'broken.json').write_text('{nope')
        # corrupt documents read as None under the broken.json stem
        self.assertIsNone(jobs.read_job('broken.json'))

    def test_pid_alive(self):
        self.assertTrue(jobs.pid_alive(os.getpid()))
        self.assertFalse(jobs.pid_alive(0))
        self.assertFalse(jobs.pid_alive(-1))
        self.assertFalse(jobs.pid_alive(None))
        self.assertFalse(jobs.pid_alive('not-a-pid'))
        # a reaped pid is dead
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait()
        self.assertFalse(jobs.pid_alive(proc.pid))

    def _live_doc(self, pid=None, heartbeat=None, phase='downloading'):
        return {
            'schema_version': jobs.JOB_SCHEMA_VERSION,
            'desktop_id': 'fakeapp.desktop',
            'phase': phase,
            'pid': os.getpid() if pid is None else pid,
            'heartbeat': time.time() if heartbeat is None else heartbeat,
        }

    def test_classify_terminal_phases_are_final(self):
        for phase in ('done', 'error'):
            self.assertEqual(
                jobs.classify_job(self._live_doc(phase=phase)), phase)
        # a terminal document stays terminal even with a dead pid
        self.assertEqual(jobs.classify_job(self._live_doc(pid=0,
                                                          phase='done')),
                         'done')

    def test_classify_live_pid_fresh_heartbeat_is_active(self):
        self.assertEqual(jobs.classify_job(self._live_doc()), 'active')

    def test_classify_dead_pid_is_interrupted(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'])
        proc.wait()
        self.assertEqual(
            jobs.classify_job(self._live_doc(pid=proc.pid)), 'interrupted')

    def test_classify_stale_heartbeat_is_interrupted(self):
        stale = time.time() - (jobs.JOB_STALE_AFTER_SECONDS + 1)
        self.assertEqual(
            jobs.classify_job(self._live_doc(heartbeat=stale)),
            'interrupted')

    def test_classify_malformed_document_is_interrupted(self):
        self.assertEqual(jobs.classify_job(None), 'interrupted')
        self.assertEqual(jobs.classify_job({'pid': 'x', 'heartbeat': 'y'}),
                         'interrupted')

    def test_active_job_matches_only_active_documents(self):
        self.assertIsNone(jobs.active_job('fakeapp.desktop'))
        jobs.write_job('fakeapp.desktop', self._live_doc())
        self.assertIsNotNone(jobs.active_job('fakeapp.desktop'))
        # terminal and interrupted documents are not active
        jobs.write_job('fakeapp.desktop', self._live_doc(phase='done'))
        self.assertIsNone(jobs.active_job('fakeapp.desktop'))
        stale = time.time() - (jobs.JOB_STALE_AFTER_SECONDS + 1)
        jobs.write_job('fakeapp.desktop', self._live_doc(heartbeat=stale))
        self.assertIsNone(jobs.active_job('fakeapp.desktop'))


class JobLogTests(FakeXDGTestCase):
    """The per-job log: append-mode fd next to the document, capped."""

    def test_log_fd_appends_next_to_the_document(self):
        fd = jobs.open_job_log_fd('fakeapp.desktop')
        os.write(fd, b'first run\n')
        os.close(fd)
        fd = jobs.open_job_log_fd('fakeapp.desktop')
        os.write(fd, b'second run\n')
        os.close(fd)
        with open(jobs.job_log_path('fakeapp.desktop'), 'rb') as f:
            self.assertEqual(f.read(), b'first run\nsecond run\n')

    def test_oversized_log_is_truncated_on_open(self):
        path = jobs.job_log_path('fakeapp.desktop')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'x' * (jobs.JOB_LOG_MAX_BYTES + 1))
        fd = jobs.open_job_log_fd('fakeapp.desktop')
        os.write(fd, b'fresh\n')
        os.close(fd)
        with open(path, 'rb') as f:
            self.assertEqual(f.read(), b'fresh\n')

    def test_second_claim_is_refused(self):
        # the open fd IS the claim: while one job holds it, a second open
        # must not be granted (and must not touch the log it is writing).
        # A TOCTOU-free claim is what makes "--async refuses immediately"
        # true even when both starts race before either document exists.
        fd = jobs.open_job_log_fd('fakeapp.desktop')
        os.write(fd, b'live job output\n')
        with self.assertRaises(BlockingIOError):
            jobs.open_job_log_fd('fakeapp.desktop')
        with open(jobs.job_log_path('fakeapp.desktop'), 'rb') as f:
            self.assertEqual(f.read(), b'live job output\n')
        os.close(fd)

    def test_claim_is_released_when_the_holder_closes(self):
        # the kernel drops a flock with the last descriptor for that open
        # file description: process death and an explicit close alike
        # leave no stale claim file behind (there is no claim file)
        os.close(jobs.open_job_log_fd('fakeapp.desktop'))
        fd = jobs.open_job_log_fd('fakeapp.desktop')
        os.close(fd)
        self.assertEqual(sorted(os.listdir(self.plugin_jobs_dir)),
                         ['fakeapp.log'])

    def test_oversized_log_is_not_truncated_without_the_claim(self):
        # truncating an over-cap log only under the held claim is what
        # keeps a refused second start from wiping the running job's log
        path = jobs.job_log_path('fakeapp.desktop')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'x' * (jobs.JOB_LOG_MAX_BYTES + 1))
        holder = jobs.open_job_log_fd('fakeapp.desktop')
        try:
            with open(path, 'wb') as f:
                f.write(b'y' * (jobs.JOB_LOG_MAX_BYTES + 1))
            with self.assertRaises(BlockingIOError):
                jobs.open_job_log_fd('fakeapp.desktop')
            self.assertEqual(os.path.getsize(path),
                             jobs.JOB_LOG_MAX_BYTES + 1)
        finally:
            os.close(holder)


class JobWriterTests(FakeXDGTestCase):
    """JobWriter: the document walk the grandchild publishes."""

    def setUp(self):
        super().setUp()
        self.writer = jobs.JobWriter('fakeapp.desktop', pid=4242)

    def _doc(self):
        return jobs.read_job('fakeapp.desktop')

    def test_initial_document_is_starting(self):
        doc = self._doc()
        self.assertEqual(doc['schema_version'], jobs.JOB_SCHEMA_VERSION)
        self.assertEqual(doc['phase'], 'starting')
        self.assertEqual(doc['pid'], 4242)
        self.assertEqual(doc['desktop_id'], 'fakeapp.desktop')
        self.assertLessEqual(doc['started'], doc['heartbeat'])
        self.assertIsNone(doc['status'])
        self.assertIsNone(doc['error'])

    def test_stage_downloading_records_declared_size(self):
        manager = mock.Mock(download_size=5000)
        self.writer.stage('downloading', manager)
        doc = self._doc()
        self.assertEqual(doc['phase'], 'downloading')
        self.assertEqual(doc['total_bytes'], 5000)
        self.assertEqual(doc['downloaded_bytes'], 0)

    def test_progress_walks_downloading_then_verifying(self):
        self.writer.stage('downloading', mock.Mock(download_size=1000))
        self.writer.progress_cb(0.5)
        doc = self._doc()
        self.assertEqual(doc['phase'], 'downloading')
        self.assertEqual(doc['progress'], 0.5)
        self.assertEqual(doc['downloaded_bytes'], 500)
        # top fraction: the body is complete, the digest check runs next
        self.writer.progress_cb(1.0)
        doc = self._doc()
        self.assertEqual(doc['phase'], 'verifying')
        self.assertEqual(doc['progress'], 1.0)

    def test_stage_replacing_then_finish_updated(self):
        self.writer.stage('replacing')
        doc = self._doc()
        self.assertEqual(doc['phase'], 'replacing')
        self.assertEqual(doc['progress'], 1.0)

        self.writer.finish('updated', message='all good',
                           new_version='v2.0', downloaded_bytes=4321)
        doc = self._doc()
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'updated')
        self.assertEqual(doc['new_version'], 'v2.0')
        self.assertEqual(doc['downloaded_bytes'], 4321)
        self.assertIsNone(doc['error'])

    def test_finish_up_to_date_and_skipped_running_are_done(self):
        self.writer.finish('up-to-date', message='nothing to do')
        doc = self._doc()
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'up-to-date')

        writer2 = jobs.JobWriter('fakeapp.desktop', pid=4242)
        writer2.finish('skipped-running', message='app is running')
        doc = self._doc()
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'skipped-running')

    def test_finish_error_is_terminal_error(self):
        self.writer.finish_error('download exploded')
        doc = self._doc()
        self.assertEqual(doc['phase'], 'error')
        self.assertEqual(doc['status'], 'error')
        self.assertEqual(doc['error'], 'download exploded')
        self.assertEqual(doc['message'], 'download exploded')

    def test_heartbeat_thread_refreshes_the_document(self):
        with mock.patch.object(jobs, 'JOB_HEARTBEAT_INTERVAL_SECONDS',
                               0.05):
            self.writer.start_heartbeat()
            first = self._doc()['heartbeat']
            time.sleep(0.35)
            second = self._doc()['heartbeat']
            self.writer.stop_heartbeat()
        self.assertGreater(second, first)
        # after stop the document stops refreshing
        third = self._doc()['heartbeat']
        time.sleep(0.2)
        self.assertEqual(self._doc()['heartbeat'], third)


class JobWriterLogCapTests(FakeXDGTestCase):
    """The log cap is enforced DURING the run, not only on open: one long
    verification/replace phase would otherwise grow the log past
    JOB_LOG_MAX_BYTES without bound, which the size-capped-log contract
    does not allow."""

    def setUp(self):
        super().setUp()
        self.writer = jobs.JobWriter('fakeapp.desktop', pid=4242)
        self.log_path = jobs.job_log_path('fakeapp.desktop')

    def test_under_cap_log_is_left_alone(self):
        Path(self.log_path).write_bytes(b'still here\n')
        self.writer.enforce_log_cap()
        with open(self.log_path, 'rb') as f:
            self.assertEqual(f.read(), b'still here\n')

    def test_over_cap_log_is_dropped_while_the_job_runs(self):
        Path(self.log_path).write_bytes(b'x' * (jobs.JOB_LOG_MAX_BYTES + 1))
        self.writer.enforce_log_cap()
        self.assertEqual(os.path.getsize(self.log_path), 0)

    def test_enforcement_survives_a_vanished_log(self):
        # a state dir that goes away mid-job must not kill the job
        Path(self.log_path).write_bytes(b'gone soon\n')
        os.unlink(self.log_path)
        self.writer.enforce_log_cap()

    def test_heartbeat_thread_enforces_the_cap(self):
        # the heartbeat thread is the natural owner of the periodic cap
        with mock.patch.object(jobs, 'JOB_HEARTBEAT_INTERVAL_SECONDS',
                               0.02):
            Path(self.log_path).write_bytes(
                b'x' * (jobs.JOB_LOG_MAX_BYTES + 1))
            self.writer.start_heartbeat()
            deadline = time.time() + 3
            while (os.path.getsize(self.log_path) and
                   time.time() < deadline):
                time.sleep(0.01)
            self.writer.stop_heartbeat()
        self.assertEqual(os.path.getsize(self.log_path), 0)


class AsyncJobCliTests(InProcessCliMixin, FakeXDGTestCase):
    """Parent-side --async CLI behaviour (spawn mocked or refused
    pre-fork — the test process never forks) and the grandchild
    entrypoint's terminal documents without a download."""

    def setUp(self):
        super().setUp()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)
        self.install_fake_app()

    def _seed_active_job(self):
        doc = {
            'schema_version': jobs.JOB_SCHEMA_VERSION,
            'desktop_id': 'fakeapp.desktop',
            'phase': 'downloading',
            'pid': os.getpid(),
            'heartbeat': time.time(),
        }
        jobs.write_job('fakeapp.desktop', doc)
        return doc

    def test_job_started_json_document(self):
        with mock.patch.object(
                cli, '_spawn_update_job',
                return_value=(4242, '/jobs/fakeapp.json',
                              '/jobs/fakeapp.log')) as spawn:
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json', '--async'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload, {
            'schema_version': 1,
            'result': 'job-started',
            'desktop_id': 'fakeapp.desktop',
            'job_path': '/jobs/fakeapp.json',
            'log_path': '/jobs/fakeapp.log',
            'pid': 4242,
        })
        # the claim fd opened by the parent is handed to the spawner (the
        # grandchild dup2s it onto stdout/stderr, which keeps the flock
        # alive for the job's whole life)
        spawn.assert_called_once_with('fakeapp.desktop', 'fakeapp',
                                      keep_both=False, force=False,
                                      log_fd=mock.ANY, kill_running=False)

    def test_job_started_plain_text(self):
        with mock.patch.object(cli, '_spawn_update_job',
                               return_value=(4242, '/j.json', '/j.log')):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--async'])
        self.assertEqual(code, 0, err)
        self.assertIn('Update job started', out)
        self.assertIn('fakeapp.desktop', out)

    def test_async_json_still_requires_yes(self):
        out, err, code = self.run_cmd(
            cli.cmd_update, ['fakeapp', '--json', '--async'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)['result'], 'error')

    def test_active_job_refusal(self):
        seeded = self._seed_active_job()
        out, err, code = self.run_cmd(
            cli.cmd_update, ['fakeapp', '--yes', '--json', '--async'])
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('already active', payload['error'])
        self.assertIn('fakeapp.desktop', payload['error'])
        # the first job's document is untouched and nothing was spawned
        self.assertEqual(jobs.read_job('fakeapp.desktop'), seeded)
        # the refusal took (and released) the claim, so the log exists but
        # is EMPTY: nothing a second start does may reach the first job's
        # log, and it never reopens it for writing
        log_path = jobs.job_log_path('fakeapp.desktop')
        self.assertEqual(os.path.getsize(log_path), 0)

    def _assert_refused(self, out, code):
        self.assertEqual(code, 1)
        payload = json.loads(out)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'error')
        self.assertEqual(payload['desktop_id'], 'fakeapp.desktop')
        return payload['error']

    def test_second_start_is_refused_while_the_claim_is_held(self):
        # TOCTOU: the first job's document only appears once its grandchild
        # writes it, so two simultaneous starts used to both pass the
        # active-job check and both write the same log. The claim is now
        # taken first, atomically, so the loser is refused immediately.
        seeded = self._seed_active_job()
        holder = jobs.open_job_log_fd('fakeapp.desktop')
        os.write(holder, b'first job output\n')
        try:
            with mock.patch.object(cli, '_spawn_update_job') as spawn:
                out, err, code = self.run_cmd(
                    cli.cmd_update,
                    ['fakeapp', '--yes', '--json', '--async'])
            spawn.assert_not_called()
        finally:
            os.close(holder)
        error = self._assert_refused(out, code)
        self.assertIn('already active', error)
        self.assertIn('fakeapp.desktop', error)
        # the first job's log and document are untouched
        with open(jobs.job_log_path('fakeapp.desktop'), 'rb') as f:
            self.assertEqual(f.read(), b'first job output\n')
        self.assertEqual(jobs.read_job('fakeapp.desktop'), seeded)

    def test_claim_refusal_is_a_contract_error_on_the_plain_path(self):
        self._seed_active_job()
        holder = jobs.open_job_log_fd('fakeapp.desktop')
        try:
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--async'])
        finally:
            os.close(holder)
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('Error:', err)
        self.assertIn('already active', err)

    def test_unusable_desktop_id_is_a_contract_error_document(self):
        # a .desktop filename may legally hold a space or a non-ASCII
        # char, but the job file name may not. The sync path accepts such
        # an id, so --async must refuse it with the contract's error shape
        # (never a ValueError escaping as "unexpected error" + traceback)
        # and must stay side-effect free. Driven through main(), because
        # that is the layer whose catch-all used to swallow it.
        self.install_fake_app(desktop_name='my app', app_name='Spaced')
        with mock.patch.object(cli.janitor, 'sweep') as sweep, \
                mock.patch.object(cli, '_spawn_update_job') as spawn:
            out, err, code = self.run_cmd(
                cli.main,
                ['appimage', '--update', 'my app', '--yes', '--json',
                 '--async'])
        sweep.assert_not_called()
        spawn.assert_not_called()
        self.assertEqual(code, 1, err)
        payload = json.loads(out)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['result'], 'error')
        self.assertIn('my app.desktop', payload['error'])
        # the reason is named, and nothing was created in the jobs dir
        self.assertIn('not usable as a job file name', payload['error'])
        self.assertFalse(os.path.exists(self.plugin_jobs_dir))
        self.assertNotIn('Traceback', err)

    def test_unusable_desktop_id_is_a_plain_error_too(self):
        self.install_fake_app(desktop_name='caf\u00e9', app_name='Cafe')
        out, err, code = self.run_cmd(
            cli.main,
            ['appimage', '--update', 'caf\u00e9', '--yes', '--async'])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('Error:', err)
        self.assertIn('not usable as a job file name', err)
        self.assertNotIn('Traceback', err)

    def _set_source(self, url):
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater', {'url': url})

    def test_skipped_running_terminal_document(self):
        # source points at a closed port: any download attempt would fail
        # the job, so a `skipped-running` document proves the guard fired
        # before any network traffic
        self._set_source('http://127.0.0.1:9/f.appimage.zsync')
        mounts = os.path.join(self.sandbox, 'mounts')
        Path(mounts).write_text(
            f'{self.managed_dir}/fakeapp.appimage /tmp/.mount_x '
            'fuse.AppImage rw 0 0\n')

        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', mounts), \
                mock.patch.object(
                    updaters.StaticFileUpdater, 'is_update_available',
                    return_value=True), \
                mock.patch.object(cli.appimage_provider,
                                  'update_from_url') as install:
            code = cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                       keep_both=False, force=False)
        # availability says "there is an update", so the guard is the
        # thing that stopped the job — and it stopped it before the
        # download (finding A: the guard now runs after the availability
        # check, never before it)
        install.assert_not_called()
        self.assertEqual(code, 0)
        doc = jobs.read_job('fakeapp.desktop')
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'skipped-running')
        self.assertIn('--force', doc['message'])
        # the app image is untouched
        with open(os.path.join(self.managed_dir, 'fakeapp.appimage'),
                  'rb') as f:
            self.assertTrue(f.read().startswith(b'\x7fELF'))

    def test_error_terminal_document(self):
        # a zsync control file without a SHA-1 line is not installable, so
        # the strict policy refuses it before any byte is downloaded ->
        # terminal error document
        server = FixtureHTTPServer(
            {'/f.appimage.zsync': (200, {}, b'URL: /x\n\nno sha here\n')})
        self.addCleanup(server.close)
        self._set_source(server.url('/f.appimage.zsync'))

        code = cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                   keep_both=False, force=False)
        self.assertEqual(code, 1)
        doc = jobs.read_job('fakeapp.desktop')
        self.assertEqual(doc['phase'], 'error')
        self.assertEqual(doc['status'], 'error')
        # the refusal now happens at the policy gate, not inside the
        # download: no SHA-1 line means no usable digest for this source
        self.assertIn('unverified artifact', doc['error'])
        self.assertIn('digest', doc['error'])


class AsyncJobFlowTests(InProcessCliMixin, FakeXDGTestCase):
    """The grandchild entrypoint against a real zsync release served by
    the loopback fixture server: full phase walk, terminal fields, scoped
    temp-dir cleanup. The release is the real type-2 AppImage fixture, so
    the whole download -> verify -> extract -> replace pipeline runs."""

    def setUp(self):
        super().setUp()
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        self.fixture = fixture
        with open(fixture, 'rb') as f:
            self.body = f.read()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)

    def _setup_zsync_app(self, slow_seconds: float = 0.0):
        self.install_fake_app()
        server = FixtureHTTPServer({
            '/update.appimage': self._slow_route(slow_seconds),
        })
        server.routes['/f.appimage.zsync'] = (
            200, {}, _zsync_control(server, self.body))
        self.addCleanup(server.close)

        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage.zsync')})
        return server

    def _slow_route(self, seconds: float):
        body = self.body

        def _serve(handler):
            if handler.command == 'HEAD':
                return (200, {'Content-Length': str(len(body))}, b'')
            if seconds:
                time.sleep(seconds)
            return (200, {}, body)
        return _serve

    def test_full_phase_sequence(self):
        self._setup_zsync_app(slow_seconds=0.8)

        # observe `verifying` deterministically: the SHA-1 check (the
        # digest tail of StaticFileUpdater.download) sleeps after hashing
        real_hash = updaters.get_file_hash

        def slow_sha1(path, alg='md5'):
            result = real_hash(path, alg=alg)
            if alg == 'sha1':
                time.sleep(0.6)
            return result
        hash_patcher = mock.patch.object(updaters, 'get_file_hash',
                                         slow_sha1)
        hash_patcher.start()
        self.addCleanup(hash_patcher.stop)

        # observe `starting` deterministically: the availability check
        # (the pipeline step between the starting document and the first
        # downloading rewrite) waits until the poller has read a document
        poller = _DocPoller(jobs.job_document_path('fakeapp.desktop'))
        real_avail = updaters.StaticFileUpdater.is_update_available

        def gated_availability(self_manager):
            if not poller.first_sample.wait(timeout=30):
                raise AssertionError('poller never observed a job document')
            return real_avail(self_manager)
        avail_patcher = mock.patch.object(
            updaters.StaticFileUpdater, 'is_update_available',
            gated_availability)
        avail_patcher.start()
        self.addCleanup(avail_patcher.stop)

        # pre-seed a pending-notification marker: a successful job must
        # drop it (inherited from run_update, not duplicated)
        state_path = os.path.join(
            self.config_home, 'io.github.glasschan.appimage',
            'updates-state.json')
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        Path(state_path).write_text(
            json.dumps({'fakeapp.desktop': 'x|1'}))

        poller.start()
        try:
            code = cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                       keep_both=False, force=False)
        finally:
            poller.stop()

        self.assertEqual(code, 0)
        self.assertEqual(poller.parse_errors, 0)

        # the document walked every phase, in order
        self.assertIn(['starting', 'downloading', 'verifying', 'replacing',
                       'done'], [poller.phases()])

        doc = jobs.read_job('fakeapp.desktop')
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'updated')
        self.assertEqual(doc['new_version'], 'v0.11.3')
        self.assertEqual(doc['downloaded_bytes'], len(self.body))
        self.assertIsNone(doc['error'])
        self.assertIn('was updated successfully', doc['message'])

        # the fixture AppImage was installed over the fake app
        with open(os.path.join(self.managed_dir, 'fakeapp.appimage'),
                  'rb') as f:
            self.assertEqual(f.read(), self.body)

        # the notification marker was dropped by the inherited core logic
        self.assertEqual(json.loads(Path(state_path).read_text()), {})

        # progress advanced and the heartbeat stayed fresh during the run
        downloading = [s for s in poller.samples if s[0] == 'downloading']
        self.assertTrue(downloading)
        progresses = [s[1] for s in downloading]
        self.assertGreater(max(progresses), 0.0)
        for _phase, _progress, heartbeat in downloading:
            self.assertGreater(heartbeat, time.time() - 30)

        # the pipeline's own temp dirs are gone (registry entries AND
        # their on-disk dirs), captured live while the download ran
        own_dirs = {d for sample in poller.registry_samples for d in sample
                    if 'appimage-update-' in d or 'gearlever_appimage_' in d}
        self.assertTrue(own_dirs)
        final_registry = set(extractor.temp_dirs_snapshot())
        for path in own_dirs:
            self.assertNotIn(path, final_registry)
            self.assertFalse(os.path.exists(path), path)

    def test_scoped_temp_cleanup_leaves_foreign_dirs_alone(self):
        self._setup_zsync_app()

        # a dir some other subsystem registered before the update: the
        # scoped cleanup must NOT wipe it (the old global
        # cleanup_temp_dirs() call in run_update did)
        decoy = extractor.new_temp_dir('decoy-foreign-subsystem-')
        self.addCleanup(extractor.cleanup_temp_dirs)

        result = cli.run_update('fakeapp')
        self.assertEqual(result.status, 'updated')

        # the registry still holds exactly the decoy: the pipeline's own
        # dirs (download + extraction) were removed, nothing else touched
        self.assertEqual(extractor.temp_dirs_snapshot(), (decoy,))
        self.assertTrue(os.path.isdir(decoy))


class AsyncDetachSubprocessTests(FakeXDGTestCase):
    """One real detach run: main.py --update --async as a subprocess
    forks the grandchild, reports job-started and exits while the job
    continues; the document reaches `done` and the AppImage is replaced.
    The test never waits on the grandchild (it is reparented) — it polls
    with a deadline. The loopback test seam is enabled inside the spawned
    CLI via sitecustomize on PYTHONPATH (the same test-only seam the
    in-process tests patch directly; production cannot enable it)."""

    def setUp(self):
        super().setUp()
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable')
        with open(fixture, 'rb') as f:
            self.body = f.read()

        seam_dir = os.path.join(self.sandbox, 'sitecustomize-dir')
        os.makedirs(seam_dir)
        Path(seam_dir, 'sitecustomize.py').write_text(
            'import sys\n'
            f'sys.path.insert(0, {BACKEND_DIR!r})\n'
            'from omarchy_appimage import net\n'
            'net._ALLOW_LOCAL_FOR_TESTS = True\n')
        self.seam_dir = seam_dir

    def test_async_subprocess_detached_run(self):
        appimage, _desktop, _icon = self.install_fake_app()
        body = self.body

        def slow_route(handler):
            if handler.command == 'HEAD':
                return (200, {'Content-Length': str(len(body))}, b'')
            time.sleep(1.5)  # the parent must exit while this sleeps
            return (200, {}, body)

        server = FixtureHTTPServer({
            '/update.appimage': slow_route,
        })
        server.routes['/f.appimage.zsync'] = (
            200, {}, _zsync_control(server, body))
        self.addCleanup(server.close)

        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater',
                                     {'url': server.url('/f.appimage.zsync')})

        result = self.run_cli('--update', 'fakeapp', '--yes', '--json',
                              '--async',
                              extra_env={'PYTHONPATH': self.seam_dir})
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'job-started')
        self.assertEqual(payload['desktop_id'], 'fakeapp.desktop')
        job_path = payload['job_path']
        self.assertEqual(job_path,
                         jobs.job_document_path('fakeapp.desktop'))

        def read_doc():
            try:
                with open(job_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (OSError, ValueError):
                return None

        # the parent returned while the target route was still sleeping:
        # no terminal document can exist yet
        early = read_doc()
        self.assertTrue(early is None
                        or early.get('phase') not in ('done', 'error'))

        # poll until terminal (the grandchild is detached from this tree)
        deadline = time.monotonic() + 120
        doc = None
        while time.monotonic() < deadline:
            doc = read_doc()
            if doc is not None and doc.get('phase') in ('done', 'error'):
                break
            time.sleep(0.25)

        self.assertIsNotNone(doc, 'job did not reach a terminal phase')
        self.assertEqual(doc['phase'], 'done', doc)
        self.assertEqual(doc['status'], 'updated', doc)
        self.assertEqual(doc['new_version'], 'v0.11.3')
        self.assertEqual(doc['downloaded_bytes'], len(body))

        # the fixture AppImage replaced the fake app in the sandbox
        with open(appimage, 'rb') as f:
            self.assertEqual(f.read(), body)

        # the grandchild's log exists (size-capped, next to the document)
        self.assertTrue(os.path.isfile(payload['log_path']))


if __name__ == '__main__':
    unittest.main()

# test_janitor.py — janitor sweep tests (ticket 03).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Test suite written for this plugin. Covers: job-document ageing
# (terminal/interrupted past the acknowledgement window removed with
# their log; active documents never touched; corrupt documents age out
# from their mtime), the orphan `appimage-update-*` grace-window rule
# (effective mtime, prefix scoping, symlink skipping), and the two
# production hooks (--list-installed, async job start in the parent) via
# the in-process cmd_* seam the suite already uses.

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import FakeXDGTestCase
from test_cli import InProcessCliMixin

from omarchy_appimage import cli, janitor, jobs

WINDOW = janitor.JOB_ACK_WINDOW_SECONDS
GRACE = janitor.UPDATE_TEMP_GRACE_SECONDS


def _dead_pid() -> int:
    """A pid that is definitely reaped (test_jobs.py pattern)."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


class _DocSeedMixin:
    """Seeding helpers for the sandbox jobs dir."""

    def _write_doc_file(self, stem: str, doc) -> str:
        os.makedirs(self.plugin_jobs_dir, exist_ok=True)
        path = os.path.join(self.plugin_jobs_dir, stem + '.json')
        Path(path).write_text(json.dumps(doc) if doc is not None
                              else '{nope')  # None seeds a corrupt document
        return path

    def _write_log_file(self, stem: str) -> str:
        os.makedirs(self.plugin_jobs_dir, exist_ok=True)
        path = os.path.join(self.plugin_jobs_dir, stem + '.log')
        Path(path).write_bytes(b'job log line\n')
        return path

    def _doc(self, phase='downloading', pid=None, heartbeat=None):
        return {
            'schema_version': jobs.JOB_SCHEMA_VERSION,
            'desktop_id': 'fakeapp.desktop',
            'phase': phase,
            'pid': os.getpid() if pid is None else pid,
            'heartbeat': time.time() if heartbeat is None else heartbeat,
        }


class JanitorDocumentTests(_DocSeedMixin, FakeXDGTestCase):
    """sweep_job_documents: the acknowledgement-window rules."""

    def test_fresh_terminal_document_and_log_survive(self):
        fresh = time.time() - (WINDOW // 2)
        self._write_doc_file('fakeapp', self._doc(phase='done',
                                                  heartbeat=fresh))
        self._write_log_file('fakeapp')
        removed = janitor.sweep_job_documents()
        self.assertEqual(removed, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.json')))
        self.assertTrue(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.log')))

    def test_stale_terminal_document_and_log_are_removed(self):
        stale = time.time() - (WINDOW + 60)
        self._write_doc_file('fakeapp', self._doc(phase='done',
                                                  heartbeat=stale))
        self._write_log_file('fakeapp')
        removed = janitor.sweep_job_documents()
        self.assertEqual(removed, 1)
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.json')))
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.log')))

    def test_error_document_ages_out_the_same_way(self):
        stale = time.time() - (WINDOW + 60)
        self._write_doc_file('fakeapp', self._doc(phase='error',
                                                  heartbeat=stale))
        self.assertEqual(janitor.sweep_job_documents(), 1)

    def test_active_document_is_never_touched(self):
        doc = self._doc()  # live pid (this process) + fresh heartbeat
        self._write_doc_file('fakeapp', doc)
        self.assertEqual(janitor.sweep_job_documents(), 0)
        self.assertEqual(jobs.read_job('fakeapp.desktop'), doc)

    def test_live_pid_with_ancient_heartbeat_ages_out(self):
        # liveness needs a FRESH heartbeat: a live pid alone does not
        # make a document active — it reads as interrupted and ages out
        # under the window (rule 3), pinned here for the panel readers
        ancient = self._doc(heartbeat=time.time() - (WINDOW + 60))
        self._write_doc_file('fakeapp', ancient)
        self.assertEqual(janitor.sweep_job_documents(), 1)
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.json')))

    def test_interrupted_document_survives_within_the_window(self):
        # dead pid + fresh heartbeat: reader-classified interrupted, and
        # the document stays so the panel can show it and offer a retry
        doc = self._doc(pid=_dead_pid())
        self._write_doc_file('fakeapp', doc)
        self.assertEqual(janitor.sweep_job_documents(), 0)
        self.assertEqual(jobs.read_job('fakeapp.desktop'), doc)

    def test_interrupted_document_ages_out_after_the_window(self):
        # window semantics: the clock runs from the LAST HEARTBEAT (the
        # dead job's last sign of life), not from the moment the sweep
        # first saw the document
        stale = time.time() - (WINDOW + 60)
        doc = self._doc(pid=_dead_pid(), heartbeat=stale)
        self._write_doc_file('fakeapp', doc)
        self._write_log_file('fakeapp')
        self.assertEqual(janitor.sweep_job_documents(), 1)
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.json')))
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.log')))

    def test_corrupt_document_survives_while_fresh(self):
        # picked rule: a corrupt document has no heartbeat, so it ages
        # out from its file mtime under the same window (pinned)
        path = self._write_doc_file('broken', None)
        self.assertEqual(janitor.sweep_job_documents(), 0)
        self.assertTrue(os.path.exists(path))

    def test_corrupt_document_ages_out_by_mtime(self):
        path = self._write_doc_file('broken', None)
        self._write_log_file('broken')
        old = time.time() - (WINDOW + 60)
        os.utime(path, (old, old))
        self.assertEqual(janitor.sweep_job_documents(), 1)
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'broken.log')))

    def test_document_with_malformed_heartbeat_falls_back_to_mtime(self):
        doc = self._doc(phase='done', heartbeat='garbage')
        path = self._write_doc_file('fakeapp', doc)
        self.assertEqual(janitor.sweep_job_documents(), 0)
        old = time.time() - (WINDOW + 60)
        os.utime(path, (old, old))
        self.assertEqual(janitor.sweep_job_documents(), 1)
        self.assertFalse(os.path.exists(path))

    def test_symlinked_document_is_skipped_entirely(self):
        target = os.path.join(self.sandbox, 'outside.json')
        Path(target).write_text(json.dumps(
            self._doc(phase='done', heartbeat=time.time() - (WINDOW + 60))))
        os.makedirs(self.plugin_jobs_dir, exist_ok=True)
        link = os.path.join(self.plugin_jobs_dir, 'linked.json')
        os.symlink(target, link)
        # never followed, never removed — the writer never produces
        # symlinked documents
        self.assertEqual(janitor.sweep_job_documents(), 0)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.exists(target))


class JanitorTempDirTests(_DocSeedMixin, FakeXDGTestCase):
    """sweep_update_temp_dirs: the disk-only grace-window rule."""

    def setUp(self):
        super().setUp()
        self.area = os.path.join(self.sandbox, 'tmproot')
        os.makedirs(self.area)

    def _orphan(self, name: str, age_seconds: float = None,
                child_content: bytes = None,
                child_age_seconds: float = None) -> str:
        path = os.path.join(self.area, name)
        os.makedirs(path)
        if child_content is not None:
            child = os.path.join(path, 'update.appimage')
            Path(child).write_bytes(child_content)
            if child_age_seconds is not None:
                old = time.time() - child_age_seconds
                os.utime(child, (old, old))
        if age_seconds is not None:
            old = time.time() - age_seconds
            os.utime(path, (old, old))
        return path

    def test_orphan_past_grace_is_removed(self):
        path = self._orphan('appimage-update-old',
                            age_seconds=GRACE + 300,
                            child_content=b'\x7fELFpartial',
                            child_age_seconds=GRACE + 300)
        self.assertEqual(janitor.sweep_update_temp_dirs(temp_area=self.area),
                         1)
        self.assertFalse(os.path.exists(path))

    def test_orphan_within_grace_is_kept(self):
        path = self._orphan('appimage-update-live',
                            age_seconds=GRACE // 10,
                            child_content=b'\x7fELFpartial',
                            child_age_seconds=GRACE // 10)
        janitor.sweep_update_temp_dirs(temp_area=self.area)
        self.assertTrue(os.path.isdir(path))

    def test_orphan_integrate_dir_past_grace_is_removed(self):
        # the panel kills its own --integrate URL child when it closes
        # (panel QML unload → Process destroyed → SIGTERM), which skips
        # cmd_integrate's `finally` rmtree and leaves the truncated
        # download in the temp area with no process behind it
        path = self._orphan(janitor.INTEGRATE_TEMP_PREFIX + 'an9_lb0a',
                            age_seconds=GRACE + 300,
                            child_content=b'\x7fELFpartial',
                            child_age_seconds=GRACE + 300)
        self.assertEqual(janitor.sweep_update_temp_dirs(temp_area=self.area),
                         1)
        self.assertFalse(os.path.exists(path))

    def test_live_integrate_dir_is_kept(self):
        # a running --integrate keeps writing download.appimage, so the
        # child's mtime is fresh even after a long transfer
        path = self._orphan(janitor.INTEGRATE_TEMP_PREFIX + 'live_run',
                            age_seconds=GRACE + 300,
                            child_content=b'\x7fELFpartial',
                            child_age_seconds=2.0)
        janitor.sweep_update_temp_dirs(temp_area=self.area)
        self.assertTrue(os.path.isdir(path))

    def test_integrate_prefix_is_swept(self):
        # cli.py takes the prefix from this constant, so the sweep and
        # the mkdtemp call cannot drift apart
        self.assertIn(janitor.INTEGRATE_TEMP_PREFIX,
                      janitor.UPDATE_TEMP_PREFIXES)

    def test_dir_mtime_stale_but_child_fresh_is_kept(self):
        # pins the EFFECTIVE-mtime rule: net.download_to_file keeps
        # writing one file, so a live download refreshes the file's
        # mtime while the directory's own mtime went stale at entry
        # creation — the dir must survive
        path = self._orphan('appimage-update-downloading',
                            age_seconds=GRACE + 300,
                            child_content=b'\x7fELFpartial',
                            child_age_seconds=1.0)
        janitor.sweep_update_temp_dirs(temp_area=self.area)
        self.assertTrue(os.path.isdir(path))

    def test_gearlever_prefix_is_never_touched(self):
        path = self._orphan('gearlever_appimage_ancient',
                            age_seconds=WINDOW * 10)
        self._orphan('unsquashfs-ancient', age_seconds=WINDOW * 10)
        janitor.sweep_update_temp_dirs(temp_area=self.area)
        self.assertTrue(os.path.isdir(path))
        self.assertTrue(os.path.isdir(
            os.path.join(self.area, 'unsquashfs-ancient')))

    def test_symlinked_entry_is_skipped_and_target_untouched(self):
        target = self._orphan('real-target', age_seconds=1.0)
        link = os.path.join(self.area, 'appimage-update-linked')
        os.symlink(target, link)
        janitor.sweep_update_temp_dirs(temp_area=self.area)
        self.assertTrue(os.path.islink(link))
        self.assertTrue(os.path.isdir(target))

    def test_regular_file_with_prefix_is_skipped(self):
        path = os.path.join(self.area, 'appimage-update-stray')
        Path(path).write_bytes(b'not a dir')
        old = time.time() - (GRACE + 300)
        os.utime(path, (old, old))
        janitor.sweep_update_temp_dirs(temp_area=self.area)
        self.assertTrue(os.path.isfile(path))

    def test_sweep_runs_both_surfaces_and_counts(self):
        stale = time.time() - (WINDOW + 60)
        self._write_doc_file('fakeapp', self._doc(phase='done',
                                                  heartbeat=stale))
        self._write_log_file('fakeapp')
        self._orphan('appimage-update-old', age_seconds=GRACE + 300)
        counts = janitor.sweep(temp_area=self.area)
        self.assertEqual(counts, {'documents_removed': 1,
                                  'temp_dirs_removed': 1})
        self.assertFalse(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'fakeapp.json')))
        self.assertFalse(os.path.exists(
            os.path.join(self.area, 'appimage-update-old')))


class JanitorHookTests(_DocSeedMixin, InProcessCliMixin, FakeXDGTestCase):
    """The two production hooks, through the in-process cmd_* seam."""

    def _seed_stale_terminal(self, stem='staleapp'):
        stale = time.time() - (WINDOW + 60)
        self._write_doc_file(stem, self._doc(phase='done',
                                             heartbeat=stale))
        self._write_log_file(stem)
        return (os.path.join(self.plugin_jobs_dir, stem + '.json'),
                os.path.join(self.plugin_jobs_dir, stem + '.log'))

    def test_list_installed_removes_stale_documents(self):
        doc_path, log_path = self._seed_stale_terminal()
        # the temp-dir half is patched out: hook tests stay hermetic
        with mock.patch.object(janitor, 'sweep_update_temp_dirs',
                               return_value=0) as temp_sweep:
            out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['installed'], [])
        temp_sweep.assert_called_once()
        self.assertFalse(os.path.exists(doc_path))
        self.assertFalse(os.path.exists(log_path))

    def test_list_installed_leaves_fresh_and_active_documents(self):
        self._write_doc_file('freshapp', self._doc(
            phase='done', heartbeat=time.time() - 60))
        active = self._doc()  # live pid + fresh heartbeat
        self._write_doc_file('activeapp', active)
        with mock.patch.object(janitor, 'sweep_update_temp_dirs',
                               return_value=0):
            out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.exists(
            os.path.join(self.plugin_jobs_dir, 'freshapp.json')))
        self.assertEqual(jobs.read_job('activeapp.desktop'), active)

    def test_job_start_in_the_parent_sweeps_before_spawning(self):
        self.install_fake_app()
        doc_path, log_path = self._seed_stale_terminal('staleapp')
        with mock.patch.object(janitor, 'sweep_update_temp_dirs',
                               return_value=0), \
                mock.patch.object(cli, '_spawn_update_job',
                                  return_value=(4242, '/j.json', '/j.log')):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json', '--async'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'job-started')
        self.assertFalse(os.path.exists(doc_path))
        self.assertFalse(os.path.exists(log_path))

    def test_job_start_refusal_keeps_active_document(self):
        self.install_fake_app()
        active = self._doc()
        self._write_doc_file('fakeapp', active)
        with mock.patch.object(janitor, 'sweep_update_temp_dirs',
                               return_value=0):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json', '--async'])
        self.assertEqual(code, 1)
        self.assertIn('already active', json.loads(out)['error'])
        self.assertEqual(jobs.read_job('fakeapp.desktop'), active)


class SweepThrottleTests(_DocSeedMixin, InProcessCliMixin, FakeXDGTestCase):
    """--list-installed runs on the panel's 10 s poll, and a sweep is an
    unbounded os.listdir + stat walk of the shared temp dir in the CLI's
    FOREGROUND — so the listing hook is throttled to one sweep per
    janitor.SWEEP_THROTTLE_SECONDS through a marker file in the plugin
    state dir (never /tmp). A job start still sweeps unconditionally."""

    def test_first_call_sweeps(self):
        with mock.patch.object(janitor, 'sweep') as sweep:
            swept = janitor.sweep_throttled()
        self.assertTrue(swept)
        sweep.assert_called_once()

    def test_second_call_inside_the_window_is_throttled(self):
        with mock.patch.object(janitor, 'sweep') as sweep:
            self.assertTrue(janitor.sweep_throttled(now=1000.0))
            self.assertFalse(janitor.sweep_throttled(now=1000.0 + 60))
        self.assertEqual(sweep.call_count, 1)

    def test_call_after_the_window_sweeps_again(self):
        start = 1000.0
        with mock.patch.object(janitor, 'sweep') as sweep:
            self.assertTrue(janitor.sweep_throttled(now=start))
            self.assertTrue(janitor.sweep_throttled(
                now=start + janitor.SWEEP_THROTTLE_SECONDS + 1))
        self.assertEqual(sweep.call_count, 2)

    def test_missing_marker_sweeps(self):
        # the marker is an optimisation, never a precondition: a fresh
        # sandbox (or a marker removed by hand) must still sweep
        self.assertFalse(os.path.exists(janitor.sweep_marker_path()))
        with mock.patch.object(janitor, 'sweep') as sweep:
            self.assertTrue(janitor.sweep_throttled())
        sweep.assert_called_once()

    def test_corrupt_marker_sweeps(self):
        Path(janitor.sweep_marker_path()).parent.mkdir(
            parents=True, exist_ok=True)
        Path(janitor.sweep_marker_path()).write_text('not-a-timestamp')
        with mock.patch.object(janitor, 'sweep') as sweep:
            self.assertTrue(janitor.sweep_throttled())
        sweep.assert_called_once()
        # …and the sweep re-stamped it, so the marker is healthy again
        self.assertLessEqual(
            abs(float(Path(janitor.sweep_marker_path()).read_text())
                - time.time()), 60)

    def test_marker_lives_in_the_plugin_state_dir(self):
        # never bare in the temp area: the marker must sit inside the
        # plugin's own state dir (namespaced, and not a name the sweep's
        # temp cleanup could ever be asked to remove)
        self.assertEqual(os.path.dirname(janitor.sweep_marker_path()),
                         os.path.dirname(self.plugin_jobs_dir))
        self.assertNotEqual(os.path.dirname(janitor.sweep_marker_path()),
                            os.path.realpath(tempfile.gettempdir()))

    def test_list_installed_sweeps_only_once_per_window(self):
        with mock.patch.object(janitor, 'sweep') as sweep:
            for _ in range(3):
                out, err, code = self.run_cmd(
                    cli.cmd_list_installed, ['--json'])
                self.assertEqual(code, 0, err)
        self.assertEqual(sweep.call_count, 1)

    def test_job_start_sweeps_unconditionally(self):
        # a fresh marker (the panel just listed) must not stop a job start
        # from sweeping: that hook is not throttled
        self.install_fake_app()
        janitor.sweep_throttled()
        with mock.patch.object(janitor, 'sweep') as sweep, \
                mock.patch.object(cli, '_spawn_update_job',
                                  return_value=(4242, '/j.json', '/j.log')):
            out, err, code = self.run_cmd(
                cli.cmd_update, ['fakeapp', '--yes', '--json', '--async'])
        self.assertEqual(code, 0, err)
        sweep.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()

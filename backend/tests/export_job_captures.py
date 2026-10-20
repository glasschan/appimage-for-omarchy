# export_job_captures.py — regenerate the REAL job-document captures the
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
# node parse harness pins (tests-js/captures/job-*.json).
#
# NOT a test module (no test_ prefix): pytest never picks it up. Run it by
# hand whenever the job-document contract changes:
#
#     python3 backend/tests/export_job_captures.py
#
# Provenance of each capture (all documents are produced by the backend's
# own machinery in a FakeXDG sandbox, never hand-typed):
#   job-starting / -downloading / -verifying / -replacing
#       A jobs.JobWriter walked through the exact calls cli._job_entrypoint's
#       pipeline makes (constructor, stage('downloading', manager),
#       progress_cb(0.42), progress_cb(>=0.999), stage('replacing')) — the
#       writer's own atomic write_job produces the bytes on disk.
#   job-done-updated / job-error / job-done-skipped-running
#       cli._job_entrypoint run in-process end to end (the same entrypoint
#       the detached grandchild executes): a loopback fixture server serves
#       a zsync release; the error case fails fail-closed on a SHA-1-less
#       control file, the skipped-running case trips the running guard, the
#       updated case downloads the real nvim fixture when it is available
#       (skipped offline — the JobWriter-driven fallback capture stays).
#   job-stale-heartbeat / job-dead-pid
#       Real downloading documents with ONLY the liveness field edited
#       (heartbeat back-dated 300 s; pid replaced by a reaped child's pid).
#       Legitimate edits: no writer ever produces these states on purpose,
#       and JS cannot probe pids either — the harness passes the verdict in.
#   job-empty / job-truncated / job-malformed
#       Reader-contract fixtures written directly. The backend cannot emit
#       them (every rewrite is atomic), but a reader must survive them.

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import helpers
from helpers import FakeXDGTestCase, FixtureHTTPServer, download_fixture
from test_jobs import _zsync_control

from omarchy_appimage import cli, jobs, net, utils
from omarchy_appimage.ini_config import Config

CAPTURES_DIR = os.path.join(helpers.BACKEND_DIR, '..', 'tests-js', 'captures')

# The download size a real --list-updates reports for the fixture release
# (frozen from the e2e captures; stage() publishes it as total_bytes).
FIXTURE_TOTAL_BYTES = 11442680


def _save_capture(sandbox_doc_path: str, capture_name: str):
    """Copy the document's exact on-disk bytes into the captures dir."""
    os.makedirs(CAPTURES_DIR, exist_ok=True)
    text = Path(sandbox_doc_path).read_text(encoding='utf-8')
    with open(os.path.join(CAPTURES_DIR, capture_name), 'w',
              encoding='utf-8') as f:
        f.write(text)


def _dead_pid() -> int:
    """A pid that is guaranteed not to exist anymore (spawned and reaped)."""
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    return proc.pid


class JobCaptureExportTests(FakeXDGTestCase):
    """Method names are zero-padded on purpose: unittest runs them in
    alphabetical order, and the updated capture depends on the walk (the
    JobWriter-driven fallback must be written before the entrypoint can
    overwrite it with the fully-real document)."""

    def setUp(self):
        super().setUp()
        seam = mock.patch.object(net, '_ALLOW_LOCAL_FOR_TESTS', True)
        seam.start()
        self.addCleanup(seam.stop)
        self.install_fake_app()
        self.doc_path = jobs.job_document_path('fakeapp.desktop')

    def _set_source(self, url):
        el = cli.appimage_provider.list_installed()[0]
        Config.set_app_update_config(el, 'StaticFileUpdater', {'url': url})

    def _run_entrypoint(self):
        return cli._job_entrypoint('fakeapp.desktop', 'fakeapp',
                                   keep_both=False, force=False)

    # ------------------------------------------------------------ 01: walk

    def test_01_writer_phase_walk(self):
        """Active phases + the `updated` fallback + the liveness edits,
        all through jobs.JobWriter's real API (the calls the pipeline's
        on_stage/progress_cb hooks make)."""
        appimage = os.path.join(self.managed_dir, 'fakeapp.appimage')

        class _Manager:
            download_size = FIXTURE_TOTAL_BYTES

        writer = jobs.JobWriter('fakeapp.desktop')
        self.assertEqual(jobs.read_job('fakeapp.desktop')['phase'], 'starting')
        _save_capture(self.doc_path, 'job-starting.json')

        writer.stage('downloading', manager=_Manager())
        writer.progress_cb(0.42)
        _save_capture(self.doc_path, 'job-downloading.json')
        writer.progress_cb(0.9995)
        _save_capture(self.doc_path, 'job-verifying.json')
        writer.stage('replacing')
        _save_capture(self.doc_path, 'job-replacing.json')
        # the terminal rewrite cli._finish_job_document makes for an
        # `updated` verdict (same call, same message format); the fully
        # real entrypoint run in test_04 overwrites this capture when the
        # fixture is downloadable
        writer.finish('updated',
                      message=f'{appimage} was updated successfully',
                      new_version='v0.11.3',
                      downloaded_bytes=FIXTURE_TOTAL_BYTES)
        terminal = jobs.read_job('fakeapp.desktop')
        self.assertEqual(terminal['phase'], 'done')
        _save_capture(self.doc_path, 'job-done-updated.json')

        # liveness edits on an otherwise-real downloading document (see
        # module docstring): stale heartbeat, then a reaped pid. The
        # terminal-only fields reset to their mid-flight values (a real
        # downloading document carries status/new_version/error null).
        mid = dict(terminal)
        mid['phase'] = 'downloading'
        mid['message'] = 'downloading update'
        mid['progress'] = 0.42
        mid['status'] = None
        mid['new_version'] = None
        mid['error'] = None
        mid['downloaded_bytes'] = int(0.42 * mid['total_bytes'])
        mid['heartbeat'] = mid['heartbeat'] - 300
        jobs.write_job('fakeapp.desktop', mid)
        _save_capture(self.doc_path, 'job-stale-heartbeat.json')

        dead = dict(mid)
        dead['heartbeat'] = mid['started']  # fresh relative to capture time
        dead['pid'] = _dead_pid()
        jobs.write_job('fakeapp.desktop', dead)
        _save_capture(self.doc_path, 'job-dead-pid.json')

    # ----------------------------------------------- 02-04: the entrypoint

    def test_02_entrypoint_error(self):
        """A zsync control file without a SHA-1 line fails the download
        fail-closed -> terminal error document (real failure text)."""
        server = FixtureHTTPServer(
            {'/f.appimage.zsync': (200, {}, b'URL: /x\n\nno sha here\n')})
        self.addCleanup(server.close)
        self._set_source(server.url('/f.appimage.zsync'))
        self.assertEqual(self._run_entrypoint(), 1)
        doc = jobs.read_job('fakeapp.desktop')
        self.assertEqual(doc['phase'], 'error')
        self.assertIn('SHA-1', doc['error'])
        _save_capture(self.doc_path, 'job-error.json')

    def test_03_entrypoint_skipped_running(self):
        """The running guard fires before any network traffic: a done
        document with status skipped-running and the real message."""
        self._set_source('http://127.0.0.1:9/f.appimage.zsync')
        mounts = os.path.join(self.sandbox, 'mounts')
        Path(mounts).write_text(
            f'{self.managed_dir}/fakeapp.appimage /tmp/.mount_x '
            'fuse.AppImage rw 0 0\n')
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', mounts):
            self.assertEqual(self._run_entrypoint(), 0)
        doc = jobs.read_job('fakeapp.desktop')
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'skipped-running')
        _save_capture(self.doc_path, 'job-done-skipped-running.json')

    def test_04_entrypoint_updated(self):
        """The full real walk against the loopback server: the nvim fixture
        is downloaded, verified and installed. Offline (no fixture) this
        skips and test_01's JobWriter-driven fallback capture stays."""
        fixture = download_fixture()
        if fixture is None:
            self.skipTest('fixture download unavailable — '
                          'job-done-updated.json stays at the '
                          'JobWriter-driven fallback')
        with open(fixture, 'rb') as f:
            body = f.read()
        server = FixtureHTTPServer({
            '/update.appimage': (200, {}, body),
        })
        server.routes['/f.appimage.zsync'] = (
            200, {}, _zsync_control(server, body))
        self.addCleanup(server.close)
        self._set_source(server.url('/f.appimage.zsync'))
        self.assertEqual(self._run_entrypoint(), 0)
        doc = jobs.read_job('fakeapp.desktop')
        self.assertEqual(doc['phase'], 'done')
        self.assertEqual(doc['status'], 'updated')
        self.assertEqual(doc['new_version'], 'v0.11.3')
        self.assertEqual(doc['downloaded_bytes'], len(body))
        _save_capture(self.doc_path, 'job-done-updated.json')

    # ------------------------------------------------- 05: corrupt fixtures

    def test_05_corrupt_fixtures(self):
        """Corrupt-document fixtures the backend cannot emit (atomic
        writes) but a reader must survive: empty, truncated mid-JSON,
        garbage. A plausible downloading prefix, then cut/garbled."""
        os.makedirs(self.plugin_jobs_dir, exist_ok=True)
        good = ('{"desktop_id": "fakeapp.desktop", "downloaded_bytes": 4668378, '
                '"heartbeat": 1760000000.0, "message": "downloading update", '
                '"phase": "downloading", "pid": 12345, "progress": 0.42, '
                '"schema_version": 1, "started": 1759999900.0, '
                '"total_bytes": 11115280}')
        for name, text in (('job-empty.json', ''),
                           ('job-truncated.json', good[:len(good) // 2]),
                           ('job-malformed.json',
                            'python3: not found\nTraceback (most recent '
                            'call last):\n  File "main.py"\n')):
            with open(self.doc_path, 'w', encoding='utf-8') as f:
                f.write(text)
            _save_capture(self.doc_path, name)
        # sanity: the harness side must reject every one of these
        for name in ('job-empty.json', 'job-truncated.json',
                     'job-malformed.json'):
            with open(os.path.join(CAPTURES_DIR, name),
                      'r', encoding='utf-8') as f:
                with self.assertRaises(ValueError):
                    json.loads(f.read())


if __name__ == '__main__':
    unittest.main(verbosity=2)

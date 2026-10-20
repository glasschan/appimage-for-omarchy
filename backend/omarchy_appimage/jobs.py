# jobs.py — background update-job documents (stdlib only).
#
# One document per app at $XDG_STATE_HOME/io.github.glasschan.appimage/
# jobs/<desktop id>.json, written by the detached --update --async runner
# (cli._job_entrypoint) and read by any CLI invocation. Every rewrite is
# atomic (utils.atomic_write: temp file + os.replace), so a reader always
# sees a complete document.
#
# `interrupted` is NEVER written by anyone: it is a reader-side
# classification (classify_job) for a non-terminal document whose process
# is gone or whose heartbeat went stale. Phases a writer sets:
#   starting | downloading | verifying | replacing | done | error
# Terminal phases are `done` and `error` only.

import json
import logging
import os
import re
import threading
import time

from . import constants
from .utils import atomic_write

JOB_SCHEMA_VERSION = 1

# Non-terminal phases a writer sets. `verifying` is best-effort: it is
# entered from the download progress callback reaching the top fraction,
# so sources that send no Content-Length never show it.
ACTIVE_PHASES = ('starting', 'downloading', 'verifying', 'replacing')
TERMINAL_PHASES = ('done', 'error')

# Liveness rule (CONTRACT.md): a job is ACTIVE iff its phase is
# non-terminal AND its pid is alive AND its heartbeat is fresher than
# this threshold. 120 s sits comfortably above the downloader's 30 s
# per-read stall guard, so a slow-but-alive download is never
# misclassified as interrupted.
JOB_STALE_AFTER_SECONDS = 120

# The grandchild refreshes the heartbeat at least this often (progress
# callbacks rewrite the document too; this thread covers the phases no
# progress callback fires in — verification, replace, long stalls).
JOB_HEARTBEAT_INTERVAL_SECONDS = 2.0

# Progress fraction at or above this means the response body is fully
# received and the source's digest verification is running.
JOB_VERIFY_FRACTION = 0.999

# The per-job log is truncated on open when it has grown past this.
JOB_LOG_MAX_BYTES = 256 * 1024

# Desktop ids are basenames of .desktop files; the job file name must
# stay a single flat path component (no separators, no traversal).
_JOB_STEM_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]*$')


def jobs_dir() -> str:
    return os.path.join(constants.plugin_state_dir(), 'jobs')


def _job_stem(desktop_id) -> str:
    if not isinstance(desktop_id, str):
        raise ValueError(f'not a usable desktop id for a job file: '
                         f'{desktop_id!r}')
    stem = desktop_id[:-8] if desktop_id.endswith('.desktop') else desktop_id
    if not _JOB_STEM_RE.match(stem or ''):
        raise ValueError(f'not a usable desktop id for a job file: '
                         f'{desktop_id!r}')
    return stem


def job_document_path(desktop_id: str) -> str:
    return os.path.join(jobs_dir(), _job_stem(desktop_id) + '.json')


def job_log_path(desktop_id: str) -> str:
    return os.path.join(jobs_dir(), _job_stem(desktop_id) + '.log')


def read_job(desktop_id: str):
    """The stored job document, or None when there is none. A corrupt
    document reads as None (the runner's next rewrite heals it)."""
    try:
        with open(job_document_path(desktop_id), 'r',
                  encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_job(desktop_id: str, doc: dict):
    """Atomically (re)write the job document (temp file + os.replace)."""
    atomic_write(job_document_path(desktop_id),
                 json.dumps(doc, indent=2, ensure_ascii=False,
                            sort_keys=True).encode('utf-8'))


def pid_alive(pid) -> bool:
    """True when `pid` names a live process (signal 0 probe). A pid we
    may not signal belongs to another user — it is alive."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def classify_job(doc, now: float = None) -> str:
    """Reader-side classification of a job document:
    'done' | 'error' for terminal phases, else 'active' when the phase is
    non-terminal AND the pid is alive AND the heartbeat is fresh, and
    'interrupted' for every other non-terminal document (dead pid or
    stale heartbeat — 'interrupted' is derived, never written)."""
    if not isinstance(doc, dict):
        return 'interrupted'
    phase = doc.get('phase')
    if phase in TERMINAL_PHASES:
        return phase
    if now is None:
        now = time.time()
    heartbeat = doc.get('heartbeat') or 0
    try:
        fresh = (now - float(heartbeat)) <= JOB_STALE_AFTER_SECONDS
    except (TypeError, ValueError):
        fresh = False
    if fresh and pid_alive(doc.get('pid')):
        return 'active'
    return 'interrupted'


def active_job(desktop_id: str):
    """The document when an ACTIVE job exists for the app, else None."""
    doc = read_job(desktop_id)
    if doc is not None and classify_job(doc) == 'active':
        return doc
    return None


def open_job_log_fd(desktop_id: str) -> int:
    """Writable (append-mode) fd for the per-job log, living next to the
    job document. Size-capped: an existing log over JOB_LOG_MAX_BYTES is
    truncated on open, so a log can never grow unboundedly across job
    runs. The caller owns the fd (the detached runner dup2s it onto
    stdout/stderr; the spawning parent closes its copy)."""
    path = job_log_path(desktop_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        if os.path.getsize(path) > JOB_LOG_MAX_BYTES:
            with open(path, 'w'):
                pass
    except OSError as e:
        logging.debug('could not truncate job log %s: %s', path, e)
    return os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)


class JobWriter:
    """Grandchild-side writer: owns one job document through its phases.

    All mutations funnel through _write() (heartbeat refresh + atomic
    rewrite) under a lock, because the heartbeat thread rewrites the
    document while the pipeline thread moves it through phases."""

    def __init__(self, desktop_id: str, pid: int = None):
        self.desktop_id = desktop_id
        self.pid = os.getpid() if pid is None else pid
        self._lock = threading.Lock()
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        now = time.time()
        self._doc = {
            'schema_version': JOB_SCHEMA_VERSION,
            'desktop_id': desktop_id,
            'phase': 'starting',
            'progress': 0.0,
            'total_bytes': None,
            'downloaded_bytes': 0,
            'pid': self.pid,
            'started': now,
            'heartbeat': now,
            'message': 'starting',
            'status': None,
            'new_version': None,
            'error': None,
        }
        self._write()

    # ------------------------------------------------------------- writing

    def _write(self):
        with self._lock:
            self._doc['heartbeat'] = time.time()
            write_job(self.desktop_id, dict(self._doc))

    def document(self) -> dict:
        with self._lock:
            return dict(self._doc)

    # ------------------------------------------------- run_update hooks

    def stage(self, stage: str, manager=None):
        """Phase transition reported by cli.run_update's on_stage hook."""
        with self._lock:
            self._doc['phase'] = stage
            if stage == 'downloading':
                self._doc['message'] = 'downloading update'
                size = getattr(manager, 'download_size', None) \
                    if manager is not None else None
                if size:
                    # declared at the availability check — for .zsync
                    # sources that is the CONTROL FILE's size, so
                    # mid-download byte counts are approximate; the
                    # terminal rewrite carries the exact downloaded bytes
                    self._doc['total_bytes'] = int(size)
            elif stage == 'replacing':
                self._doc['message'] = 'installing the downloaded release'
                self._doc['progress'] = 1.0
        self._write()

    def progress_cb(self, fraction: float):
        """Download progress: fraction in [0, 1] from net.download_to_file.
        Every rewrite doubles as a heartbeat; the top of the transfer
        moves the phase to `verifying` (digest check runs next)."""
        fraction = min(float(fraction), 1.0)
        with self._lock:
            if fraction >= JOB_VERIFY_FRACTION:
                self._doc['phase'] = 'verifying'
                self._doc['message'] = 'verifying the downloaded release'
            else:
                self._doc['phase'] = 'downloading'
                self._doc['message'] = 'downloading update'
            self._doc['progress'] = fraction
            total = self._doc.get('total_bytes')
            if total:
                self._doc['downloaded_bytes'] = int(round(fraction * total))
        self._write()

    def finish(self, status: str, message: str, new_version: str = None,
               downloaded_bytes: int = None, error: str = None):
        """Terminal rewrite: phase `done` (status updated / up-to-date /
        skipped-running) or `error` (status error, text in `error`)."""
        phase = 'error' if status == 'error' else 'done'
        with self._lock:
            self._doc['phase'] = phase
            self._doc['status'] = status
            self._doc['message'] = message
            if new_version is not None:
                self._doc['new_version'] = new_version
            if downloaded_bytes is not None:
                self._doc['downloaded_bytes'] = int(downloaded_bytes)
            if error is not None:
                self._doc['error'] = error
            if phase == 'done':
                self._doc['progress'] = 1.0
        self._write()

    def finish_error(self, message: str):
        self.finish('error', message, error=message)

    # ---------------------------------------------------------- heartbeat

    def start_heartbeat(self):
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True,
            name='appimage-job-heartbeat')
        self._heartbeat_thread.start()

    def _heartbeat_loop(self):
        while not self._heartbeat_stop.wait(
                JOB_HEARTBEAT_INTERVAL_SECONDS):
            try:
                self._write()
            except OSError:
                # state dir vanished mid-job: keep the job running, the
                # document just stops refreshing (reads as interrupted)
                return

    def stop_heartbeat(self):
        if self._heartbeat_stop is not None:
            self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None

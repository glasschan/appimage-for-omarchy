# pick_file.py — crash-isolated native file dialog for --pick-file.
#
# Test suite written for this plugin (spec #14 / ticket #16).
#
# The backend never dialogs in-process: it ORCHESTRATES a dialog that
# lives in child processes, so a crashed or hung dialog can never take
# down the caller (the panel's backend process). Probe order:
#
#   1. xdg-desktop-portal FileChooser, driven through the glib DBus CLI
#      (`gdbus`): a `gdbus monitor` is started on the portal first, then
#      FileChooser.OpenFile is called — the method reply only carries the
#      request handle, the actual answer arrives as the Request.Response
#      signal on the monitored stream (monitor-then-call).
#   2. `zenity --file-selection`.
#
# First available wins; neither present -> a typed "unavailable" result
# (the panel keeps today's path-field UX). The whole operation runs under
# one absolute deadline (net.py's whole-operation budget discipline): a
# stuck dialog yields a typed timeout error, never a hang. Child stdout
# stays bounded (run_command's 10 MiB/stream cap; monitor lines are
# additionally capped per line). Subprocess argv is fully constant — the
# dialog title is not user data, and the picked path is returned as JSON
# data, never spliced into a command line. No shell is ever involved.

import logging
import os
import re
import select
import shutil
import subprocess
import time
from urllib.parse import unquote, urlsplit

from . import constants
from .utils import run_command

# Whole-operation wall-clock budget for the dialog: long enough for a
# human to browse, short enough that a stuck dialog can never hang the
# panel's backend process forever.
DEFAULT_DEADLINE_SECONDS = 600

# A single monitor line larger than this is dropped (bounded memory); the
# real Response payload is a few hundred bytes.
MAX_MONITOR_LINE_BYTES = 1 << 20  # 1 MiB

# gdbus monitor chunk size / monitor line decode guard
_MONITOR_CHUNK = 1 << 16

PORTAL_DEST = 'org.freedesktop.portal.Desktop'
PORTAL_PATH = '/org/freedesktop/portal/desktop'
PORTAL_IFACE = 'org.freedesktop.portal.FileChooser'
REQUEST_IFACE = 'org.freedesktop.portal.Request'

# handle_token pins the last element of the request object path, so the
# Response signal line can be matched to OUR request even while other
# applications are making their own portal requests on the same bus.
_HANDLE_TOKEN = 'omarchypick'
_DIALOG_TITLE = 'Open AppImage'

# Single file ('multiple': <false>), AppImage-filtered. Constant GVariant
# text — nothing user-controlled is ever interpolated.
_PORTAL_OPTIONS = (
    "{'handle_token': <'%s'>, 'multiple': <false>, "
    "'filters': <[('AppImage', [(0, '*.AppImage'), (0, '*.appimage')])], "
    "'current_filter': <('AppImage', [(0, '*.AppImage'), "
    "(0, '*.appimage')])>}" % _HANDLE_TOKEN)

PORTAL_DIALOG_ARGV = [
    'gdbus', 'call', '--session', '--dest', PORTAL_DEST,
    '--object-path', PORTAL_PATH,
    '--method', PORTAL_IFACE + '.OpenFile',
    '', _DIALOG_TITLE, _PORTAL_OPTIONS,
]

PORTAL_MONITOR_ARGV = ['gdbus', 'monitor', '--session', '--dest', PORTAL_DEST]

PORTAL_PROBE_ARGV = ['gdbus', 'introspect', '--session', '--dest', PORTAL_DEST,
                     '--object-path', PORTAL_PATH]

# zenity --file-selection is single-file unless --multiple is passed.
ZENITY_ARGV = ['zenity', '--file-selection', '--title', _DIALOG_TITLE,
               '--file-filter', 'AppImage | *.AppImage *.appimage']

UNAVAILABLE_MESSAGE = ('no file chooser is available (neither the desktop '
                       'portal nor zenity); type or paste the path instead')


class PickError(Exception):
    """Typed pick failure; `reason` distinguishes timeout / backend-failed
    / not-local for the panel."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class PickCancelled(Exception):
    """The user cancelled the dialog (no fallback: reopening a second
    dialog after an explicit cancel is never wanted)."""


def _which(name: str):
    """Seam over shutil.which (tests patch this module attribute)."""
    return shutil.which(name)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PickError('timeout',
                        'the file dialog did not answer in time '
                        f'({constants.APP_NAME} gave up after its whole-'
                        'operation deadline)')
    return remaining


def _result(schema_extra: dict) -> dict:
    return {'schema_version': constants.JSON_SCHEMA_VERSION, **schema_extra}


def _picked(path: str, backend: str) -> dict:
    return _result({'result': 'picked', 'path': path, 'backend': backend})


def _cancelled(backend: str) -> dict:
    return _result({'result': 'cancelled', 'backend': backend})


def _unavailable() -> dict:
    return _result({'result': 'unavailable', 'message': UNAVAILABLE_MESSAGE})


def _error(reason: str, message: str) -> dict:
    return _result({'result': 'error', 'reason': reason, 'error': message})


# ------------------------------------------------------------ portal probe

def portal_available(deadline: float) -> bool:
    """True when gdbus exists AND the portal object carries the
    FileChooser interface (introspection is side-effect free — probing
    must never open a dialog)."""
    if not _which('gdbus'):
        return False
    try:
        result = run_command(PORTAL_PROBE_ARGV, check=False,
                             timeout=_remaining(deadline))
    except (OSError, subprocess.TimeoutExpired) as e:
        logging.debug('portal probe failed: %s', e)
        return False
    return (result.returncode == 0
            and PORTAL_IFACE in result.stdout.decode(errors='replace'))


def zenity_available() -> bool:
    return _which('zenity') is not None


# ----------------------------------------------------------- monitor session

class _MonitorSession:
    """A running `gdbus monitor` child: line reads bounded by the
    whole-operation deadline (select) and per-line size cap.

    The child runs in its own session (start_new_session) and is always
    reaped by close() — killpg takes the whole group so no grandchild
    survives, mirroring utils.run_command's process-group discipline."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._fd = proc.stdout.fileno()
        self._buf = bytearray()
        self._overlong = False

    def readline_deadline(self, deadline: float):
        """Next line without the newline (UTF-8), or None at EOF. Raises
        PickError('timeout') when the deadline expires first."""
        while True:
            nl = self._buf.find(b'\n')
            if nl >= 0:
                line = bytes(self._buf[:nl])
                del self._buf[:nl + 1]
                if self._overlong:
                    self._overlong = False
                    logging.warning('dropped an over-long monitor line')
                    continue
                return line.decode('utf-8', errors='replace')
            if len(self._buf) > MAX_MONITOR_LINE_BYTES:
                # keep draining (the child must never block on a full
                # pipe) but stop accumulating this line
                self._buf.clear()
                self._overlong = True

            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise PickError('timeout',
                                'the file dialog did not answer in time')
            ready, _, _ = select.select([self._fd], [], [], timeout)
            if not ready:
                continue  # loop re-checks the deadline
            chunk = os.read(self._fd, _MONITOR_CHUNK)
            if not chunk:
                if self._buf and not self._overlong:
                    line = bytes(self._buf)
                    self._buf.clear()
                    return line.decode('utf-8', errors='replace')
                return None
            self._buf += chunk

    def close(self):
        try:
            # the child runs in its own session: kill the whole group so
            # no grandchild survives
            os.killpg(os.getpgid(self._proc.pid), 9)
        except (ProcessLookupError, PermissionError, OSError, TypeError,
                ValueError):
            try:
                self._proc.kill()
            except OSError:
                pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for stream in (self._proc.stdout, self._proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


def _spawn_monitor(argv: list) -> _MonitorSession:
    """Seam over subprocess.Popen (tests patch this module attribute)."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    return _MonitorSession(proc)


# ----------------------------------------------------------------- parsing

_HANDLE_RE = re.compile(r"objectpath '([^']+)'")
_RESPONSE_CODE_RE = re.compile(r'\(uint32\s+(\d+)')
_URIS_RE = re.compile(r"'uris':\s*<\[(.*?)\]>")
_QUOTED_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")
_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', "'": "'", '"': '"', '\\': '\\'}


def _parse_handle(reply_stdout: bytes):
    """The request object path from the `gdbus call` reply, e.g.
    (objectpath '/org/freedesktop/portal/desktop/request/1_42/tok',)."""
    match = _HANDLE_RE.search(reply_stdout.decode(errors='replace'))
    return match.group(1) if match else None


def _parse_response(line: str):
    """(code, uris) from a Request.Response signal line."""
    code_match = _RESPONSE_CODE_RE.search(line)
    uris_match = _URIS_RE.search(line)
    code = int(code_match.group(1)) if code_match else -1
    uris = []
    if uris_match:
        for quoted in _QUOTED_RE.findall(uris_match.group(1)):
            uris.append(re.sub(r'\\(.)',
                               lambda m: _ESCAPES.get(m.group(1),
                                                      m.group(1)), quoted))
    return code, uris


def _uri_to_path(uri: str) -> str:
    """A picked file:// URI -> local absolute plain path (percent-decoded).
    Anything else (other scheme, remote host) is rejected: the result is
    only ever a path the local panel can integrate."""
    parts = urlsplit(uri)
    if parts.scheme != 'file':
        raise PickError('not-local',
                        f'the file dialog returned a non-local address '
                        f'({uri!r}); only local file:// selections are '
                        'supported')
    if parts.netloc not in ('', 'localhost'):
        raise PickError('not-local',
                        f'the file dialog returned a remote share '
                        f'({uri!r}); only local paths are supported')
    path = unquote(parts.path)
    if not os.path.isabs(path):
        raise PickError('not-local',
                        f'the file dialog returned a non-absolute path '
                        f'({uri!r})')
    return path


# -------------------------------------------------------------- backends

def _pick_via_portal(deadline: float) -> str:
    """Portal route: monitor-then-call. Returns the picked local path."""
    _remaining(deadline)
    # the monitor MUST be running before the call, or the async Response
    # signal (the only real answer — the method reply is just a handle)
    # could slip past unread
    monitor = _spawn_monitor(PORTAL_MONITOR_ARGV)
    try:
        try:
            reply = run_command(PORTAL_DIALOG_ARGV, check=False,
                                timeout=_remaining(deadline))
        except (OSError, subprocess.TimeoutExpired) as e:
            reason = 'timeout' if isinstance(e, subprocess.TimeoutExpired) \
                else 'backend-failed'
            raise PickError(reason, f'the portal file chooser failed: {e}')
        if reply.returncode != 0:
            raise PickError(
                'backend-failed',
                'the portal file chooser failed (gdbus exit '
                f'{reply.returncode}: '
                f'{reply.stderr.decode(errors="replace").strip()[:200]})')

        handle = _parse_handle(reply.stdout)
        if not handle:
            raise PickError('backend-failed',
                            'could not parse the portal request handle')

        while True:
            line = monitor.readline_deadline(deadline)
            if line is None:
                raise PickError(
                    'backend-failed',
                    'the file dialog ended without answering '
                    '(the portal monitor stream closed)')
            if not line.startswith(handle + ':'):
                continue  # unrelated traffic from other applications
            if REQUEST_IFACE + '.Response' not in line:
                continue
            code, uris = _parse_response(line)
            if code == 1:
                raise PickCancelled()
            if code != 0:
                raise PickError('backend-failed',
                                f'the file dialog failed (portal response '
                                f'code {code})')
            if not uris:
                raise PickError('backend-failed',
                                'the file dialog returned no file')
            if len(uris) > 1:
                logging.warning('the single-file dialog returned %d files; '
                                'using the first', len(uris))
            return _uri_to_path(uris[0])
    finally:
        monitor.close()


def _pick_via_zenity(deadline: float) -> str:
    """zenity route: the dialog IS the child process; exit 1 = cancel."""
    _remaining(deadline)
    try:
        result = run_command(ZENITY_ARGV, check=False,
                             timeout=_remaining(deadline))
    except (OSError, subprocess.TimeoutExpired) as e:
        reason = 'timeout' if isinstance(e, subprocess.TimeoutExpired) \
            else 'backend-failed'
        raise PickError(reason, f'the zenity file dialog failed: {e}')
    if result.returncode == 1:
        raise PickCancelled()
    if result.returncode != 0:
        raise PickError('backend-failed',
                        f'the zenity file dialog failed (exit '
                        f'{result.returncode}: '
                        f'{result.stderr.decode(errors="replace").strip()[:200]})')
    path = result.stdout.decode(errors='replace').strip()
    if not path:
        raise PickError('backend-failed',
                        'the zenity file dialog returned no file')
    if not os.path.isabs(path):
        raise PickError('backend-failed',
                        f'the zenity file dialog returned a non-absolute '
                        f'path ({path!r})')
    return path


# ----------------------------------------------------------------- entry

def _run_pick(deadline: float) -> dict:
    """Probe order: portal first, zenity fallback, then typed unavailable.
    A portal ERROR falls through to zenity (a broken portal must not cost
    the user the feature); an explicit CANCEL never does."""
    errors = []
    if portal_available(deadline):
        try:
            return _picked(_pick_via_portal(deadline), 'portal')
        except PickCancelled:
            return _cancelled('portal')
        except PickError as e:
            logging.warning('portal file chooser failed: %s', e)
            errors.append(e)

    if zenity_available():
        try:
            return _picked(_pick_via_zenity(deadline), 'zenity')
        except PickCancelled:
            return _cancelled('zenity')
        except PickError as e:
            logging.warning('zenity file dialog failed: %s', e)
            errors.append(e)

    if errors:
        raise errors[-1]  # the last diagnosis wins; logged above
    return _unavailable()


def run_pick(deadline_seconds: float = None) -> dict:
    """Run one bounded file-pick operation; always returns a typed result
    payload (picked / cancelled / unavailable / error), never raises and
    never hangs."""
    deadline = time.monotonic() + (DEFAULT_DEADLINE_SECONDS
                                   if deadline_seconds is None
                                   else float(deadline_seconds))
    try:
        return _run_pick(deadline)
    except PickCancelled:
        # defensive: a cancel must always be reported, never leak as error
        return _cancelled('unknown')
    except PickError as e:
        return _error(e.reason, str(e))

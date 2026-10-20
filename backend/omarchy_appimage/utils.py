# utils.py — small helpers shared by the backend (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# extract_terminal_arguments, remove_special_chars and get_file_hash are
# ported from src/lib/utils.py; Gio/Gtk/dbus helpers are dropped and the
# subprocess helpers always use argument lists (never `sh -c`).

import glob
import hashlib
import logging
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading

# Producer-side output bound (marketplace review): a chatty child can
# never make the backend buffer more than this per stream. Past the cap
# the reader stops draining the pipe, the child blocks on write() and
# the timeout reaps it — so memory stays bounded without killing well-
# behaved children early.
MAX_OUTPUT_BYTES = 10 * 1024 * 1024


def get_file_hash(file_path: str, alg: str = 'md5') -> str:
    h = hashlib.new(alg)
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def remove_special_chars(filename: str, replacement: str = '') -> str:
    """Removes special characters from a filename (GearLever, utils.py)."""
    pattern = r'[^\w\._]+'
    return re.sub(pattern, replacement, filename)


def extract_terminal_arguments(command: str) -> dict:
    """Extract env variables, executable and arguments from a command
    string (GearLever, utils.py). Handles quoted paths and flags."""
    tokens = shlex.split(command)

    result = {
        'env_vars': [],
        'executable': '',
        'arguments': []
    }

    for token in tokens:
        if token == 'env':
            continue
        elif '=' in token and not token.startswith('/') and not token.startswith('-'):
            result['env_vars'].append(token)
        elif not result['executable'] and not token.startswith('-'):
            result['executable'] = token
        else:
            result['arguments'].append(token)

    return result


def run_command(command: list, cwd: str = None, check: bool = True,
                timeout: float = 120, stdin=None,
                max_output_bytes: int = MAX_OUTPUT_BYTES
                ) -> subprocess.CompletedProcess:
    """Run a command with argument lists only (no shell), stdout/stderr
    captured; raises CalledProcessError on failure when check=True.

    Each output stream is capped at `max_output_bytes`: only the first
    cap bytes are retained, while the pipe keeps being drained (and the
    excess discarded) so a chatty child never deadlocks on a full pipe,
    whatever the pipe size. Unbounded producers are reaped by `timeout`.
    `stdin` may be an open file object (bsdtar reads the squashfs stream
    from one)."""
    logging.debug('Running %s', command)
    proc = subprocess.Popen(command, cwd=cwd, stdin=stdin,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)

    def _drain(stream) -> bytes:
        # Keep reading until EOF, but retain only the first
        # max_output_bytes. Reading past the cap without storing the
        # excess is what bounds memory: a chatty child is drained (so it
        # never deadlocks on a full pipe, whatever the pipe size) and
        # simply runs until it finishes or the timeout reaps it.
        kept = bytearray()
        scratch = bytearray(1 << 16)
        while True:
            n = stream.readinto(scratch)
            if not n:
                break
            if len(kept) < max_output_bytes:
                kept += scratch[:min(n, max_output_bytes - len(kept))]
        return bytes(kept)

    holders = {'stdout': [], 'stderr': []}

    def _drain_to(stream, key):
        holders[key].append(_drain(stream))

    threads = [
        threading.Thread(target=_drain_to, args=(proc.stdout, 'stdout')),
        threading.Thread(target=_drain_to, args=(proc.stderr, 'stderr')),
    ]
    for t in threads:
        t.daemon = True
        t.start()

    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            # the child runs in its own session: kill the whole group so
            # no grandchild survives
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait()
        raise
    finally:
        for t in threads:
            t.join(timeout=5)
        # the readers stop at the cap (the child then blocks on its full
        # pipe until the timeout reaps it); the streams are only closed
        # once the process is gone
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass

    result = subprocess.CompletedProcess(
        command, proc.returncode,
        stdout=(holders['stdout'][0] if holders['stdout'] else b''),
        stderr=(holders['stderr'][0] if holders['stderr'] else b''))
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, command,
            output=result.stdout, stderr=result.stderr)
    return result


def atomic_write(path: str, data: bytes):
    """Write `data` to `path` atomically (temp file + os.replace), so a
    crash can never leave a truncated .desktop file behind."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix='.tmp-', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # mkstemp creates 0600; desktop files must stay world-readable
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def is_app_running(file_path: str,
                   mounts_path: str = None) -> bool:
    """True when the executable at `file_path` is currently running
    (ported from GearLever's AppImageProvider.is_app_running).

    Two signals are OR-ed, because a type-2 AppImage started through its
    FUSE runtime executes from /tmp/.mount_* so its `ps` exe never equals
    the AppImage path:
      1. a `ps -eo exe` entry equals `file_path` (direct execution);
      2. `file_path` is the mount source of an active FUSE mount
         (see is_fuse_mounted).

    `mounts_path` overrides the /proc/mounts location (tests)."""
    return (_running_by_ps(file_path)
            or is_fuse_mounted(file_path, mounts_path))


def _running_by_ps(file_path: str) -> bool:
    """The original GearLever detection: exact match in `ps -eo exe`."""
    if not file_path:
        return False

    try:
        result = run_command(['ps', '-eo', 'exe'], check=True)
    except Exception as e:
        logging.warning('ps lookup failed: %s', e)
        return False

    for line in result.stdout.decode(errors='replace').split('\n'):
        if line.strip() == file_path:
            return True

    return False


PROC_MOUNTS_PATH = '/proc/mounts'


def _unescape_mounts_field(field: str) -> str:
    """Decode the octal escapes used by /proc/mounts (mounts(5): space is
    \\040, tab \\011, newline \\012, backslash \\134, ...)."""
    def repl(match):
        return chr(int(match.group(1), 8))
    return re.sub(r'\\([0-7]{3})', repl, field)


def parse_proc_mounts(path: str = None) -> list:
    """Parse a mounts(5) style file into (source, target, fstype) tuples.

    A missing or unreadable file yields an empty list (best-effort, like
    the `ps` lookup)."""
    if path is None:
        path = PROC_MOUNTS_PATH

    entries = []
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                fields = line.split()
                if len(fields) < 3:
                    continue
                entries.append((_unescape_mounts_field(fields[0]),
                                _unescape_mounts_field(fields[1]),
                                fields[2]))
    except OSError as e:
        logging.debug('could not read %s: %s', path, e)
    return entries


def is_fuse_mounted(file_path: str, mounts_path: str = None) -> bool:
    """True when `file_path` is the source of an active FUSE mount.

    While a type-2 AppImage runs, its runtime FUSE-mounts the image at
    /tmp/.mount_XXXXXX, e.g.

        escrcpy.appimage /tmp/.mount_escrcpPC9VbJ fuse.escrcpy.appimage rw,... 0 0

    Note the source: /proc/mounts only ever holds the AppImage's BASENAME
    there, never the absolute path, so the attribution lives in
    procs.mount_points_for (which also keeps the fuse.* filter that stops
    an unrelated regular-file source — a loop device, a bind mount — from
    matching). `mounts_path` overrides the /proc/mounts location (tests)."""
    if not file_path:
        return False

    # Imported here, not at module scope: procs imports run_command and
    # parse_proc_mounts from this module, so a top-level import either way
    # would be a cycle.
    from . import procs

    return bool(procs.mount_points_for(file_path, mounts_path))


def gnu_naturalsize(value: int, precision: int = 1) -> str:
    """Format a byte count like GNU `ls -lh` (GearLever, utils.py)."""
    if value < 0:
        return f"-{gnu_naturalsize(abs(value), precision)}"

    suffixes = ('B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB', 'ZB', 'YB')
    base = 1024.0

    if value < base:
        return f"{value}B"

    import math
    i = int(math.floor(math.log(value, base)))
    if i >= len(suffixes):
        i = len(suffixes) - 1

    v = value / math.pow(base, i)
    return f"{v:.{precision}f} {suffixes[i]}"


# ------------------------------------------------------------ fuse2 probe
#
# The fuse2 runtime probe (spec #14, ticket #17): type-2 AppImages need
# libfuse.so.2 to launch; when it is absent the panel's launch-failure
# banner can name the likely cause instead of a generic error. The probe
# is best-effort only — it NEVER raises and NEVER fails a caller; a state
# it cannot decide is reported as 'unknown'.

FUSE2_SONAME = 'libfuse.so.2'

# `ldconfig -p` prints the dynamic-linker cache as one
# "<soname> (<abi>) => <path>" line per entry; a missing binary, a
# non-zero exit or a timeout only degrades the probe to the directory
# globs below — never an error.
FUSE2_CACHE_COMMAND = ['ldconfig', '-p']

# Standard library directories: the multiarch dir, lib64 and lib under
# the usual roots, scanned with the glob FUSE2_SONAME + '*'.
_FUSE2_LIB_SUBDIRS = ('lib/x86_64-linux-gnu', 'lib64', 'lib')
FUSE2_LIB_DIRS = tuple(root + sub
                       for root in ('/', '/usr/', '/usr/local/')
                       for sub in _FUSE2_LIB_SUBDIRS)

FUSE2_PRESENT = 'present'
FUSE2_MISSING = 'missing'
FUSE2_UNKNOWN = 'unknown'


def _cache_has_fuse2(cache_output: str) -> bool:
    """True when the ldcache listing carries a libfuse.so.2 entry.

    Match on the entry's first token (the soname column): the summary
    line and near-miss sonames (libfuse3.so.3, libfuse.so.20) never
    match."""
    for line in cache_output.splitlines():
        tokens = line.split()
        if tokens and tokens[0] == FUSE2_SONAME:
            return True
    return False


def _fuse2_in_lib_dirs(lib_dirs) -> bool:
    """True when any standard library directory holds the soname file
    (libfuse.so.2) or a real-file name behind it (libfuse.so.2.9.9).
    Absent directories are the normal case — glob just finds nothing."""
    for directory in lib_dirs:
        if glob.glob(os.path.join(directory, FUSE2_SONAME + '*')):
            return True
    return False


def probe_fuse2(cache_output: str = None, cache_command: list = None,
                lib_dirs=None) -> str:
    """Best-effort fuse2 runtime check: FUSE2_PRESENT, FUSE2_MISSING or
    FUSE2_UNKNOWN. One linker-cache read plus a few globs, once per
    caller (the installed-list envelope probes once per call, never per
    app).

    Signal order:
      1. the dynamic-linker cache (`ldconfig -p`, best-effort): a
         libfuse.so.2 entry is decisive ('present');
      2. otherwise the standard library directories are globbed — a hit
         is decisive too ('present', even when the cache phase failed);
         no hit after a *successful* cache read is 'missing'.

    The cache phase failing (binary absent, non-zero exit, timeout) is
    not an error: the globs still run and, without a glob hit, the
    verdict is 'unknown' (the primary evidence source was unavailable).
    The function NEVER raises; any unexpected failure is logged and
    reported as 'unknown'.

    Tests pin behaviour with fakes (the host's real fuse2 state is never
    depended on): `cache_output` stands in for the `ldconfig -p` stdout
    (the command is then not run), `cache_command` replaces the command
    (point it at something failing to fake an unavailable cache),
    `lib_dirs` replaces FUSE2_LIB_DIRS."""
    try:
        if cache_output is None:
            try:
                result = run_command(cache_command or FUSE2_CACHE_COMMAND,
                                     check=False, timeout=10)
            except Exception as e:
                logging.debug('fuse2 probe: linker cache unavailable: %s', e)
                result = None
            if result is not None and result.returncode == 0:
                cache_output = result.stdout.decode(errors='replace')

        cache_phase_ok = cache_output is not None
        if cache_phase_ok and _cache_has_fuse2(cache_output):
            return FUSE2_PRESENT

        if _fuse2_in_lib_dirs(lib_dirs or FUSE2_LIB_DIRS):
            return FUSE2_PRESENT
        return FUSE2_MISSING if cache_phase_ok else FUSE2_UNKNOWN
    except Exception:
        logging.exception('fuse2 probe failed')
        return FUSE2_UNKNOWN

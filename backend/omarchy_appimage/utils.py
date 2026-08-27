# utils.py — small helpers shared by the backend (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# extract_terminal_arguments, remove_special_chars and get_file_hash are
# ported from src/lib/utils.py; Gio/Gtk/dbus helpers are dropped and the
# subprocess helpers always use argument lists (never `sh -c`).

import hashlib
import logging
import os
import re
import shlex
import subprocess
import tempfile


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
                timeout: float = 120) -> subprocess.CompletedProcess:
    """Run a command with argument lists only (no shell), stdout/stderr
    captured; raises CalledProcessError on failure when check=True."""
    logging.debug('Running %s', command)
    result = subprocess.run(command, cwd=cwd, check=False,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout)
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
    /tmp/.mount_XXXXXX with the AppImage path as the mount source and
    `fuse.AppImage` as the filesystem type, e.g.

        /home/user/AppImages/foo.appimage /tmp/.mount_fooXXXX fuse.AppImage rw,... 0 0

    The fuse.* fstype filter keeps unrelated mounts whose source happens
    to be a regular file (loop devices, bind mounts) from matching.
    `mounts_path` overrides the /proc/mounts location (tests)."""
    if not file_path:
        return False

    for source, _target, fstype in parse_proc_mounts(mounts_path):
        if source == file_path and fstype.startswith('fuse.'):
            return True

    return False


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

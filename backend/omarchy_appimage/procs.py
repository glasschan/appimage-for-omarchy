# procs.py — which processes belong to an installed AppImage, and how to
# close them (stdlib only).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Identity rule, and the whole point of this module: a process belongs to
# an AppImage only when its `exe` PROVES it — either the AppImage path
# itself (a type-2 runtime parent keeps the AppImage as its own exe) or a
# path under a FUSE mount that /proc/mounts attributes to this AppImage.
# Nothing else is EVER matched, and nothing else is ever signalled.
# Process names, desktop `Exec=` lines and /proc/<pid>/env* are all
# either ambiguous or measurably empty for these processes (the runtime
# parent exports no APPIMAGE/APPDIR into its own process environment and
# a mount child's env* region reads as zeroed), so a match on them would
# be a guess — and a wrong kill is worse than a missed one.
#
# Two measured host facts the rules rest on:
#   * the runtime parent's /proc/<pid>/exe IS the AppImage file path;
#   * /proc/mounts' source field for an AppImage mount is a BASENAME
#     (`escrcpy.appimage /tmp/.mount_escrcpPC9VbJ fuse.escrcpy.appimage`),
#     never the absolute path — so mounts are matched on basenames.
#
# Everything runs through one probe seam (the module default `_PROBE`), so
# every branch below is testable without touching a real process: the
# process list, the /proc readers, the signal sender and the clock/sleep
# all come from the probe. Production signals with os.kill only — no
# pkill/pgrep, no shell.

import logging
import os
import signal
import time

from .utils import parse_proc_mounts, run_command

# One bounded `ps` call per scan (pid + exe, no shell). 10 s is far above
# any real `ps` run and still short enough that a wedged one can never
# stall the update path; the explicit timeout= is what rule 5 of
# scripts/ci/security-gates.sh pins.
_PS_COMMAND = ('ps', '-eo', 'pid=,exe=')
_PS_TIMEOUT_SECONDS = 10

# Wait-loop slice for TERM->grace->KILL: the set is polled this often, so
# a process that dies politely is noticed almost immediately.
_POLL_SECONDS = 0.05

# Spec #24: give a well-behaved app 5 s to shut down before SIGKILL.
_DEFAULT_TERM_GRACE_SECONDS = 5.0

# Per-pid outcomes of quit_app (the report's `outcomes` values).
_OUTCOME_TERMINATED = 'terminated'      # died on SIGTERM
_OUTCOME_SIGKILLED = 'sigkilled'        # survived the grace, needed SIGKILL
_OUTCOME_NOT_PERMITTED = 'not-permitted'    # another user's process
_OUTCOME_SIGNAL_FAILED = 'signal-failed'    # the signal itself failed


def _parse_ps_exe_lines(text: str) -> list:
    """Parse `ps -eo pid=,exe=` output into (pid, exe) pairs.

    `pid=` / `exe=` suppress the header, but the pid column keeps its
    padding ("  1 /sbin/init") and the exe column is EMPTY where ps could
    not resolve it (kernel thread, another user's process, a zombie), with
    `-` as ps's placeholder. An empty exe is kept as '' so iter_processes
    can fall back to /proc/<pid>/exe instead of dropping the pid here;
    anything without an integer pid is skipped."""
    pairs = []
    for line in text.splitlines():
        head, _, rest = line.strip().partition(' ')
        try:
            pid = int(head)
        except ValueError:
            continue
        if pid <= 0:
            continue
        exe = rest.strip()
        pairs.append((pid, '' if exe == '-' else exe))
    return pairs


class _SystemProbe:
    """The production probe: `ps` for the process list, /proc for exe and
    liveness, os.kill for the signals, time for the clock.

    `proc_root` / `mounts_path` / `ps_command` exist only so the production
    readers can be pointed at a fake tree or a failing `ps` in tests; every
    default is the real host path."""

    def __init__(self, proc_root: str = '/proc', mounts_path: str = None,
                 ps_command=None):
        self.proc_root = proc_root
        self.mounts_path = mounts_path or os.path.join(proc_root, 'mounts')
        self.ps_command = tuple(ps_command or _PS_COMMAND)

    def processes(self) -> list:
        """The raw `ps` view: (pid, exe hint), the hint possibly ''."""
        try:
            result = run_command(list(self.ps_command), check=True,
                                 timeout=_PS_TIMEOUT_SECONDS)
        except Exception as e:
            # Best-effort, exactly like utils._running_by_ps: no process
            # list means "nothing attributable", never a caller error.
            logging.warning('ps lookup failed: %s', e)
            return []
        return _parse_ps_exe_lines(result.stdout.decode(errors='replace'))

    def mounts(self) -> list:
        return parse_proc_mounts(self.mounts_path)

    def exe_for(self, pid: int):
        """The resolved /proc/<pid>/exe, or None when it is absent (the
        process died), unreadable (another user) or deleted."""
        try:
            return os.readlink(
                os.path.join(self.proc_root, str(pid), 'exe'))
        except OSError:
            return None

    def state_for(self, pid: int):
        """The /proc/<pid>/stat state char ('R', 'S', 'Z', ...), or None
        when the entry is unreadable (which for a live-enough pid means it
        is gone)."""
        try:
            with open(os.path.join(self.proc_root, str(pid), 'stat'),
                      'r', encoding='utf-8', errors='replace') as f:
                data = f.read()
        except OSError:
            return None
        # Field 3, after the parenthesised comm — which may itself contain
        # spaces and parentheses, so split at the LAST ')'.
        _head, sep, rest = data.rpartition(')')
        if not sep:
            return None
        fields = rest.split()
        return fields[0] if fields else None

    def signal(self, pid: int, sig: int):
        os.kill(pid, sig)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float):
        time.sleep(seconds)


_PROBE = _SystemProbe()


def _probe_or_default(probe):
    return _PROBE if probe is None else probe


def iter_processes(probe=None):
    """Yield (pid, exe) for every process the probe can attribute an exe
    to.

    The list comes from one bounded `ps -eo pid=,exe=` call. A pid whose
    exe column is empty falls back to /proc/<pid>/exe, and a pid that
    cannot be resolved there either is SKIPPED — an entry without an exe
    proves nothing and must never be guessed at."""
    probe = _probe_or_default(probe)
    for pid, exe in probe.processes():
        if not exe:
            exe = probe.exe_for(pid)
        if not exe:
            continue
        yield pid, exe


def mount_points_for(appimage_path: str, mounts_path: str = None) -> list:
    """The mount points /proc/mounts attributes to this AppImage.

    The match is on the mount's SOURCE BASENAME, because that field only
    ever holds the AppImage's file name — /proc/mounts says
    `escrcpy.appimage /tmp/.mount_escrcpPC9VbJ fuse.escrcpy.appimage`,
    never the absolute path, so comparing the full path (what
    is_fuse_mounted used to do) never matches.

    Only `fuse.*` mounts count: an unrelated regular-file source with the
    same basename (a loop device, a bind mount) must never attribute
    processes. A missing or unreadable mounts file yields [] (best-effort,
    like every other read here), and a stale entry whose mount is gone
    simply matches no exe. `mounts_path` overrides the /proc/mounts
    location (tests)."""
    if not appimage_path:
        return []
    return _mount_points(parse_proc_mounts(mounts_path), appimage_path)


def _mount_points(entries, appimage_path: str) -> list:
    """The basename rule over already-parsed mounts(5) entries, shared by
    mount_points_for and pids_for (which reads through the probe)."""
    basename = os.path.basename(appimage_path)
    if not basename:
        return []
    return [target for source, target, fstype in entries
            if fstype.startswith('fuse.')
            and os.path.basename(source) == basename]


def _under(path: str, mount_points) -> bool:
    """True when `path` resolves inside one of the mount points. The
    separator is part of the match: /tmp/.mount_fooextra is NOT under
    /tmp/.mount_foo."""
    return any(path == mount
               or path.startswith(mount.rstrip(os.sep) + os.sep)
               for mount in mount_points)


def pids_for(appimage_path: str, probe=None) -> dict:
    """{'parent': [pid...], 'children': [pid...]} for one AppImage.

    parent  — /proc/<pid>/exe IS the AppImage path (the type-2 runtime
              parent).
    children — exe lives under a mount point /proc/mounts attributes to
              this AppImage (the helpers running out of the FUSE mount;
              killing only the parent would orphan them against a mount
              that is about to disappear).

    Both lists are sorted. An empty path scans nothing, and an unreadable
    process list or a stale mount entry yields empty lists, never an
    error."""
    empty = {'parent': [], 'children': []}
    if not appimage_path:
        return empty

    probe = _probe_or_default(probe)
    mounts = _mount_points(probe.mounts(), appimage_path)
    parents, children = [], []
    for pid, exe in iter_processes(probe=probe):
        if exe == appimage_path:
            parents.append(pid)
        elif _under(exe, mounts):
            children.append(pid)
    return {'parent': sorted(parents), 'children': sorted(children)}


def quit_app(appimage_path: str, *,
             term_grace_s: float = _DEFAULT_TERM_GRACE_SECONDS,
             probe=None) -> dict:
    """Close every process that provably belongs to `appimage_path`.

    One SIGTERM round covers the parent AND the mount children together:
    killing only the runtime parent leaves its helpers running against a
    mount that is about to vanish. Surviving pids are polled for up to
    `term_grace_s` seconds, then SIGKILLed. A pid we may not signal
    (another user's process that happens to share the file name) is
    reported and left alone — never escalated. The mount set is
    recomputed here, so a process that appeared since the last check is
    included.

    Returns {'signalled': [pid...], 'killed_with_sigkill': [pid...],
    'outcomes': {pid: outcome}}. Only pids we acted on appear: a pid that
    vanished before the TERM landed is left out of both lists and
    `outcomes` entirely. Outcome values: 'terminated' (died on TERM),
    'sigkilled' (needed SIGKILL), 'not-permitted' (could not be signalled
    at all) and 'signal-failed' (the signal call itself failed)."""
    probe = _probe_or_default(probe)
    found = pids_for(appimage_path, probe=probe)
    # de-duplicated, parent first: the process the user sees leads the
    # round, its helpers follow in the same one
    targets = list(dict.fromkeys(found['parent'] + found['children']))

    outcomes = {}
    signalled = []
    for pid in targets:
        try:
            probe.signal(pid, signal.SIGTERM)
        except ProcessLookupError:
            # it exited while we were looking at it: nothing happened to
            # it, so it gets no outcome (and no entry in `signalled`)
            continue
        except PermissionError:
            outcomes[pid] = _OUTCOME_NOT_PERMITTED
            continue
        except OSError as e:
            logging.warning('could not signal %s: %s', pid, e)
            outcomes[pid] = _OUTCOME_SIGNAL_FAILED
            continue
        signalled.append(pid)
        outcomes[pid] = _OUTCOME_TERMINATED

    killed = []
    for pid in _wait_for_exit(probe, signalled, term_grace_s):
        try:
            probe.signal(pid, signal.SIGKILL)
        except ProcessLookupError:
            # exited between the last poll and the KILL: the TERM was
            # enough after all, so the outcome stays 'terminated'
            continue
        except OSError as e:
            logging.warning('could not SIGKILL %s: %s', pid, e)
            outcomes[pid] = _OUTCOME_SIGNAL_FAILED
            continue
        killed.append(pid)
        outcomes[pid] = _OUTCOME_SIGKILLED

    if signalled:
        logging.info('closed %d process(es) for %s (%d needed SIGKILL)',
                     len(signalled), appimage_path, len(killed))
    return {'signalled': signalled, 'killed_with_sigkill': killed,
            'outcomes': outcomes}


def _wait_for_exit(probe, pids: list, grace_s: float) -> list:
    """The survivors of the TERM round after at most `grace_s` seconds.

    A polite process often dies before the first poll, so the set is
    checked before any sleep (grace 0 means "no grace at all"). Liveness
    is signal 0 plus the /proc state: a ZOMBIE still answers signal 0 but
    has already exited — its parent just has not reaped it yet — and
    treating it as alive would burn the whole grace and then report a
    SIGKILL that never happened."""
    deadline = probe.monotonic() + max(0.0, grace_s)
    pending = [pid for pid in pids if _is_alive(probe, pid)]
    while pending:
        remaining = deadline - probe.monotonic()
        if remaining <= 0:
            break
        probe.sleep(min(_POLL_SECONDS, remaining))
        pending = [pid for pid in pending if _is_alive(probe, pid)]
    return pending


def _is_alive(probe, pid: int) -> bool:
    """Same rule as jobs.pid_alive (ProcessLookupError means gone,
    PermissionError means another user's process — alive), plus the zombie
    check above. It reads through the probe so tests drive it."""
    try:
        probe.signal(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    state = probe.state_for(pid)
    return state is not None and state not in ('Z', 'X')
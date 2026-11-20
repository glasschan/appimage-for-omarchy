# procs.py — which processes belong to an installed AppImage, and how to
# close them (stdlib only).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# A process belongs to an AppImage only when its `exe` PROVES it: the
# AppImage path itself (a type-2 runtime parent keeps the AppImage as its
# own exe), or a path under a FUSE mount that /proc/mounts attributes to
# this AppImage. Process names, desktop `Exec=` lines and /proc/<pid>/env*
# are ambiguous or measurably empty here, and a wrong kill is worse than a
# missed one.
#
# Two measured host facts the rules rest on:
#   * the runtime parent's /proc/<pid>/exe IS the AppImage file path;
#   * /proc/mounts' source field is a BASENAME
#     (`escrcpy.appimage /tmp/.mount_escrcpPC9VbJ fuse.escrcpy.appimage`),
#     never the absolute path, so mounts are matched on basenames.
#
# Everything runs through one probe seam (`_PROBE`), so every branch is
# testable without a real process. Production signals with os.kill only.

import logging
import os
import signal
import time

from .utils import parse_proc_mounts, run_command

# One bounded `ps` call per scan (pid + exe, no shell). The explicit
# timeout pins rule 5 of scripts/ci/security-gates.sh.
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
# the pid no longer proves it belongs to this AppImage (recycled pid, exe
# replaced underneath it, /proc entry unreadable): never signalled
_OUTCOME_IDENTITY_CHANGED = 'identity-changed'

# Outcomes that mean "still there and out of our reach": the pre-replace
# guard in cli refuses to swap the file on those alone.
_STUCK_OUTCOMES = (_OUTCOME_NOT_PERMITTED, _OUTCOME_SIGNAL_FAILED)

# SIGKILL cannot be ignored, but the process still needs a scheduling slot
# to die in: poll the killed set for this long before calling any of it
# survived (a false "survived" would refuse a legitimate update).
_SIGKILL_SETTLE_SECONDS = 1.0


def _parse_ps_exe_lines(text: str) -> list:
    """Parse `ps -eo pid=,exe=` output into (pid, exe) pairs.

    `pid=` / `exe=` suppress the header, but the pid column keeps its
    padding and the exe column is EMPTY where ps could not resolve it,
    with `-` as ps's placeholder. An empty exe is kept as '' so
    iter_processes can fall back to /proc/<pid>/exe; anything without an
    integer pid is skipped."""
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
        self.mounts_path = mounts_path
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
        """The parsed mounts table. With no explicit path, utils.PROC_MOUNTS_PATH
        is read at CALL time (lazy import), so patching that one name
        redirects the whole production path."""
        path = self.mounts_path
        if path is None:
            from . import utils
            path = utils.PROC_MOUNTS_PATH
        return parse_proc_mounts(path)

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


def mount_points_for(appimage_path: str, mounts_path: str = None,
                     probe=None) -> list:
    """The mount points /proc/mounts attributes to this AppImage.

    The match is on the mount's SOURCE BASENAME, because that field only
    ever holds the AppImage's file name: /proc/mounts says
    `escrcpy.appimage /tmp/.mount_escrcpPC9VbJ fuse.escrcpy.appimage`,
    never the absolute path, so comparing the full path never matches.

    Only `fuse.*` mounts count: an unrelated regular-file source with the
    same basename must never attribute processes. A missing or unreadable
    mounts file yields []. `mounts_path` overrides the /proc/mounts
    location (tests)."""
    if not appimage_path:
        return []
    if probe is not None and mounts_path is None:
        # the caller already holds a scan (a listing's _ScanCache): reuse
        # its mounts table instead of re-reading /proc/mounts per row
        return _mount_points(_probe_or_default(probe).mounts(), appimage_path)
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

    parent: /proc/<pid>/exe IS the AppImage path (the type-2 runtime
    parent). children: exe lives under a mount point /proc/mounts
    attributes to this AppImage, the helpers running out of the FUSE
    mount, which killing only the parent would leave orphaned.

    Both lists are sorted, and an unreadable process list or stale mount
    entry yields empty lists."""
    empty = {'parent': [], 'children': []}
    if not appimage_path:
        return empty

    probe = _probe_or_default(probe)
    mounts = _mount_points(probe.mounts(), appimage_path)
    wanted = os.path.realpath(appimage_path)
    parents, children = [], []
    for pid, exe in iter_processes(probe=probe):
        if _is_own_exe(exe, wanted):
            parents.append(pid)
        elif _under(exe, mounts):
            children.append(pid)
    return {'parent': sorted(parents), 'children': sorted(children)}


def _is_own_exe(exe: str, wanted_realpath: str) -> bool:
    """The parent rule: this process's exe IS the AppImage.

    Both sides are realpath-normalised, because an install directory is
    routinely a symlink (~/Applications -> ~/AppImages) while /proc and
    `ps` report whichever spelling the process was started with. The
    normalisation is also what keeps an exe rendered `<path> (deleted)`
    out of the match. This is the ONE rule `running` and `running_pids`
    both answer through."""
    if exe == wanted_realpath:
        return True
    return os.path.realpath(exe) == wanted_realpath


def _still_ours(probe, pid: int, wanted_realpath: str, mounts) -> bool:
    """Re-prove membership for one pid against the CURRENT /proc view.

    A pid captured by the scan can be recycled before the signal lands
    (and between the TERM round and the KILL round the exe can change
    too), so membership is re-read right before every signal. An exe that
    is gone, unreadable or different proves nothing and the pid is
    dropped."""
    exe = probe.exe_for(pid)
    if not exe:
        return False
    return _is_own_exe(exe, wanted_realpath) or _under(exe, mounts)


def quit_app(appimage_path: str, *,
             term_grace_s: float = _DEFAULT_TERM_GRACE_SECONDS,
             probe=None) -> dict:
    """Close every process that provably belongs to `appimage_path`.

    One SIGTERM round covers the parent AND the mount children together,
    since killing only the runtime parent leaves its helpers running
    against a mount that is about to vanish. Surviving pids are polled for
    up to `term_grace_s` seconds, then SIGKILLed. A pid we may not signal
    is reported and left alone, never escalated.

    This is ONE scan plus one signal round: processes that appeared later
    are not picked up, but each pid already in the set is re-proved before
    every signal, so a recycled pid is dropped as 'identity-changed'.

    Returns {'signalled', 'killed_with_sigkill', 'survived', 'outcomes'}.
    Only pids we acted on appear in `signalled` / `killed_with_sigkill`.
    Outcomes: 'terminated', 'sigkilled', 'not-permitted',
    'signal-failed', 'identity-changed'. `survived` lists pids still
    there afterwards; the caller refuses to replace their file."""
    probe = _probe_or_default(probe)
    found = pids_for(appimage_path, probe=probe)
    # de-duplicated, parent first: the process the user sees leads the
    # round, its helpers follow in the same one
    targets = list(dict.fromkeys(found['parent'] + found['children']))
    mounts = _mount_points(probe.mounts(), appimage_path)
    wanted = os.path.realpath(appimage_path)

    outcomes = {}
    signalled = []
    for pid in targets:
        if not _still_ours(probe, pid, wanted, mounts):
            outcomes[pid] = _OUTCOME_IDENTITY_CHANGED
            continue
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
        if not _still_ours(probe, pid, wanted, mounts):
            # recycled during the grace: the new process is not ours
            outcomes[pid] = _OUTCOME_IDENTITY_CHANGED
            continue
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

    still_alive = set(_wait_for_exit(probe, killed, _SIGKILL_SETTLE_SECONDS))
    survived = sorted(pid for pid, outcome in outcomes.items()
                      if outcome in _STUCK_OUTCOMES
                      or (outcome == _OUTCOME_SIGKILLED
                          and pid in still_alive))

    if signalled:
        logging.info('closed %d process(es) for %s (%d needed SIGKILL)',
                     len(signalled), appimage_path, len(killed))
    return {'signalled': signalled, 'killed_with_sigkill': killed,
            'survived': survived, 'outcomes': outcomes}


def _wait_for_exit(probe, pids: list, grace_s: float) -> list:
    """The survivors of the TERM round after at most `grace_s` seconds.

    A polite process often dies before the first poll, so the set is
    checked before any sleep. Liveness is signal 0 plus the /proc state: a
    ZOMBIE still answers signal 0 but has already exited, and treating it
    as alive would burn the whole grace and then report a SIGKILL that
    never happened."""
    deadline = probe.monotonic() + max(0.0, grace_s)
    pending = [pid for pid in pids if _is_alive(probe, pid)]
    while pending:
        remaining = deadline - probe.monotonic()
        if remaining <= 0:
            break
        probe.sleep(min(_POLL_SECONDS, remaining))
        pending = [pid for pid in pending if _is_alive(probe, pid)]
    return pending


class _ScanCache:
    """A probe wrapper that answers processes() and mounts() from ONE
    read each, for the length of one caller's scan.

    A panel listing asks pids_for once per installed app; without the
    cache that is one fresh `ps` spawn plus one /proc/mounts parse per row,
    every 10 s. Everything else passes straight through to the real probe,
    so liveness stays live."""

    def __init__(self, probe):
        self._probe = probe
        self._processes = None
        self._mounts = None

    def processes(self):
        if self._processes is None:
            self._processes = self._probe.processes()
        return self._processes

    def mounts(self):
        if self._mounts is None:
            self._mounts = self._probe.mounts()
        return self._mounts

    def __getattr__(self, name):
        return getattr(self._probe, name)


def _cached_probe(probe=None):
    """One process table + one mounts table, reusable across several
    pids_for calls. A helper for cli's listing only."""
    return _ScanCache(_probe_or_default(probe))


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
        # any other OSError means "still there, for all we know": only
        # ProcessLookupError proves the pid is gone
        return True
    state = probe.state_for(pid)
    return state is not None and state not in ('Z', 'X')
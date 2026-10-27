# janitor.py — best-effort sweep of fossil job documents and orphan
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
# update temp dirs (stdlib only).
#
# Runs (ticket 03): (a) in the async-update PARENT before it spawns a job,
# (b) on every --list-installed. NEVER in the detached grandchild (the
# grandchild owns exactly one live job and exits through its own scoped +
# global temp-dir cleanup), and NEVER via extractor.cleanup_temp_dirs()
# — that global wipe is the process-exit backstop of the process that
# owns the registry; this module decides from DISK evidence alone (it
# runs in a different process than any live job and cannot see another
# process's registry).
#
# Everything here is best-effort and bounded: every per-item operation is
# wrapped in try/except OSError (plus a blanket except Exception guard),
# removals are capped per sweep, and the entrypoints never raise — a
# sweep failure must never fail --list-installed or a job start.

import json
import logging
import os
import shutil
import stat
import tempfile
import time

from . import constants, extractor, icon, jobs, utils

# A terminal (done|error) or interrupted document is removed — together
# with its per-job log — once this window has passed since its last
# activity. For every document the writer refreshes `heartbeat` on each
# rewrite (JobWriter._write), so the terminal rewrite's heartbeat doubles
# as the finished-at timestamp and a dead job's heartbeat is its last
# sign of life; that field (falling back to the file mtime when it is
# missing or malformed, e.g. for corrupt documents) is the window clock.
# 24 h comfortably outlives a desktop session, so the panel can still
# show `interrupted` (and offer a retry) long after a crashed job, while
# the state dir cannot grow without bound.
JOB_ACK_WINDOW_SECONDS = 24 * 60 * 60

# An `appimage-update-*` dir in the temp area (the prefix provider.
# update_from_url downloads into; legacy orphans from hard-killed
# pre-async downloads carry no registry entry and no job document) is
# removed once its EFFECTIVE mtime — the max of the dir's own mtime and
# its immediate entries' mtimes — is older than this grace window. The
# children matter: net.download_to_file writes one file continuously, so
# during a live download the FILE's mtime keeps advancing while the
# DIRECTORY's only reflects entry creation. 600 s is 5x
# jobs.JOB_STALE_AFTER_SECONDS (120 s) and 20x the downloader's 30 s
# per-read stall guard: a live job cannot go quiet that long, and a job
# whose document still classifies active (heartbeat < 120 s old) always
# has a download dir well inside the grace window.
UPDATE_TEMP_GRACE_SECONDS = 600

# Prefixes of temp dirs that may be swept from the temp area. Extraction
# dirs (`gearlever_appimage_`, `unsquashfs-`, `bsdtar-`, anything else) are
# never touched — they belong to a live process's registry.
#
# `appimage-update-` is provider.update_from_url's download dir (and the
# legacy pre-async orphans). `omarchy-integrate-` is cmd_integrate's
# per-run dir (cli.py mkdtemp — kept in lockstep through this constant):
# a clean exit removes it in its `finally`, but a SIGNALLED child skips
# that, and that is the normal case for the panel — closing the panel
# unloads the panel QML object, which destroys the Quickshell Process and
# SIGTERMs python mid-download, leaving a truncated multi-MiB AppImage in
# the temp area with no process behind it.
INTEGRATE_TEMP_PREFIX = 'omarchy-integrate-'

UPDATE_TEMP_PREFIXES = ('appimage-update-', INTEGRATE_TEMP_PREFIX)

# Hard bounds per sweep: the scans stop after this many removals, so a
# pathological state/temp dir cannot make --list-installed slow.
MAX_REMOVALS_PER_SWEEP = 200

# The prefix of the temporary file the icon sweep writes before it
# renames the sanitised bytes over the old icon: owned by icon.py now,
# kept here as an alias for compatibility with the docs/tests that name
# it (the atomic writer is icon.replace_icon_file).
ICON_TMP_PREFIX = icon.ICON_TMP_PREFIX


def sweep_job_documents(now: float = None) -> int:
    """Remove terminal/interrupted job documents past the acknowledgement
    window (JOB_ACK_WINDOW_SECONDS), each together with its log.

    Per document (one `*.json` file in jobs.jobs_dir()):
      * ACTIVE documents (jobs.classify_job: non-terminal phase, live pid,
        fresh heartbeat) are NEVER touched;
      * every other document ages out from its `heartbeat` (the writer
        refreshes it on every rewrite, so a terminal document's heartbeat
        is its finished-at time and an interrupted one's is its last sign
        of life) — falling back to the file mtime when the field is
        missing or malformed (corrupt documents have no heartbeat at all
        and age out from their mtime under the same window);
      * symlinks and non-regular files are skipped, never followed.

    Returns the number of documents removed. Never raises.
    """
    if now is None:
        now = time.time()
    removed = 0
    try:
        entries = os.listdir(jobs.jobs_dir())
    except OSError as e:
        logging.debug('janitor: no job directory to sweep: %s', e)
        return 0

    for name in entries:
        if removed >= MAX_REMOVALS_PER_SWEEP:
            break
        if not name.endswith('.json'):
            continue
        path = os.path.join(jobs.jobs_dir(), name)
        try:
            if not _is_regular_file(path):
                continue
            doc = _read_document(path)
            classification = jobs.classify_job(doc, now=now)
            if classification == 'active':
                continue
            if _age_seconds(path, doc, now) <= JOB_ACK_WINDOW_SECONDS:
                continue
            os.unlink(path)
            removed += 1
            _remove_log_companion(path)
            logging.debug('janitor: removed stale job document %s (%s)',
                          path, classification)
        except Exception as e:
            logging.debug('janitor: skipped job document %s: %s', path, e)
    return removed


def sweep_update_temp_dirs(now: float = None,
                           temp_area: str = None) -> int:
    """Remove partial-download temp dirs with no live owner.

    Disk-only rule (the janitor runs in a different process than any live
    job, so the in-process registry is invisible): a temp dir whose name
    starts with one of UPDATE_TEMP_PREFIXES — `appimage-update-*` (async
    update downloads) or `omarchy-integrate-*` (a `--integrate` URL run's
    download) — and whose EFFECTIVE mtime (the dir's own mtime vs. the max
    of its immediate entries' mtimes — a live download keeps writing its
    file, so the file's mtime is the live signal) is older than
    UPDATE_TEMP_GRACE_SECONDS is a fossil and is removed. Symlinked
    entries are skipped (never followed), and nothing with any other
    prefix — `gearlever_appimage_*` above all — is ever touched.

    `temp_area` overrides the scan root (default tempfile.gettempdir(),
    the same area provider.update_from_url downloads into). Returns the
    number of dirs removed. Never raises.
    """
    if now is None:
        now = time.time()
    if temp_area is None:
        temp_area = tempfile.gettempdir()
    removed = 0
    try:
        entries = os.listdir(temp_area)
    except OSError as e:
        logging.debug('janitor: cannot list temp area %s: %s', temp_area, e)
        return 0

    for name in entries:
        if removed >= MAX_REMOVALS_PER_SWEEP:
            break
        if not name.startswith(UPDATE_TEMP_PREFIXES):
            continue
        path = os.path.join(temp_area, name)
        try:
            if os.path.islink(path) or not os.path.isdir(path):
                continue
            if _effective_age_seconds(path, now) \
                    <= UPDATE_TEMP_GRACE_SECONDS:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
            logging.debug('janitor: removed orphan update dir %s', path)
        except Exception as e:
            logging.debug('janitor: skipped temp dir %s: %s', path, e)
    return removed


def sweep_icons(icons_dir: str = None, reg: dict = None) -> int:
    """Make the retained icons safe to decode: sanitise them or remove them.

    The v0.9.9 sanitiser gates the EXTRACT path only, and provider.py
    unlinks the icons of the stem it replaces; an icon written by an
    older plugin version is otherwise decoded by Panel.qml on every panel
    open. The first backstop for that store was a header-only check
    (icon.installed_icon_is_unsafe): it could read the IHDR dimensions
    and the file size, but not the ancillary chunks — a small-dimension
    PNG carrying inflated text/ICC metadata passed it and was decoded
    unbounded. That function is gone; this sweep now runs the FULL
    sanitiser (icon.sanitize_icon) over every retained icon:

    * a file larger than extractor.MAX_ICON_BYTES is removed — it is
      not worth sanitising, and its stored bytes alone are past the cap;
    * the format comes from the file name, and from the content when the
      name says nothing (icon.retained_bytes);
    * the sanitised bytes REPLACE the file atomically
      (icon.replace_icon_file), or the file is left byte-identical when
      it is already sanitised;
    * a file the sanitiser refuses — or cannot read — is REMOVED: an
      icon that cannot be sanitised must not be handed to the decoder.

    Every outcome is mirrored into the sanitisation registry (`reg`,
    default icon.load_sanitized_registry()): kept/rewritten icons get an
    entry recording the CURRENT stat, removed/skipped ones lose theirs,
    and registry entries naming files no longer in the folder are pruned.
    The registry is SAVED at the end when this pass changed it
    (best-effort).

    The folder is the plugin's own (one icon per integrated app) and the
    sweep is throttled (janitor.sweep_throttled), so a full pass stays
    affordable; the removal cap bounds the destructive part of it.
    Symlinks and non-regular files are skipped; returns the number of
    files removed; never raises.
    """
    if not icons_dir or os.path.islink(icons_dir) \
            or not os.path.isdir(icons_dir):
        return 0
    if reg is None:
        reg = icon.load_sanitized_registry()
    dirty = False
    try:
        entries = os.listdir(icons_dir)
    except OSError as e:
        logging.debug('janitor: cannot list icons dir %s: %s', icons_dir, e)
        return 0
    removed = 0
    changed = 0
    seen = set()
    for name in entries:
        if changed >= MAX_REMOVALS_PER_SWEEP:
            break
        if name.startswith('.'):
            # The panel loads `<stem>.png`/`<stem>.svg`, and a stem is a
            # visible file name; a hidden name is what the plugin's own
            # atomic writers leave behind while an install is in flight
            # (provider._replace_destination, utils.atomic_write):
            # reading a half-written one would refuse it and delete it,
            # which would break the install that owns it.
            continue
        path = os.path.join(icons_dir, name)
        seen.add(path)
        try:
            if not _is_regular_file(path):
                reg.pop(path, None)
                dirty = True
                continue
            outcome = _sanitize_installed_icon(path)
        except Exception as e:
            logging.debug('janitor: skipped icon %s: %s', path, e)
            if reg.pop(path, None) is not None:
                dirty = True
            continue
        if outcome in ('kept', 'rewritten'):
            if _record_icon_entry(path, reg):
                dirty = True
        else:
            if outcome == 'removed':
                removed += 1
                changed += 1
            if reg.pop(path, None) is not None:
                dirty = True
    # Opportunistic prune: entries for icons the folder no longer holds
    # (an uninstalled app, an integrated icon that replaced its stem).
    for path in list(reg.keys()):
        if os.path.dirname(path) == icons_dir and path not in seen:
            reg.pop(path, None)
            dirty = True
    if dirty:
        icon.save_sanitized_registry(reg)
    return removed


def _record_icon_entry(path: str, reg: dict) -> bool:
    """Record an icon's current stat in the registry (True when the map
    changed). A file that vanished mid-sweep gets its entry dropped."""
    try:
        st = os.lstat(path)
    except OSError:
        return reg.pop(path, None) is not None
    entry = {'v': icon.SANITIZER_VERSION, 's': st.st_size,
             'm': st.st_mtime_ns}
    if reg.get(path) != entry:
        reg[path] = entry
        return True
    return False


def verify_retained_icon(path: str, reg: dict) -> bool:
    """THE retained-icon verdict: did the CURRENT sanitiser prove this file?

    The listing calls this per candidate icon and reports the answer as
    the app object's additive `icon_safe`; the frontend renders its
    built-in glyph unless it is True, so the shell's decoder never opens
    a file the current gate has not proved — not during a throttle
    window, not after an interrupted sweep, not past the sweep's file
    cap, not after a failed rewrite.

    Fast path: a registry entry recorded by THIS gate version whose
    size/mtime_ns still match the file answers without a read or a
    decode. Slow path: read (icon.retained_bytes), sanitise, and either
    record (bytes already safe), rewrite and record the new stat, or
    drop the entry on any refusal. The FILE is never deleted here — the
    sweep owns deletion; a refusal costs a glyph. Never raises; may
    mutate `reg` (the caller persists it)."""
    try:
        st = os.lstat(path)
    except OSError:
        reg.pop(path, None)
        return False
    entry = reg.get(path)
    if isinstance(entry, dict):
        v, s, m = entry.get('v'), entry.get('s'), entry.get('m')
        if (isinstance(v, int) and v == icon.SANITIZER_VERSION
                and isinstance(s, int) and s == st.st_size
                and isinstance(m, int) and m == st.st_mtime_ns):
            return True
    data, fmt = icon.retained_bytes(path)
    if data is None:
        reg.pop(path, None)
        return False
    safe = icon.sanitize_icon(data, fmt)
    if safe is None:
        reg.pop(path, None)
        return False
    if safe == data:
        reg[path] = {'v': icon.SANITIZER_VERSION, 's': st.st_size,
                     'm': st.st_mtime_ns}
        return True
    try:
        icon.replace_icon_file(path, safe, stat.S_IMODE(st.st_mode))
    except OSError:
        reg.pop(path, None)
        return False
    try:
        st = os.lstat(path)
    except OSError:
        reg.pop(path, None)
        return False
    reg[path] = {'v': icon.SANITIZER_VERSION, 's': st.st_size,
                 'm': st.st_mtime_ns}
    return True


def _sanitize_installed_icon(path: str) -> str:
    """Sanitise one retained icon in place.

    Returns 'kept' (the file was already sanitised), 'rewritten' (the
    sanitised bytes replaced it), 'removed' (it could not be made safe)
    or 'skipped' (it is not a regular file after all). Never raises:
    every failure ends in a removal or in the file being left alone.
    The read and the atomic rewrite go through icon.retained_bytes /
    icon.replace_icon_file so the sweep and the listing's verdict share
    the same primitives.
    """
    st = os.lstat(path)
    if st.st_size > extractor.MAX_ICON_BYTES:
        os.unlink(path)
        logging.debug('janitor: removed oversized installed icon %s', path)
        return 'removed'
    data, fmt = icon.retained_bytes(path)
    if data is None:
        if os.path.islink(path):
            # the name was a regular file a moment ago, and a symlink
            # planted in its place must not be followed (nor removed)
            return 'skipped'
        # An icon we cannot read cannot be sanitised, and the shell
        # decodes whatever sits in the folder: it goes.
        logging.debug('janitor: cannot read installed icon %s', path)
        _unlink_quietly(path)
        return 'removed'
    safe = icon.sanitize_icon(data, fmt) if fmt else None
    if safe is None:
        os.unlink(path)
        logging.debug('janitor: removed unsanitisable installed icon %s',
                      path)
        return 'removed'
    if safe == data:
        return 'kept'
    icon.replace_icon_file(path, safe, stat.S_IMODE(st.st_mode))
    logging.debug('janitor: sanitised installed icon %s', path)
    return 'rewritten'


def _unlink_quietly(path: str):
    """Remove a file, ignoring the case where it is already gone."""
    try:
        os.unlink(path)
    except OSError as e:
        logging.debug('janitor: cannot remove installed icon %s: %s',
                      path, e)



def sweep(now: float = None, temp_area: str = None,
          icons_dir: str = None) -> dict:
    """All three sweeps, best-effort: job documents past the
    acknowledgement window, orphan download temp dirs (UPDATE_TEMP_PREFIXES)
    past the grace window, then the retained icons in `icons_dir` (the
    managed folder's `.icons`), each sanitised before the shell may
    decode it and removed when it cannot be sanitised. Returns
    per-surface removal counts; never raises."""
    counts = {'documents_removed': 0, 'temp_dirs_removed': 0,
              'icons_removed': 0}
    try:
        counts['documents_removed'] = sweep_job_documents(now=now)
    except Exception:  # belt and braces: sweep() itself never raises
        logging.exception('janitor: job-document sweep failed')
    try:
        counts['temp_dirs_removed'] = sweep_update_temp_dirs(
            now=now, temp_area=temp_area)
    except Exception:
        logging.exception('janitor: temp-dir sweep failed')
    try:
        counts['icons_removed'] = sweep_icons(icons_dir=icons_dir)
    except Exception:
        logging.exception('janitor: icon sweep failed')
    return counts


# --list-installed runs on the panel's 10 s poll, and each sweep is an
# UNBOUNDED os.listdir + stat walk of the shared temp dir (every other
# user's temp entries land in that same walk) in the CLI's FOREGROUND —
# the listing hook must not pay that every tick. Job starts keep
# sweeping unconditionally: they are rare, and a fossil document must be
# gone before the new job's document appears.
SWEEP_THROTTLE_SECONDS = 5 * 60


def sweep_marker_path() -> str:
    """The throttle marker: plugin state dir, NEVER /tmp.

    A /tmp marker would be (a) wiped by the very cleanup the sweep does
    and (b) shared with every other process on the box."""
    return os.path.join(constants.plugin_state_dir(), 'last-sweep')


def _write_sweep_marker(now: float):
    """Stamp the marker as JSON {"v": SANITIZER_VERSION, "at": epoch}.

    The version makes a sanitiser change force exactly one re-sweep; `at`
    is the throttled call's epoch."""
    try:
        utils.atomic_write(
            sweep_marker_path(),
            json.dumps({'v': icon.SANITIZER_VERSION,
                        'at': float(now)}).encode('utf-8'))
    except OSError as e:
        logging.debug('janitor: could not stamp the sweep marker: %s', e)


def _read_sweep_marker():
    """(version, epoch) out of the marker, or None when it cannot say.

    The current format is the JSON `{"v": ..., "at": ...}` stamp; a
    legacy bare-float marker (pre-v2) reads as version 0, so the first
    reader re-sweeps and the new format replaces it. Anything else —
    missing, corrupt, wrongly shaped — is None ("never swept")."""
    try:
        with open(sweep_marker_path(), 'r', encoding='utf-8') as f:
            raw = f.read().strip()
    except OSError:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        doc = None
    if isinstance(doc, dict):
        try:
            return int(doc.get('v')), float(doc.get('at'))
        except (TypeError, ValueError):
            return None
    try:
        return 0, float(raw)
    except ValueError:
        return None


def sweep_throttled(interval_seconds: float = SWEEP_THROTTLE_SECONDS,
                    now: float = None, icons_dir: str = None) -> bool:
    """sweep(), at most once per `interval_seconds`.

    For the high-frequency hook (--list-installed) only; sweep() itself
    keeps its semantics for every direct caller. The marker is JSON
    carrying the sanitiser version and the sweep's epoch: the throttle
    holds only when the marker names the CURRENT SANITIZER_VERSION and
    its stamp is inside the window — a marker left by an older gate (or
    the legacy bare-float format) forces exactly one re-sweep. A MISSING
    or CORRUPT marker means "never swept" and sweeps (the throttle is an
    optimisation, never a precondition — a janitor that stops running
    because its bookkeeping broke is worse than one extra sweep). A
    marker timestamped in the future (clock skew) is treated the same
    way.

    The marker is stamped AFTER sweep() returns: a sweep interrupted by
    process death leaves the OLD marker, so the next poll re-sweeps
    instead of trusting a pass that never finished. The bounded sweeps
    (MAX_REMOVALS_PER_SWEEP) make that crash-loop cost acceptable, and
    verify_retained_icon's registry fast path keeps the listing cheap
    regardless.

    Returns True when this call swept, False when it was throttled.
    Never raises."""
    if now is None:
        now = time.time()
    marker = _read_sweep_marker()
    if marker is not None:
        version, last = marker
        if version == icon.SANITIZER_VERSION \
                and 0 <= now - last < interval_seconds:
            return False
    sweep(now=now, icons_dir=icons_dir)
    _write_sweep_marker(now)
    return True


# ----------------------------------------------------------------- helpers

def _is_regular_file(path: str) -> bool:
    """True for a plain regular file only. Symlinks are skipped outright
    (never followed, never removed — the writer never produces them), as
    are directories and every other file type."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _read_document(path: str):
    """The parsed job document, or None when unreadable/corrupt (the
    janitor's own read — it walks the directory and has no desktop id
    for jobs.read_job; corrupt documents fall through to the mtime
    rule)."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _document_timestamp(doc, path: str, now: float) -> float:
    """The document's last-activity epoch second: `heartbeat` when it is
    a sane number, else the file's mtime (corrupt documents, legacy
    shapes)."""
    heartbeat = doc.get('heartbeat') if isinstance(doc, dict) else None
    try:
        value = float(heartbeat)
    except (TypeError, ValueError):
        value = 0.0
    # sanity: a future timestamp is corrupt, never a live clock — fall
    # through to mtime so a far-future value can't pin the file forever
    if 0 < value <= max(now, 0.0):
        return value
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _age_seconds(path: str, doc, now: float) -> float:
    return max(0.0, now - _document_timestamp(doc, path, now))


def _effective_age_seconds(path: str, now: float) -> float:
    """now - max(mtime of `path`, mtime of its immediate entries): the
    dir mtime alone goes stale the moment a live download stops creating
    entries, the file mtimes keep moving until it finishes."""
    newest = 0.0
    try:
        newest = os.lstat(path).st_mtime
        for name in os.listdir(path):
            try:
                newest = max(newest,
                             os.lstat(os.path.join(path, name)).st_mtime)
            except OSError:
                continue
    except OSError:
        return 0.0
    return max(0.0, now - newest)


def _remove_log_companion(document_path: str):
    """Remove `<stem>.log` next to a removed document (best-effort; a
    directory squatting on the log name is left alone)."""
    log_path = document_path[:-len('.json')] + '.log'
    try:
        if os.path.isdir(log_path) and not os.path.islink(log_path):
            return
        os.unlink(log_path)
    except OSError as e:
        logging.debug('janitor: could not remove job log %s: %s',
                      log_path, e)

# envelope.py — shared CLI envelope helpers (ticket #56).
#
# The helpers every verb reads: JSON printing, the interactive ask, argv
# flag helpers, option validation, target resolution, the --list-installed
# app-object normaliser, the updates-state key/normalisers and the print
# table. Also the module-level singletons (appimage_provider) and the
# exit codes / error types the whole CLI speaks. Nothing here parses a
# verb's own argv; that belongs to the per-verb modules.
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import json
import logging
import os
import sys

from .. import procs, utils
from ..provider import AppImageProvider

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


class UsageError(Exception):
    pass


class OperationError(Exception):
    pass


appimage_provider = AppImageProvider()


# ------------------------------------------------------------------ helpers

def _json_dump(data) -> str:
    return json.dumps(data, ensure_ascii=False)


def _print_json(data):
    print(_json_dump(data))


def _ask(message: str, options: list) -> str:
    value = None
    message = message.strip() + ' '
    while value not in options:
        try:
            value = input(message)
        except EOFError:
            print(file=sys.stderr)
            raise OperationError('aborted: no input available')
    return value


def _remove_flag_value(args: list, flags: list) -> list:
    return [a for a in args if a not in flags]


def _running_pid_count(el, probe=None) -> int:
    """How many processes provably belong to this installed AppImage
    (ticket #27): `procs.pids_for`'s runtime parent plus its FUSE-mount
    children — the same set `--kill-running` would close.

    Display-only: the panel's force-quit card names the number before the
    user agrees to close the app, and nothing acts on it. Best-effort like
    the `running` flag it accompanies — a failed scan reports 0 instead of
    erroring (one warning, not a traceback: the panel polls this every
    10 s), and callers only ask for RUNNING rows.

    `probe` is procs' probe seam; a listing passes ONE cached probe (see
    procs._cached_probe) so N running rows cost one `ps` and one mounts
    parse instead of N of each."""
    try:
        found = procs.pids_for(el.file_path, probe=probe)
        return len(found['parent']) + len(found['children'])
    except Exception as e:
        logging.warning('could not count the processes of %s: %s',
                        el.file_path, e)
        return 0


def _make_app_json(el, manager=None, running=None, probe=None,
                   pending=None) -> dict:
    if running is None:
        try:
            # `probe` is the listing's ONE cached scan: asking the question
            # per row would spawn a fresh `ps` for every app, every poll
            # (ticket #41)
            running = appimage_provider.is_app_running(el, probe=probe)
        except Exception:
            running = False
            logging.exception('is_app_running failed')

    desktop_file_path = getattr(el, 'desktop_file_path', None)
    return {
        'name': el.name,
        'path': el.file_path,
        'desktop_id': os.path.basename(desktop_file_path) if desktop_file_path else None,
        'current_version': getattr(el, 'version', None),
        'available_version': manager.available_version if manager else None,
        'download_size': manager.download_size if manager else None,
        'manager': manager.name if manager else None,
        # additive (schema_version stays 1), issue #39: the method a fetch
        # would use to prove the artifact, and whether that fetch would even
        # be attempted. `null` means no verdict yet (no check has run, or
        # there is no source); the policy itself is unchanged — a source
        # that cannot prove its artifact still refuses the install.
        'verification': _verification_of(manager),
        'installable': _installable_of(manager),
        'embedded_source': bool(manager and manager.embedded),
        'running': running,
        # additive (schema_version stays 1): how many processes belong to
        # this AppImage, 0 when it is not running or the scan cannot tell.
        # Only a running row is scanned — a non-running one costs nothing.
        'running_pids': _running_pid_count(el, probe) if running else 0,
        # the desktop entry's Exec arguments (field codes stay VERBATIM —
        # the UI strips them), "" when the app has none
        'exec_arguments': getattr(el, 'exec_arguments', '') or '',
        # additive (schema_version stays 1), issue #53: the verdict the
        # last check recorded for this app. `pending` is the record's own
        # entry for this app (None when the record has none): an entry
        # means a pending release is known, and it carries the version
        # that check found and whether that release could be installed.
        # The record is the backend's memory, so the listing reports the
        # same verdict on every poll without checking the network again.
        # Only a record (dict) or a legacy signature (str) counts as
        # pending — a corrupt scalar/container entry is not a verdict,
        # matching the normalisers below (adversarial review, 0.11.0).
        'pending_update': isinstance(pending, (dict, str)),
        'pending_version': (_update_state_version(pending) or None)
        if isinstance(pending, (dict, str)) else None,
        'pending_installable': _update_state_installable(pending),
    }


# The four verification methods a source can report (issue #39). Only the
# two digest methods let download() accept an artifact, which is what makes
# installability derivable instead of a second opinion.
DIGEST_VERIFICATIONS = ('sha256', 'zsync-sha1')
VERIFICATIONS = DIGEST_VERIFICATIONS + ('size', 'none')


def _verification_of(manager) -> str:
    """The method this source would use, normalised to the contract's set."""
    if manager is None:
        return 'none'
    method = manager.verification_method()
    return method if method in VERIFICATIONS else 'none'


def _installable_of(manager):
    """True/False once a check reached a verdict, None before one.

    Derived from the method so the two fields can never disagree: only a
    digest lets the fetch path install, and #39 changes reporting only."""
    if manager is None:
        return None
    verdict = manager.installable()
    if verdict is None:
        return None
    return bool(verdict) and _verification_of(manager) in DIGEST_VERIFICATIONS


def _reject_unknown_options(args: list, allowed: set):
    """Raise UsageError for any '-'-prefixed token outside `allowed`.

    Every verb validates its own option set: an option it does not
    implement is a usage error, never a silently ignored token (same rule
    CONTRACT.md states for the preview verb)."""
    for a in args:
        if a.startswith('-') and a not in allowed:
            raise UsageError(f'unknown option: {a!r}')


def _resolve_target(target: str):
    """Resolve a path / desktop-id / app name to an installed element."""
    installed = appimage_provider.list_installed()
    target = target.strip()

    # 1. exact AppImage file path (GearLever's behaviour)
    for el in installed:
        if el.file_path == target:
            return el

    # 2. desktop id, with or without the .desktop extension
    wanted = target[:-8] if target.endswith('.desktop') else target
    for el in installed:
        if el.desktop_file_path and \
                os.path.basename(el.desktop_file_path) == wanted + '.desktop':
            return el

    # 3. desktop-file name, case-insensitive
    for el in installed:
        if el.name.lower() == target.lower():
            return el

    # 4. a stale record: its .desktop file is gone, so list_installed()
    #    cannot know it — but the record in apps.ini still names the
    #    AppImage, and the user must be able to reclaim it explicitly.
    for el in appimage_provider.list_stale_records():
        if el.file_path == target:
            return el
        if el.desktop_file_path and \
                os.path.basename(el.desktop_file_path) == wanted + '.desktop':
            return el
        if el.name.lower() == target.lower():
            return el

    raise OperationError(
        f'AppImage not integrated: {target!r} '
        f'(see --list-installed for valid names/ids)')


def _updates_state_key(el) -> str:
    """Key for the updates-state.json map: desktop id, falling back to the
    AppImage file name (settings.updates_state_path entries)."""
    if el.desktop_file_path:
        return os.path.basename(el.desktop_file_path)
    return os.path.basename(el.file_path or '')


# A real release tag is far shorter than this; the cap only bounds a
# pathological or hostile `available_version` (adversarial review, 0.11.0).
MAX_PERSISTED_VERSION_CHARS = 200


def _capped_version(value) -> str:
    """The version string for the record, truncated to the persisted cap."""
    return str(value or '')[:MAX_PERSISTED_VERSION_CHARS]


def _update_state_version(entry) -> str:
    """The pending release version out of a stored entry ('' if unknown).

    A legacy plain-signature entry names no version at all, so it reads as
    '' — the same as a source that offers a release without naming it. A
    legacy or hand-edited record is also capped here, so the listing never
    echoes an unbounded version even if the file predates the write cap."""
    if isinstance(entry, dict):
        return _capped_version(entry.get('version'))
    return ''


def _update_state_installable(entry):
    """The installability verdict out of a stored entry (None if it has none).

    A legacy plain-signature entry carries no verdict, so it reads as None:
    "unknown", never a promise that the pending release can be installed.
    Only a real boolean passes through — the same normalisation the app
    object's own `installable` gets, so a hand-edited or corrupt record
    (a string like "false", a number, a list) can never reach the panel as
    "this can be installed"."""
    if isinstance(entry, dict):
        value = entry.get('installable')
        if isinstance(value, bool):
            return value
    return None


def _runtime_block() -> dict:
    """The additive `runtime` envelope block (ticket #17): best-effort
    runtime capability states, probed ONCE per listing — never per app.
    probe_fuse2 never raises by contract; the guard keeps even a probe
    crash from failing the listing ('unknown', block still present)."""
    try:
        fuse2 = utils.probe_fuse2()
    except Exception:
        logging.exception('fuse2 probe failed')
        fuse2 = utils.FUSE2_UNKNOWN
    return {'fuse2': fuse2}


def _print_table(table: list):
    longest_cols = [
        (max(len(str(row[i])) for row in table) + 3)
        for i in range(len(table[0]))
    ]
    row_format = ''.join(f'{{:<{w}}}' for w in longest_cols)
    for row in table:
        print(row_format.format(*row))


# ------------------------------------------------- running-app grant prompt

def _stdin_is_tty() -> bool:
    """Whether an interactive question can be asked at all. The single
    seam the tests override: they never touch the real terminal."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        # a closed or replaced stdin is not a terminal
        return False


def _ask_close_running(el) -> bool:
    """The update pipeline's ONE interactive question, asked by the
    running-app guard and by nothing else. Anything but an explicit yes
    declines — the safe answer, and the one an abandoned stdin gets."""
    print(f'{el.file_path} is running. Close it and continue the update? '
          f'[y/N] ', end='', file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:
        # the prompt above ended without a newline
        print(file=sys.stderr)
        return False
    except KeyboardInterrupt:
        # Ctrl-C at the question means "no": the caller gets the
        # documented skipped-running result instead of a traceback
        print(file=sys.stderr)
        return False
    return answer.strip().lower() in ('y', 'yes')


def _running_app_prompt(json_output: bool, assume_yes: bool):
    """The `confirm_close` callable for run_update's guard, or None when
    no question may be asked: --json keeps stdout a pure document stream
    (that is why it requires --yes), --yes means "skip any interactive
    question", and without a TTY there is nobody to ask — that path
    returns the skipped-running result, whose message names
    --kill-running."""
    # `_stdin_is_tty` is resolved through the cli facade at CALL time: it
    # is the tests' patch seam (mock.patch.object(cli, '_stdin_is_tty')),
    # and the facade re-export must stay the name the production guard
    # consults (ticket #56).
    from .. import cli
    if json_output or assume_yes or not cli._stdin_is_tty():
        return None
    return _ask_close_running

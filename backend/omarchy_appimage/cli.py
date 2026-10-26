# cli.py — command implementations (stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Ported from src/Cli.py + src/BackgroudUpdatesFetcher.py: the MVP command
# set (integrate / list-installed / remove) plus the update commands
# (list-updates / update / set-update-source / list-update-managers /
# fetch-updates) and the settings commands backing the panel's settings
# page. The background fetcher is a one-shot CLI command here instead of
# an in-process loop (the QML service owns the timer).
#
# Contract: with --json, stdout carries ONLY the JSON document; every
# human-readable line and all logging goes to stderr (PRD §8).

import fnmatch
import json
import logging
import os
import posixpath
import re
import select
import shutil
import sys
import tempfile
import time
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from . import (constants, extractor, janitor, jobs, net, pick_file, procs,
               settings, updaters, utils)
from .extractor import read_desktop_entry_in_place
from .ini_config import Config
from .provider import (AppImageListElement, AppImageUpdateLogic, InternalError,
                       InstalledStatus, resolve_install_names)
from .updaters import UpdateError, UpdateManagerChecker
from .utils import get_file_hash, run_command
from .commands.envelope import (EXIT_ERROR, EXIT_OK, EXIT_USAGE,
                                MAX_PERSISTED_VERSION_CHARS, OperationError,
                                UsageError, VERIFICATIONS, _ask,
                                _ask_close_running, _capped_version,
                                _installable_of, _json_dump, _make_app_json,
                                _print_json, _print_table,
                                _reject_unknown_options, _remove_flag_value,
                                _resolve_target, _running_app_prompt,
                                _running_pid_count, _runtime_block,
                                _stdin_is_tty,
                                _update_state_installable,
                                _update_state_version, _updates_state_key,
                                _verification_of, appimage_provider)

# public import surface (ticket #56): every moved name stays importable —
# and patchable — as omarchy_appimage.cli.<name>
from .commands.integrate import (_as_url, _auto_set_codeberg_source,
                                 _auto_set_forgejo_source,
                                 _auto_set_github_source,
                                 _auto_set_gitlab_source, _auto_set_source,
                                 _auto_set_static_source,
                                 _classify_integrate_url,
                                 _fetch_integrate_artifact,
                                 _fetch_integrate_artifact_through_source,
                                 _find_name_conflict, _get_file_from_args,
                                 _glob_for_version, _github_release_url_data,
                                 _is_url, _lookup_release_asset,
                                 _preview_destination, _release_api_url,
                                 _release_download_url_data,
                                 _update_source_chosen, _verification_preview,
                                 cmd_integrate, cmd_preview_integrate)

# words accepted for boolean settings / update-manager config values
# (move with the settings verbs, ticket #56)
TRUE_WORDS = ('1', 'true', 'yes', 'on')
FALSE_WORDS = ('0', 'false', 'no', 'off')

USAGE = f"""Usage: python3 main.py [OPTION...]

Manage AppImages (integrate / list / update / remove) for {constants.APP_NAME}.

Commands:
  --integrate <path|url> Integrate an AppImage (extracts .desktop and
                           icon, moves the file into the managed folder).
                           A URL is downloaded and, when possible, its
                           publisher digest is verified: a GitHub release
                           download URL (github.com/<o>/<r>/releases/
                           download/<tag>/<f>) requires the asset's sha256
                           digest; a .zsync URL is checked against the
                           control file's SHA-1. GitLab / Codeberg /
                           Forgejo release URLs and plain https URLs are
                           installed with reduced assurance (AppImage
                           magic + advertised size) and wired to the
                           matching update source
  --preview-integrate <p> Show what --integrate would install (source
                           kind, app name/version, destination, how the
                           artifact will be verified) without installing,
                           downloading or executing anything — an
                           offline, advisory preview document
  --list-installed         List integrated apps
  --remove <target>        Trash an AppImage, its .desktop file and its
                           icon; <target> may be the AppImage path, the
                           desktop id (e.g. neovim.desktop) or the app name
                           (a stale record whose .desktop file is gone is
                           removed by its desktop id too)
  --list-updates           Check every installed app that has an update
                           source for a new release
  --update <target>        Download and install an update (--keep-both
                           keeps the old version, --force updates even
                           while the app is running, --kill-running closes
                           the running app first, --async runs the
                           whole update as a detached background job and
                           reports `job-started` immediately; the job's
                           progress is published as a JSON document
                           under $XDG_STATE_HOME)
  --set-update-source ...  Set a custom update source:
                           <target> --manager <name> [key=value ...]
                           (--unset removes it again)
  --get-update-source ...  Show the configured update source of <target>
                           (--json carries the manager and its config)
  --list-update-managers   List the available update managers and the
                           config keys each one accepts
  --fetch-updates          One-shot background update check; sends a
                           desktop notification when something is new
                           (used by the panel's timer service)
  --pick-file              Open a native single-file, AppImage-filtered
                           file dialog in a child process (desktop portal
                           first, zenity fallback) and print the picked
                           path. A crashed or stuck dialog yields a typed
                           error, never a hang; with no dialog backend
                           available the result is "unavailable" and the
                           panel keeps the path field
  --settings               Show the current settings
  --set-setting <k>=<v>    Change one setting (e.g.
                           --set-setting update_check_enabled=false)

Options:
  --json                   Machine-readable JSON on stdout (requires --yes
                           for --integrate/--remove/--update)
  --yes | -y               Skip any interactive question
  --replace                On integrate name conflict, replace the already
                           integrated app instead of keeping both
  --keep-both              On --update, install the new version next to
                           the old one instead of replacing it
  --force                  On --update, proceed even while the app runs
  --kill-running           On --update, close the running app first (asks
                           on a terminal) and then update
  --delete                 Delete files permanently instead of trashing
  --help | -h              Show this help

Exit codes:
  0  success
  1  operation error (not installed, extraction failed, ...)
  2  usage error (unknown option, missing argument, ...)

Managed folder: {constants.DEFAULT_APPIMAGES_FOLDER} (configurable via
{os.path.relpath(constants.settings_path(), constants.xdg_config_home())}).
XDG_DATA_HOME / XDG_CONFIG_HOME are honoured."""


# ------------------------------------------------------------ not-yet-moved
# (temporary: these travel to commands/integrate.py, commands/checks.py and
# commands/settings.py in the following commits of ticket #56)

def _parse_bool_word(raw: str, what: str) -> bool:
    word = str(raw).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise UsageError(f'{what} is not a boolean value, allowed values: '
                     f'{"/".join(TRUE_WORDS + FALSE_WORDS)}')


def _update_signature(manager) -> str:
    """What changed about an update between two background checks."""
    return (str(manager.available_version or '') + '|'
            + str(manager.download_size or ''))


def _update_state_entry(manager) -> dict:
    """One app's remembered check result (issue #39/#43/#53): the signature
    that drives notifications, the installability verdict so a later
    listing can warn about an update that cannot be installed, the
    AppImage path so a later sweep can tell "this app is gone" from "this
    sweep could not list it" — the listing silently skips an app whose
    desktop entry is momentarily unreadable (VS Code forks rewrite their
    own entry on first launch), and forgetting its signature would
    re-notify for a release already seen — and the pending release version
    so the listing can name what it waits for."""
    entry = {'signature': _update_signature(manager),
             'installable': manager.installable()}
    el = getattr(manager, 'el', None)
    path = getattr(el, 'file_path', '') or ''
    if path:
        entry['path'] = path
    # the version the source named ('' when it names none): the record
    # says a release is pending, and this says which one. Capped at
    # MAX_PERSISTED_VERSION_CHARS so the durable record itself stays
    # bounded — --list-installed echoes it on every poll.
    entry['version'] = _capped_version(manager.available_version)
    return entry


def _update_state_signature(entry):
    """The signature out of a stored entry.

    Legacy files map an app straight to the signature string; newer ones map
    it to a record. Both are read, so upgrading never loses the dedupe."""
    if isinstance(entry, dict):
        return str(entry.get('signature', ''))
    return str(entry or '')


# ----------------------------------------------------------------- commands

def cmd_list_installed(args: list) -> int:
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args
    # janitor sweep (ticket 03): fossils — terminal/interrupted job
    # documents past the acknowledgement window and orphan update temp
    # dirs past the grace window — go away on every listing. THROTTLED:
    # the panel polls this every 10 s and each sweep is an unbounded
    # listdir + stat walk of the shared temp dir in this process's
    # foreground (the job-start hook in _cmd_update_async stays
    # unthrottled). Best-effort by contract: never raises. The icon sweep
    # is handed the managed folder's .icons: an icon installed by a
    # pre-sanitiser plugin version is still decoded by Panel.qml on every
    # open, and provider.py only unlinks the stem it is replacing.
    janitor.sweep_throttled(
        icons_dir=os.path.join(settings.appimages_folder(), '.icons'))
    apps = appimage_provider.list_installed()
    # ONE process table + one mounts read for the whole listing (ticket
    # #41): the panel polls this every 10 s, so the cost of a listing must
    # not grow with the number of installed apps. Every running answer and
    # every process count below reads through this one scan.
    scan = procs._cached_probe()

    if json_output:
        # The remembered verdict of the last check (issue #53): read ONCE
        # for the whole listing (the panel polls this every 10 s) and
        # matched per app by the record's own key.
        state = settings.load_updates_state()
        table = []
        for a in apps:
            # check_url_for_app is local-only (apps.ini config + the
            # embedded .upd_info ELF section) and fills the manager /
            # embedded_source fields; available_version / download_size
            # stay null until an actual check runs (--list-updates).
            table.append(_make_app_json(
                a, manager=UpdateManagerChecker.check_url_for_app(a),
                probe=scan, pending=state.get(_updates_state_key(a))))
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'installed': table,
            # additive envelope key (schema_version stays 1): best-effort
            # runtime capability states for the panel's launch-failure
            # banner — probing never errors the call
            'runtime': _runtime_block(),
        })
        return EXIT_OK

    if not apps:
        print('No AppImages integrated yet')
        return EXIT_OK

    rows = []
    for a in apps:
        running = ' (running)' if appimage_provider.is_app_running(a, probe=scan) else ''
        rows.append([a.name + running,
                     a.version or 'Not specified',
                     a.file_path])

    _print_table(rows)
    return EXIT_OK


def cmd_remove(args: list) -> int:
    _reject_unknown_options(args, {'--json', '--yes', '-y', '--delete'})
    json_output = '--json' in args
    assume_yes = ('-y' in args) or ('--yes' in args)
    force = '--delete' in args

    if json_output and not assume_yes:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'error': '--json mode requires --yes (no interactive prompts)',
        })
        return EXIT_USAGE

    positional = _remove_flag_value(args, ['--json', '--yes', '-y',
                                           '--delete'])
    if not positional:
        raise UsageError('missing argument: --remove <name-or-desktop-id>')

    el = _resolve_target(positional[0])

    # Issue #46: removal during that app's update job is refused here,
    # by the module that owns the job, and not only by the panel's
    # button. A job that cannot name a document cannot exist for this
    # app, so an unusable id simply has nothing to refuse.
    try:
        job_id = _job_desktop_id(el)
    except OperationError:
        job_id = ''
    if job_id:
        active = jobs.active_job(job_id)
        if active is not None:
            raise OperationError(
                f'{el.file_path} is being updated by a running job '
                f'(pid {active.get("pid")}); refusing to remove it')
        # A synchronous --update writes no job document, so the document
        # check alone would let this command uninstall the file it is
        # replacing; ask the claim itself (issue #46).
        if jobs.claim_held(job_id):
            raise OperationError(
                f'{el.file_path} is being updated right now; refusing to '
                'remove it')

    q = 'Do you really want to delete this AppImage?'
    if force:
        q += ' This action is irreversible'

    if not assume_yes:
        ans = _ask(f'{q} (y/N)', ['y', 'Y', 'n', 'N'])
        if ans.lower() != 'y':
            return EXIT_OK

    appimage_provider.uninstall(el, force_delete=force)

    message = f'{el.file_path} was removed successfully'
    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'removed',
            'message': message,
            'app': _make_app_json(el),
        })
    else:
        print(message)
    return EXIT_OK


# ------------------------------------------------------- update commands

def _source_was_consulted(manager) -> bool:
    """Did the check actually reach the source?

    A False verdict means "no newer release" only when the source could
    be consulted. A plain-URL static source answers False when its remote
    size is unknown — the source could not be reached — and that is not
    the same answer as "the installed file is the newest one". Reading
    such a False as a definite verdict would retire a pending release
    nobody ever looked at, and the pending state would keep appearing
    and disappearing (issue #43).

    The evidence is what the source told us: the release it offers
    (available_version) or that release's size (download_size), or a
    digest-backed comparison (a zsync source compares the publisher's
    control file with the installed file before it answers False)."""
    try:
        if manager.available_version is not None:
            return True
        if manager.download_size is not None:
            return True
        return manager.verification_method() == 'zsync-sha1'
    except Exception:
        logging.exception('could not read the update source state')
        return False


def _check_updates():
    """Every installed app's update verdict, plus the state keys of every
    installed app the walk saw.

    Each app's outcome is one of:
      'update'  — the source reported a newer release;
      'current' — the source reported that there is no newer release
                  (an app with no update source at all is current: there
                  is nothing to wait for);
      'unknown' — the verdict is not determinable (the check raised, it
                  answered None, or the source could not be consulted).

    Returned as (element, manager, outcome) triples; `manager` is None
    for an app without an update source. Ported from the check loop of
    upstream Cli.list_updates: per-app errors are logged and skipped so
    one broken source cannot hide the others. Callers decide on
    connectivity first (net.check_internet)."""
    outcomes = []
    installed_keys = set()
    for el in appimage_provider.list_installed():
        installed_keys.add(_updates_state_key(el))
        manager = UpdateManagerChecker.check_url_for_app(el)
        if not manager:
            outcomes.append((el, None, 'current'))
            continue

        logging.debug('checking app %s with %s', el.file_path, manager.name)
        try:
            status = manager.is_update_available()
        except Exception:
            logging.exception('update check failed for %s', el.file_path)
            outcomes.append((el, manager, 'unknown'))
            continue

        # tri-state: only a definite True counts (None = not determinable)
        if status is True:
            # a verdict exists, so installability is reportable (#39)
            manager.checked = True
            outcomes.append((el, manager, 'update'))
        elif status is False and _source_was_consulted(manager):
            outcomes.append((el, manager, 'current'))
        else:
            outcomes.append((el, manager, 'unknown'))
    return outcomes, installed_keys


def _merge_updates_state(state: dict, outcomes: list,
                          installed_keys: set) -> dict:
    """The record after a check walk (issue #43/#53).

    MERGE, never replace: an app the walk did not see keeps its entry
    while the file it names still exists (the listing silently skips an
    app whose desktop entry is momentarily unreadable — an invisible app
    is not an uninstalled one), and an app whose verdict was not
    determinable keeps what the last successful check learned. Only a
    definite verdict changes the record: a newer release writes a fresh
    entry, "no newer release" retires it."""
    remembered = {}
    for key, value in state.items():
        if key in installed_keys:
            continue
        # Not listed: keep the entry while the file it names still exists
        path = value.get('path', '') if isinstance(value, dict) else ''
        if path and os.path.exists(path):
            remembered[key] = value
    for el, manager, outcome in outcomes:
        key = _updates_state_key(el)
        if outcome == 'update':
            remembered[key] = _update_state_entry(manager)
        elif outcome == 'current':
            remembered.pop(key, None)
        elif key in state:
            # the verdict was not determinable: keep what the last
            # successful check learned (issue #43)
            remembered[key] = state[key]
    return remembered


def _save_updates_state(state: dict) -> bool:
    """Write the update record. Never fatal: a failed write is logged and
    the caller decides what a stale record means (a sweep still exits 0)."""
    try:
        settings.save_updates_state(state)
        return True
    except Exception as e:
        logging.warning('saving updates state failed: %s', e)
        return False


def _retire_update_verdict(el):
    """Drop this app's pending verdict from the record (issue #50).

    The record owns the notification marker, the badge count and the
    Update button, so deleting the entry is what makes the pending state
    settle. Called when the truth moved: the update was applied, or the
    update pipeline's own availability check found nothing newer to
    install. Best-effort — a failed write only leaves a stale marker for
    the next check to clear."""
    try:
        state = settings.load_updates_state()
        key = _updates_state_key(el)
        if key not in state:
            return
        del state[key]
        settings.save_updates_state(state)
    except Exception as e:
        logging.warning('could not retire the pending verdict: %s', e)


def cmd_list_updates(args: list) -> int:
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args

    if not net.check_internet():
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'error',
                'error': 'Internet connection not available',
            })
        else:
            print('Internet connection not available', file=sys.stderr)
        return EXIT_ERROR

    outcomes, installed_keys = _check_updates()
    updates = [(el, manager) for el, manager, outcome in outcomes
               if outcome == 'update']

    # The record is the backend's memory of the last verdict (issue
    # #53): both check verbs write it, so a later listing reports the
    # pending release without asking the network again. A failed write
    # is logged, never fatal — this verb keeps its exit codes.
    state = settings.load_updates_state()
    remembered = _merge_updates_state(state, outcomes, installed_keys)
    _save_updates_state(remembered)

    if json_output:
        scan = procs._cached_probe()
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'updates': [_make_app_json(
                el, manager=manager, probe=scan,
                pending=remembered.get(_updates_state_key(el)))
                for el, manager in updates],
        })
        return EXIT_OK

    if not updates:
        print('No updates available')
        return EXIT_OK

    rows = [[f'{el.name} [Update available, {manager.name}]', el.file_path]
            for el, manager in updates]
    _print_table(rows)
    return EXIT_OK


# ------------------------------------------------------------ update core

class UpdateResult:
    """What run_update decided. Callers (cmd_update here, the async job
    runner below) turn it into output; the core itself never writes
    stdout. `status` uses the CLI's update result values:
    'skipped-running' | 'up-to-date' | 'updated' — anything else fails
    closed (InternalError) in both output paths."""

    __slots__ = ('status', 'el', 'manager', 'message', 'downloaded_bytes',
                 'force_quit')

    def __init__(self, status: str, el, manager=None, message: str = '',
                 downloaded_bytes: int = 0, force_quit: dict = None):
        self.status = status
        # the element to report: the old one for skipped-running /
        # up-to-date, the freshly installed one for updated
        self.el = el
        self.manager = manager
        # human-readable message for skipped-running / updated ('' for
        # up-to-date: its plain-mode line is derived from el.file_path)
        self.message = message
        self.downloaded_bytes = downloaded_bytes
        # {"count": int, "sigkill": bool} when the running-app grant
        # actually closed something (None otherwise); the sync --json
        # result never carries it — it only reaches the job document
        self.force_quit = force_quit


class _ManagerStageProxy:
    """Read-only view over an update manager that reports stage changes
    for the async job document: `on_stage('replacing')` fires when
    download() — whose tail performs the digest verification — returns,
    i.e. when only the install step of update_from_url remains. Constant
    structure: nothing is ever written to the real manager, the pipeline
    owns the view."""

    def __init__(self, manager, on_replacing):
        self._manager = manager
        self._on_replacing = on_replacing

    def __getattr__(self, name):
        return getattr(self._manager, name)

    def fetch(self, dest_dir: str, policy: str, progress_cb=None,
              is_appimage=None) -> str:
        result = self._manager.fetch(dest_dir, policy, progress_cb,
                                     is_appimage)
        if self._on_replacing is not None:
            try:
                self._on_replacing()
            except Exception:
                logging.exception('stage callback failed')
        return result


def _close_running_app(file_path: str, stage: str, probe=None,
                       sink: dict = None) -> dict:
    """procs.quit_app plus the job document's additive record of it.

    Returns {'count': n, 'sigkill': bool} for the processes that were
    actually closed, or None when nothing was signalled (not running any
    more, or never was).

    A pid that survived the round — another user's process, a signal the
    app blocks, a SIGKILL that did not take — is never a licence to swap
    the file underneath it: that raises OperationError naming the pid(s)
    and the stage. The record is written to `sink` FIRST, so a job that
    closed the app and then failed still says so in its error document.

    `sink` is the caller's dict (run_update's force-quit sink); passing it
    keeps the record visible to the failure path without a second scan."""
    report = procs.quit_app(file_path, probe=probe)

    record = None
    if report['signalled']:
        logging.info('%d process(es) closed before %s: %s',
                     len(report['signalled']), stage, report['outcomes'])
        record = {'count': len(report['signalled']),
                  'sigkill': bool(report['killed_with_sigkill'])}

    if sink is not None:
        sink['force_quit'] = _merge_force_quit(sink.get('force_quit'),
                                               record)

    if report['survived']:
        pids = ', '.join(str(pid) for pid in report['survived'])
        raise OperationError(
            f'{file_path} is still running before {stage}: process(es) '
            f'{pids} could not be closed; refusing to replace the file')
    return record


def _merge_force_quit(current: dict, extra: dict):
    """Fold a second guard's record into the first: `count` is every
    process the job signalled, `sigkill` if any of them needed it."""
    if extra is None:
        return current
    if current is None:
        return dict(extra)
    return {'count': current['count'] + extra['count'],
            'sigkill': current['sigkill'] or extra['sigkill']}


def run_update(target: str, keep_both: bool = False,
               force: bool = False, progress_cb=None,
               on_stage=None, kill_running: bool = False,
               confirm_close=None, probe=None,
               force_quit_sink: dict = None,
               claim_owned: bool = True) -> UpdateResult:
    """The update lifecycle, entered through its single-flight claim.

    Issue #46: the claim lives here, not in a caller, so a second update
    of the same app is refused the same way whoever starts it — the
    panel's background job and a terminal `--update` both end up in the
    one rule below. `claim_owned=False` is the async grandchild's door:
    it inherits the flock through the log description its spawner opened
    and kept alive across the fork, so taking it again would conflict
    with itself.
    """
    if not claim_owned:
        return _run_update_body(
            target, keep_both=keep_both, force=force,
            progress_cb=progress_cb, on_stage=on_stage,
            kill_running=kill_running, confirm_close=confirm_close,
            probe=probe, force_quit_sink=force_quit_sink)

    el = _resolve_target(target)
    # A desktop id the job store cannot name a document for cannot have a
    # job either, so the synchronous path claims nothing for it — the
    # documented sync `--update` on such an app keeps working.
    try:
        job_id = _job_desktop_id(el)
    except OperationError:
        job_id = ''
    claim_fd = None
    if job_id:
        try:
            claim_fd = jobs.open_job_log_fd(job_id)
        except BlockingIOError:
            raise OperationError(
                f'an update is already running for {el.file_path}; '
                'refusing to start a second one')
    try:
        return _run_update_body(
            target, keep_both=keep_both, force=force,
            progress_cb=progress_cb, on_stage=on_stage,
            kill_running=kill_running, confirm_close=confirm_close,
            probe=probe, force_quit_sink=force_quit_sink)
    finally:
        if claim_fd is not None:
            os.close(claim_fd)


def _run_update_body(target: str, keep_both: bool = False,
                     force: bool = False, progress_cb=None,
                     on_stage=None, kill_running: bool = False,
                     confirm_close=None, probe=None,
                     force_quit_sink: dict = None) -> UpdateResult:
    """The shared update pipeline: resolve target → update-source check →
    availability check → running guard → download to temp → verify →
    atomic replace → scoped temp-dir cleanup → pending-notification drop.

    Plain function: no argv parsing, no stdout writes. Raises the same
    exceptions the CLI reported before this was extracted (OperationError
    for an unresolvable target / missing update source, InternalError /
    net.NetworkError / updater errors from download & install) — main()
    maps them to error documents and exit codes exactly as before, and
    cmd_update adds no behaviour of its own beyond the argv check.
    Ticket 02's async job runner calls this too, so the sync and async
    paths cannot drift.

    Hooks (both optional, used by the async runner only):
      progress_cb(fraction) — download progress from net.download_to_file;
        without it the fraction is only debug-logged (sync behaviour).
      on_stage(stage, manager=None) — 'downloading' fires before the
        download starts (with the checked manager, so its declared
        download_size is available), 'replacing' after the download and
        its digest verification returned (via _ManagerStageProxy).

    Running-app guard (ticket 26). The availability check runs before
    anything can touch a process, so an update that does not exist never
    costs the user their running app (status `up-to-date`, no process
    touched, no question asked, no force_quit). The one answer that needs
    no knowledge of the update — "it is running and nobody agreed to
    close it" — is decided even earlier, so the documented
    skipped-running refusal still arrives on a machine that cannot reach
    the update source. When an update DOES exist, three modes decide what
    happens, and only two of them signal anything:
      * neither a grant nor consent → the `skipped-running` result,
        untouched, and no process is signalled;
      * `kill_running=True` (--kill-running) → an unconditional grant for
        the whole job: procs.quit_app runs once here, before the
        download (fail fast — downloading for an app that cannot be
        replaced wastes the user's bandwidth), and once immediately
        before the atomic replace, so a process relaunched during the
        download is closed too. A process that survives either round is
        an OperationError: replacing the file under a running app is
        exactly what the banner must never lie about;
      * `confirm_close(el) -> bool` → the interactive path: the same
        grant, but only when the caller's question was answered yes. The
        prompt lives in cmd_update; the core never writes stdout.

    `keep_both` installs into a NEW file: os.replace never touches the
    running app's path, so neither quit call runs — --kill-running is
    accepted and ignored in that combination (a running app is not in
    the way).

    `force` keeps its documented meaning — bypass the guard silently and
    replace under a running app without closing it — and never signals.
    When both are given the explicit close request wins.

    `probe` is procs' seam: it is the process view and signal sender for
    both quit calls (None = the production probe). `force_quit_sink`, when
    given, receives the job's force-quit record as soon as it is known, so
    a failure AFTER the app was closed (which raises, so there is no
    result) can still publish it.
    """
    el = _resolve_target(target)

    manager = UpdateManagerChecker.check_url_for_app(el)
    if not manager:
        raise OperationError('No update method was found for this AppImage '
                             '(set one with --set-update-source)')

    sink = force_quit_sink if force_quit_sink is not None else {}
    force_quit = None
    # C: --keep-both writes beside the running app, never over it, so that
    # combination asks nothing, closes nothing and never skips.
    replaces_in_place = not keep_both
    granted = kill_running and replaces_in_place

    # The running-app answer, asked once (one process scan, not two):
    # only a replace-in-place without a grant needs it.
    needs_consent = replaces_in_place and not kill_running and not force
    running = (needs_consent and appimage_provider.is_app_running(el)) or False
    # A: the availability decision comes before anything can touch the
    # process. Closing (or asking to close) the running app for an update
    # that turns out not to exist is the worst possible trade — the user
    # loses the app, or answers a pointless question, and gains nothing.
    # tri-state: None (state not determinable) still attempts the download
    try:
        available = manager.is_update_available()
    except Exception:
        # The check needs the source; a refusal that does not need it must
        # still be answerable when the source cannot be reached. Only the
        # non-interactive no-grant case (the answer is the same with or
        # without an update) degrades to skipped-running; everything else
        # keeps raising exactly as before.
        if running and confirm_close is None:
            return UpdateResult(
                status='skipped-running', el=el, manager=manager,
                message=(f'{el.file_path} was skipped because the '
                         'application is running; pass --kill-running to '
                         'close it and update, or --force to replace it in '
                         'place'))
        raise
    if available is False:
        # the pipeline's own availability check just proved there is
        # nothing newer to install, so the pinned pending verdict is
        # stale: retire it here rather than waiting for the next sweep
        # (issue #50 — a definite verdict changes the record)
        _retire_update_verdict(el)
        return UpdateResult(status='up-to-date', el=el, manager=manager)

    if running:
        # the user said yes: same grant as --kill-running, and only now,
        # with an update actually available; a caller that cannot ask
        # (non-TTY, --json, --yes) refuses here, after the check
        if confirm_close is None or not confirm_close(el):
            return UpdateResult(
                status='skipped-running', el=el, manager=manager,
                message=(f'{el.file_path} was skipped because the '
                         'application is running; pass --kill-running to '
                         'close it and update, or --force to replace it in '
                         'place'))
        granted = True

    if granted:
        force_quit = _close_running_app(el.file_path, 'the update start',
                                        probe, sink)

    if on_stage is not None:
        on_stage('downloading', manager)
        manager = _ManagerStageProxy(manager, lambda: on_stage('replacing'))

    logging.debug('downloading update with %s', manager.name)

    if progress_cb is None:
        def progress_cb(fraction: float):
            logging.debug('download: %d%%', round(fraction * 100))

    before_replace = None
    if granted:
        appimage_path = el.file_path

        def before_replace():
            # the grant covers the whole job: whatever was (re)started
            # while the download ran is closed before the replace, so a
            # relaunched app cannot hold the old inode open behind us
            nonlocal force_quit
            force_quit = _merge_force_quit(
                force_quit,
                _close_running_app(appimage_path, 'the atomic replace',
                                   probe, sink))

    temp_dirs_before = extractor.temp_dirs_snapshot()
    try:
        new_el = appimage_provider.update_from_url(
            manager, el, keep_both=keep_both, progress_cb=progress_cb,
            before_replace=before_replace)
    finally:
        # the temp-dir lifecycle is core-owned: update_from_url registers
        # its download dir via extractor.new_temp_dir() (and the install
        # step registers extraction dirs) — the scoped cleanup removes
        # exactly the dirs THIS call added and leaves the rest of the
        # process-global registry alone. The global wipe stays where it
        # belongs: main.py's process-exit finally-block as a backstop.
        for path in extractor.temp_dirs_snapshot():
            if path not in temp_dirs_before:
                extractor.remove_temp_dir(path)

    # the update was applied: retire the app's pending verdict (issue
    # #50). The one record owns the notification marker, the badge count
    # and the Update button, so dropping the entry is what makes the
    # pending state settle — and it keeps the next --fetch-updates from
    # re-notifying for the (now installed) release
    _retire_update_verdict(el)

    return UpdateResult(
        status='updated', el=new_el, manager=manager,
        message=f'{el.file_path} was updated successfully',
        downloaded_bytes=new_el.size or 0, force_quit=force_quit)


def cmd_update(args: list) -> int:
    """CLI wrapper over run_update: argv/flag parsing plus the result's
    JSON document / plain line and exit code (byte-identical to the
    pre-extraction inline pipeline). --async detaches the pipeline into a
    double-forked grandchild and reports `job-started` instead."""
    _reject_unknown_options(
        args, {'--json', '--yes', '-y', '--keep-both', '--force',
               '--kill-running', '--async'})
    json_output = '--json' in args
    assume_yes = ('-y' in args) or ('--yes' in args)
    keep_both = '--keep-both' in args
    force = '--force' in args
    kill_running = '--kill-running' in args
    async_job = '--async' in args

    if json_output and not assume_yes:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'error': '--json mode requires --yes (no interactive prompts)',
        })
        return EXIT_USAGE

    positional = _remove_flag_value(
        args, ['--json', '--yes', '-y', '--keep-both', '--force',
               '--kill-running', '--async'])
    if not positional:
        raise UsageError('missing argument: --update <name-or-desktop-id>')

    if async_job:
        return _cmd_update_async(positional[0], keep_both=keep_both,
                                 force=force, kill_running=kill_running,
                                 json_output=json_output,
                                 assume_yes=assume_yes)

    result = run_update(positional[0], keep_both=keep_both, force=force,
                        kill_running=kill_running,
                        confirm_close=_running_app_prompt(json_output,
                                                          assume_yes))

    # the record owns the pending verdict (issue #53): the document
    # reports what it says about this app, so it never contradicts the
    # listing (a successful update retired the entry, so it reads as no
    # pending release)
    pending = settings.load_updates_state().get(
        _updates_state_key(result.el))

    if result.status == 'skipped-running':
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'skipped-running',
                'message': result.message,
                'app': _make_app_json(result.el, manager=result.manager,
                                     pending=pending),
            })
        else:
            print(result.message)
        return EXIT_OK

    if result.status == 'up-to-date':
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'up-to-date',
                'app': _make_app_json(result.el, manager=result.manager,
                                     pending=pending),
            })
        else:
            print(f'{result.el.file_path} is up to date')
        return EXIT_OK

    if result.status != 'updated':
        # fail closed: a future typo'd status must never print a wrong
        # document (same verdict the async runner's mapper reaches)
        raise InternalError(f'unknown update result status: '
                            f'{result.status!r}')

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'updated',
            'message': result.message,
            'app': _make_app_json(result.el, manager=result.manager,
                                     pending=pending),
            'downloaded_bytes': result.downloaded_bytes,
        })
    else:
        print(result.message)
    return EXIT_OK


# ------------------------------------------------------- async job runner

_JOB_HANDSHAKE_TIMEOUT_SECONDS = 10


def _cmd_update_async(target: str, keep_both: bool, force: bool,
                      json_output: bool, kill_running: bool = False,
                      assume_yes: bool = False) -> int:
    """Parent side of --update --async: validate the target (local name/id
    resolution only), take the job's atomic claim, refuse when a job is
    running for the app, then spawn the detached grandchild (see
    _spawn_update_job) and report `job-started` immediately. The parent
    never runs the pipeline itself and never resolves the update manager:
    that is run_update's first step inside the grandchild, so a missing
    or broken source surfaces as a terminal `error` job document instead
    of a second, drifting manager resolution here.

    --async is NOT implicitly non-interactive any more: on a TTY the
    close-the-running-app question is asked HERE, in the parent, before
    anything is spawned (the grandchild's stdin is /dev/null, so a job
    that asked would have to publish skipped-running after a
    `job-started` that already told the user the update was under way).
    A yes becomes the grandchild's --kill-running grant."""
    el = _resolve_target(target)
    # fail closed BEFORE any side effect (no sweep, no fork): the id must
    # be able to name a job file, or this path cannot honour its contract
    desktop_id = _job_desktop_id(el)

    # D: the question, still before any side effect — a decline leaves no
    # claimed log, no sweep and no grandchild behind.
    if not kill_running and not force and not keep_both:
        ask = _running_app_prompt(json_output, assume_yes)
        if ask is not None and appimage_provider.is_app_running(el):
            if not ask(el):
                message = (f'{el.file_path} was skipped because the '
                           'application is running; pass --kill-running to '
                           'close it and update, or --force to replace it '
                           'in place')
                if json_output:
                    _print_json({
                        'schema_version': constants.JSON_SCHEMA_VERSION,
                        'result': 'skipped-running',
                        'message': message,
                        'app': _make_app_json(el),
                    })
                else:
                    print(message)
                return EXIT_OK
            # a yes is the whole job's grant, exactly like --kill-running
            kill_running = True

    # janitor sweep (ticket 03), parent-side only — never in the
    # grandchild: stale terminal/interrupted documents age out and orphan
    # update temp dirs are reclaimed before a new job is spawned.
    # Best-effort by contract: sweep() never raises.
    janitor.sweep()

    # The claim comes first, and it is atomic: two starts racing before
    # either job document exists would otherwise both pass the active-job
    # check and both write this log (TOCTOU). open_job_log_fd also owns
    # the only legal truncation of an over-cap log, so a start that turns
    # out to be refused cannot wipe a live job's log.
    try:
        log_fd = jobs.open_job_log_fd(desktop_id)
    except BlockingIOError:
        return _refuse_second_job(desktop_id, json_output)

    try:
        active = jobs.active_job(desktop_id)
        if active is not None:
            return _refuse_second_job(desktop_id, json_output, active)

        try:
            pid, job_path, log_path = _spawn_update_job(
                desktop_id, target, keep_both=keep_both, force=force,
                log_fd=log_fd, kill_running=kill_running)
        except OSError as e:
            raise OperationError(f'could not start the update job: {e}')
    finally:
        # single owner: the grandchild inherited the description (and the
        # flock with it) through the fork, so this copy is done
        os.close(log_fd)

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'job-started',
            'desktop_id': desktop_id,
            'job_path': job_path,
            'log_path': log_path,
            'pid': pid,
        })
    else:
        print(f'Update job started for {el.file_path} '
              f'(pid {pid}, desktop id {desktop_id}); '
              f'job document: {job_path}')
    return EXIT_OK


def _job_desktop_id(el) -> str:
    """The app's job key, with the fail-closed precondition attached: an
    id the job store cannot turn into a file name (a legal .desktop
    filename may hold a space or a non-ASCII char) is an OperationError,
    i.e. the contract's error shape — never a ValueError escaping to
    main()'s catch-all as a generic "unexpected error" + traceback. Sync
    --update accepts such an id, so the refusal has to name the reason."""
    desktop_id = _updates_state_key(el)
    if not jobs.is_usable_desktop_id(desktop_id):
        raise OperationError(
            f'{el.file_path} cannot be updated in the background: its '
            f'desktop id {desktop_id!r} is not usable as a job file name '
            f'(allowed: letters, digits, ".", "_" and "-", '
            f'starting with a letter or digit)')
    return desktop_id


def _refuse_second_job(desktop_id: str, json_output: bool,
                       active: dict = None) -> int:
    """The --async refusal, one shape for both gates (the atomic claim
    and the active-document check). Nothing of the running job is
    touched: its document is not rewritten and its log is not opened for
    writing."""
    pid = (active or {}).get('pid')
    if pid:
        message = (f'an update job is already active for {desktop_id} '
                   f'(pid {pid}); refusing to start a second one')
    else:
        message = (f'an update job is already active for {desktop_id}; '
                   f'refusing to start a second one')
    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'desktop_id': desktop_id,
            'error': message,
        })
    else:
        print(f'Error: {message}', file=sys.stderr)
    return EXIT_ERROR


def _job_entrypoint(desktop_id: str, target: str, keep_both: bool,
                    force: bool, kill_running: bool = False) -> int:
    """The detached grandchild's body: run the whole update pipeline and
    publish one job document per phase (jobs.JobWriter). Never raises;
    returns a process exit code (the fork site os._exit()s with it).
    In-process callers (tests) use the same entrypoint, so the sync and
    async pipelines cannot drift."""
    writer = jobs.JobWriter(desktop_id)
    writer.start_heartbeat()
    # G: run_update raises instead of returning a result when it fails, so
    # the only way the error document can carry the force-quit record is
    # this sink — filled the moment the app is actually closed.
    sink = {}
    try:
        try:
            result = run_update(target, keep_both=keep_both, force=force,
                                kill_running=kill_running,
                                progress_cb=writer.progress_cb,
                                on_stage=writer.stage,
                                force_quit_sink=sink, claim_owned=False)
        except Exception as e:
            # the traceback lands in the bounded per-job log (stderr is
            # redirected there), never on stdout
            logging.exception('update job for %s failed', desktop_id)
            writer.finish_error(str(e) or type(e).__name__,
                                force_quit=sink.get('force_quit'))
            return EXIT_ERROR
        return _finish_job_document(writer, result)
    finally:
        writer.stop_heartbeat()
        # process-exit backstop for the grandchild: run_update's scoped
        # cleanup removed what the pipeline registered; whatever is left
        # in the registry dies with this process here
        extractor.cleanup_temp_dirs()


def _finish_job_document(writer, result: UpdateResult) -> int:
    """Map a completed run_update to its terminal job document (same
    verdicts, fail closed, as cmd_update's output branches)."""
    if result.status == 'updated':
        writer.finish('updated', message=result.message,
                      new_version=result.el.version,
                      downloaded_bytes=result.downloaded_bytes,
                      force_quit=result.force_quit)
        return EXIT_OK
    if result.status == 'up-to-date':
        # B: belt on top of run_update's ordering — a document that
        # applied nothing must never say the app was closed for it, so
        # `force_quit` is dropped here whatever the result carries
        writer.finish('up-to-date',
                      message=f'{result.el.file_path} is up to date',
                      force_quit=None)
        return EXIT_OK
    if result.status == 'skipped-running':
        writer.finish('skipped-running', message=result.message,
                      force_quit=result.force_quit)
        return EXIT_OK
    writer.finish_error(f'unknown update result status: {result.status!r}')
    return EXIT_ERROR


def _spawn_update_job(desktop_id: str, target: str, keep_both: bool,
                      force: bool, log_fd: int,
                      kill_running: bool = False) -> tuple:
    """Detach the job runner as a grandchild: os.fork() twice (setsid in
    the intermediate, so the grandchild owns no controlling terminal and
    cannot reacquire one), stdio redirected into the size-capped per-job
    log, stdin from /dev/null. No shell, no exec, nothing interpolated —
    the grandchild continues this process image and runs
    _job_entrypoint, then reports its pid back over a pipe so the parent
    can name it in the job-started document. The parent reaps only the
    intermediate; the grandchild is reparented and reaped by init.

    `log_fd` is the caller's already-open (and flock-claimed) job log, not
    something this function opens: the claim has to be held across the
    active-job check and the fork, and only the caller can bracket that.
    Ownership of the fd stays with the caller.

    Returns (grandchild_pid, job_document_path, job_log_path)."""
    devnull_fd = os.open(os.devnull, os.O_RDONLY)
    job_path = jobs.job_document_path(desktop_id)
    log_path = jobs.job_log_path(desktop_id)
    read_fd, write_fd = os.pipe()

    sys.stdout.flush()
    sys.stderr.flush()
    intermediate_pid = os.fork()

    if intermediate_pid == 0:
        # --- intermediate child --------------------------------------
        code = 1
        try:
            os.close(read_fd)
            os.setsid()
            grandchild_pid = os.fork()
        except BaseException:
            try:
                logging.exception('could not fork the update job')
            except Exception:
                pass
            os._exit(code)
        if grandchild_pid == 0:
            # --- grandchild: the detached job ------------------------
            grandchild_code = EXIT_ERROR
            try:
                # write_fd only: the intermediate already closed read_fd
                # before forking, so the grandchild's fd table has no
                # read end to close (a double close here would EBADF and
                # kill the job before its stdio is even redirected)
                os.close(write_fd)
                os.dup2(devnull_fd, 0)
                os.dup2(log_fd, 1)
                os.dup2(log_fd, 2)
                os.close(devnull_fd)
                os.close(log_fd)
                os.chdir('/')
                grandchild_code = _job_entrypoint(
                    desktop_id, target, keep_both=keep_both, force=force,
                    kill_running=kill_running)
            except BaseException:
                try:
                    logging.exception('update job crashed')
                except Exception:
                    pass
                grandchild_code = EXIT_ERROR
            finally:
                os._exit(grandchild_code)
        # report the grandchild pid, then leave: the grandchild is
        # reparented and never reaped by this tree
        try:
            os.write(write_fd, str(grandchild_pid).encode('ascii'))
        except OSError:
            pass
        finally:
            os._exit(0)

    # --- parent ------------------------------------------------------
    os.close(write_fd)
    try:
        data = b''
        deadline = time.monotonic() + _JOB_HANDSHAKE_TIMEOUT_SECONDS
        while len(data) < 32:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([read_fd], [], [], remaining)
            if not ready:
                break
            chunk = os.read(read_fd, 32)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(read_fd)
        os.close(devnull_fd)
    # bounded above by the handshake deadline only via the pipe: the
    # intermediate's post-fork code is close/setsid/fork/write/_exit, so a
    # hang here would require a signal mid-sequence — reap is unblocking-free
    os.waitpid(intermediate_pid, 0)

    try:
        grandchild_pid = int(data.decode('ascii', errors='replace').strip())
    except ValueError:
        grandchild_pid = 0
    if grandchild_pid <= 0:
        raise OperationError('could not start the update job '
                             '(the detached process died before it '
                             'reported its pid)')
    return grandchild_pid, job_path, log_path


def cmd_set_update_source(args: list) -> int:
    _reject_unknown_options(args, {'--json', '--unset', '--manager'})
    json_output = '--json' in args
    do_unset = '--unset' in args

    positional = _remove_flag_value(args, ['--json', '--unset'])
    manager_name = None
    rest = []
    i = 0
    while i < len(positional):
        if positional[i] == '--manager':
            if i + 1 >= len(positional):
                raise UsageError('--manager requires a manager name')
            manager_name = positional[i + 1]
            i += 2
            continue
        rest.append(positional[i])
        i += 1

    targets = [t for t in rest if '=' not in t]
    pairs = [t for t in rest if '=' in t]
    if not targets:
        raise UsageError('missing argument: --set-update-source <target> '
                         '--manager <name> [key=value ...]')
    if len(targets) > 1:
        raise UsageError(f'unexpected argument: {targets[1]!r}')

    el = _resolve_target(targets[0])

    if do_unset:
        Config.delete_app_update_config(el)
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'unset',
                'app': _make_app_json(el),
            })
        else:
            print(f'Update source removed for {el.file_path}')
        return EXIT_OK

    names = ', '.join(m.name for m in UpdateManagerChecker.get_models())
    if not manager_name:
        raise UsageError(f'missing --manager <name> (one of: {names})')

    model = next((m for m in UpdateManagerChecker.get_models()
                  if m.name == manager_name), None)
    if model is None:
        raise UsageError(f'"{manager_name}" is not a valid update manager '
                         f'(one of: {names})')

    manager = model(el=el)
    template = manager.config_template()

    data = {}
    for pair in pairs:
        key, sep, value = pair.partition('=')
        if not sep or not key:
            raise UsageError(f'invalid argument {pair!r} (expected key=value)')
        if key in data:
            raise UsageError(f'duplicate key: {key}')
        data[key] = value

    if set(data.keys()) != set(template.keys()):
        raise UsageError('Missing or invalid update configuration, required '
                         'keys: ' + ', '.join(template.keys()))

    for key, default in template.items():
        if isinstance(default, bool):
            data[key] = _parse_bool_word(data[key], key)

    try:
        manager.validate_config(data)
    except UpdateError as e:
        raise UsageError(str(e))

    Config.set_app_update_config(el, manager.name, data)

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'set',
            'app': _make_app_json(el, manager=manager),
            'manager': manager.name,
            'config': data,
        })
    else:
        print(f'Update source for {el.file_path} set to {manager.name}')
    return EXIT_OK


def cmd_get_update_source(args: list) -> int:
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args

    positional = _remove_flag_value(args, ['--json'])
    if not positional:
        raise UsageError('missing argument: --get-update-source <target>')
    if len(positional) > 1:
        raise UsageError(f'unexpected argument: {positional[1]!r}')

    el = _resolve_target(positional[0])

    # The raw section contents: `manager` names the UpdateManager class,
    # everything else is the stored key=value config (bools already
    # normalized to "true"/"false" strings by set_app_update_config).
    cfg = Config.get_app_update_config(el)
    if not cfg:
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'no-source',
                'app': _make_app_json(el),
            })
        else:
            print(f'No update source configured for {el.file_path}')
        return EXIT_OK

    manager = cfg.get('manager', '')
    config = {key: value for key, value in cfg.items() if key != 'manager'}
    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'ok',
            'manager': manager,
            'config': config,
            'app': _make_app_json(el),
        })
    else:
        pairs = ', '.join(f'{key}={value}' for key, value in sorted(config.items()))
        print(f'Update source for {el.file_path}: {manager}'
              + (f' ({pairs})' if pairs else ''))
    return EXIT_OK


def cmd_list_update_managers(args: list) -> int:
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args
    managers = UpdateManagerChecker.manager_metadata()

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'managers': managers,
        })
        return EXIT_OK

    _print_table([[m['label'], m['name']] for m in managers])
    return EXIT_OK


def cmd_fetch_updates(args: list) -> int:
    """One-shot background update check (F8), ported from upstream
    BackgroudUpdatesFetcher.fetch: never fails hard, and sends a single
    notify-send when updates NEWER than the last check are seen (upstream
    notifies on every run; the signature-based dedupe is this project's
    deviation). The QML timer service invokes this every
    update_check_interval_minutes."""
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args
    offline = not net.check_internet()

    if offline:
        # no hard failure: the service just skips this round
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'updates': [],
                'notified': False,
                'offline': True,
            })
        else:
            print('Internet connection not available', file=sys.stderr)
        return EXIT_OK

    outcomes, installed_keys = _check_updates()
    updates = [(el, manager) for el, manager, outcome in outcomes
               if outcome == 'update']

    state = settings.load_updates_state()
    new_updates = [(el, manager) for el, manager in updates
                   if _update_signature(manager)
                   != _update_state_signature(state.get(_updates_state_key(el)))]

    # The record is written BEFORE the announcement (issue #53): the
    # announcement says "this release is new to you", and it can only be
    # said once the release has been recorded. A failed write means this
    # sweep learned nothing durable, so nothing is announced — until now
    # the announcement came first and the failed write left the old
    # record behind, so the NEXT sweep announced the same release again.
    # A state-file failure must not turn a successful sweep into a failed
    # exit (CONTRACT.md: --fetch-updates always exits 0).
    remembered = _merge_updates_state(state, outcomes, installed_keys)
    written = _save_updates_state(remembered)

    notified = False
    if new_updates:
        if not written:
            logging.warning('not announcing the updates: the update record '
                            'could not be written')
        else:
            body = (f'{len(new_updates)} update(s) available — click the '
                    'AppImage icon in the bar.')
            try:
                result = run_command(
                    ['notify-send', '-a', constants.APP_NAME,
                     '--expire-time=5000', 'AppImage updates available',
                     body],
                    check=False)
                notified = result.returncode == 0
            except Exception as e:
                logging.warning('notify-send failed: %s', e)

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'updates': [_make_app_json(
                el, manager=manager,
                pending=remembered.get(_updates_state_key(el)))
                for el, manager in updates],
            'notified': notified,
            'offline': False,
        })
    else:
        if updates:
            print(f'{len(updates)} update(s) available')
        else:
            print('No updates available')
    return EXIT_OK


# ------------------------------------------------------- settings commands

def cmd_pick_file(args: list) -> int:
    """Open a native single-file AppImage dialog in a child process
    (crash-isolated): the desktop portal first, zenity as fallback. The
    heavy lifting (probe order, deadline, URI validation, typed results)
    lives in pick_file.py; this wrapper only maps the payload to the
    output contract."""
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args
    payload = pick_file.run_pick()
    result = payload.get('result')

    if result == 'picked':
        if json_output:
            _print_json(payload)
        else:
            print(payload['path'])
        return EXIT_OK

    if result in ('cancelled', 'unavailable'):
        # not errors: cancelled is the user's choice, unavailable means
        # the panel keeps today's path-field UX
        if json_output:
            _print_json(payload)
        else:
            print(payload.get('message') or 'File selection cancelled')
        return EXIT_OK

    message = payload.get('error') or 'file selection failed'
    print(f'Error: {message}', file=sys.stderr)
    if json_output:
        _print_json(payload)
    return EXIT_ERROR


def cmd_settings(args: list) -> int:
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args
    data = settings.load_settings()

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'settings': data,
        })
        return EXIT_OK

    for key in sorted(data):
        print(f'{key}: {data[key]}')
    return EXIT_OK


def cmd_set_setting(args: list) -> int:
    _reject_unknown_options(args, {'--json'})
    json_output = '--json' in args

    tokens = [a for a in args if a != '--json']
    if len(tokens) != 1 or '=' not in tokens[0] \
            or tokens[0].startswith('-'):
        raise UsageError('missing argument: --set-setting <key>=<value> '
                         '(see --settings for valid keys)')

    key, sep, raw_value = tokens[0].partition('=')
    valid_keys = ', '.join(sorted(settings._DEFAULTS))
    if key not in settings._DEFAULTS:
        raise UsageError(f'unknown setting {key!r} (valid keys: {valid_keys})')
    if not sep or not raw_value.strip():
        raise UsageError(f'missing value for setting {key!r}')

    if key in ('manage_files_outside_default_folder',
               'move_appimage_on_integration',
               'update_check_enabled'):
        value = _parse_bool_word(raw_value, key)
    elif key == 'update_check_interval_minutes':
        try:
            value = int(raw_value)
        except ValueError:
            raise UsageError(f'{key} must be an integer (minutes)')
        if value < constants.MIN_UPDATE_CHECK_INTERVAL:
            raise UsageError(f'{key} must be >= '
                             f'{constants.MIN_UPDATE_CHECK_INTERVAL} minutes')
    elif key == 'update_check_delay_minutes':
        try:
            value = int(raw_value)
        except ValueError:
            raise UsageError(f'{key} must be an integer (minutes)')
        if value < 0:
            raise UsageError(f'{key} must be >= 0 minutes')
    else:  # appimages_default_folder
        value = raw_value.strip()
        if not value.startswith(('/', '~')):
            raise UsageError(f'{key} must be an absolute path '
                             '(starting with / or ~)')

    data = settings.load_settings()
    data[key] = value
    settings.save_settings(data)

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'set',
            'settings': settings.load_settings(),
        })
    else:
        print(f'{key} set to {value}')
    return EXIT_OK


# -------------------------------------------------------------------- entry

COMMANDS = {
    'integrate': cmd_integrate,
    'preview-integrate': cmd_preview_integrate,
    'list-installed': cmd_list_installed,
    'remove': cmd_remove,
    'list-updates': cmd_list_updates,
    'update': cmd_update,
    'set-update-source': cmd_set_update_source,
    'get-update-source': cmd_get_update_source,
    'list-update-managers': cmd_list_update_managers,
    'fetch-updates': cmd_fetch_updates,
    'pick-file': cmd_pick_file,
    'settings': cmd_settings,
    'set-setting': cmd_set_setting,
}


def main(argv: list) -> int:
    args = argv[1:]

    if not args or args[0] in ('--help', '-h'):
        print(USAGE)
        return EXIT_OK if args else EXIT_USAGE

    command = COMMANDS.get(args[0].lstrip('-'))
    if command is None:
        print(f'Error: unknown option {args[0]!r}\n', file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE

    if '--help' in args[1:]:
        print(USAGE)
        return EXIT_OK

    try:
        return command(args[1:])
    except UsageError as e:
        print(f'Error: {e}', file=sys.stderr)
        print(USAGE, file=sys.stderr)
        if '--json' in args:
            # usage errors keep the same error-document shape as any
            # other failure in --json mode
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'error',
                'error': str(e),
            })
        return EXIT_USAGE
    except (OperationError, InternalError, FileNotFoundError,
            net.NetworkError) as e:
        message = e.message if isinstance(e, InternalError) else str(e)
        print(f'Error: {message}', file=sys.stderr)
        if '--json' in args:
            # in --json mode the caller expects a machine-readable error
            # document on stdout as well (QML shows it in the panel)
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'error',
                'error': message,
            })
        return EXIT_ERROR
    except Exception as e:
        logging.exception('unexpected error')
        print(f'Error: {e}', file=sys.stderr)
        if '--json' in args:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'error',
                'error': str(e),
            })
        return EXIT_ERROR

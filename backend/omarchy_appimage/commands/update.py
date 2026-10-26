# update.py — the --update verb and the shared update pipeline (ticket #56).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# run_update is the single update lifecycle, entered through its
# single-flight claim (the flock on the job log): resolve target →
# update-source check → availability check → running-app guard → download
# to temp → verify → atomic replace → scoped temp-dir cleanup →
# pending-verdict retirement. cmd_update is argv/flag parsing plus the
# result's document; --async detaches the pipeline (commands/jobs.py).
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import logging
import os

from .. import constants, extractor, jobs, procs, settings
from ..provider import InternalError
from ..updaters import UpdateManagerChecker
from .jobs import _job_desktop_id
from .envelope import (EXIT_OK, EXIT_USAGE, OperationError, UsageError, _ask_close_running,
                       _make_app_json, _print_json, _reject_unknown_options,
                       _remove_flag_value, _resolve_target,
                       _running_app_prompt, _updates_state_key,
                       appimage_provider)

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
        # `_cmd_update_async` is resolved through the cli facade at CALL
        # time: it is the tests' patch seam
        # (mock.patch.object(cli, '_cmd_update_async')), and the facade
        # re-export must stay the name the verb dispatches to (ticket #56).
        from .. import cli
        return cli._cmd_update_async(positional[0], keep_both=keep_both,
                                 force=force, kill_running=kill_running,
                                 json_output=json_output,
                                 assume_yes=assume_yes)

    # `run_update` is resolved through the cli facade at CALL time: it is
    # the tests' patch seam (mock.patch.object(cli, 'run_update')), and
    # the facade re-export must stay the name the verb dispatches to
    # (ticket #56).
    from .. import cli
    result = cli.run_update(positional[0], keep_both=keep_both, force=force,
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



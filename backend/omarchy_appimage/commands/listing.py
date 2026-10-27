# listing.py — the --list-installed and --remove verbs (ticket #56).
#
# The listing is the panel's 10 s poll: one janitor sweep (throttled), ONE
# process probe for the whole table, the pending-verdict record read once.
# --remove refuses while that app's update job is live (document or claim)
# and trashes (or deletes) the file, its .desktop entry and its icon.
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import logging
import os

from .. import constants, janitor, jobs, procs, settings, icon
from ..updaters import UpdateManagerChecker
from .jobs import _job_desktop_id
from .envelope import (EXIT_OK, EXIT_USAGE, OperationError, UsageError, _ask,
                       _make_app_json, _print_json, _print_table,
                       _reject_unknown_options, _remove_flag_value,
                       _resolve_target, _runtime_block, _updates_state_key,
                       appimage_provider)


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
        # The icon display gate (ticket #62): the registry is read ONCE
        # and persisted ONCE after the loop — and only when the loop
        # actually changed it (every verify mutation appends to `dirty`),
        # so a clean 10 s poll costs no registry write at all.
        reg = icon.load_sanitized_registry()
        icons_dir = os.path.join(settings.appimages_folder(), '.icons')
        dirty = []
        table = []
        for a in apps:
            # check_url_for_app is local-only (apps.ini config + the
            # embedded .upd_info ELF section) and fills the manager /
            # embedded_source fields; available_version / download_size
            # stay null until an actual check runs (--list-updates).
            table.append(_make_app_json(
                a, manager=UpdateManagerChecker.check_url_for_app(a),
                probe=scan, pending=state.get(_updates_state_key(a)),
                icon_safe=_listing_icon_verdicts(a, reg, icons_dir, dirty)))
        if dirty:
            icon.save_sanitized_registry(reg)
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


# The icon display gate (ticket #63): the panel probes candidates in the
# same png→svg order (lib/Model.js iconBaseFor derives
# `.icons/<basename-without-extension>`), but a candidate that fails to
# DECODE makes the Image fall through to the next one — so verifying only
# the first existing candidate let an unproved .svg reach the shell's
# decoder. Each existing candidate therefore carries its OWN verdict; a
# candidate without `true` is never offered to the Image at all.
_ICON_EXTENSIONS = ('.png', '.svg')


def _listing_icon_verdicts(el, reg: dict, icons_dir: str,
                           dirty: list) -> dict:
    """Per-candidate verdicts: `{"png": bool, "svg": bool}`, one key per
    EXISTING candidate (janitor.verify_retained_icon against `reg`; every
    registry mutation appends to `dirty`, which the listing persists once
    after the loop). No icon file at all → {}. Never raises."""
    stem = os.path.join(
        icons_dir,
        os.path.splitext(os.path.basename(el.file_path or ''))[0])
    verdicts = {}
    for ext in _ICON_EXTENSIONS:
        candidate = stem + ext
        if os.path.exists(candidate):
            verdicts[ext[1:]] = janitor.verify_retained_icon(
                candidate, reg, dirty)
    return verdicts


# ------------------------------------------------------- update commands

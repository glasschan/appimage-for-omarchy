# checks.py — the update-check verbs (ticket #56): --list-updates and
# --fetch-updates plus their shared core (_check_updates, the tri-state
# per-app walk) and the durable updates-state record they merge into.
#
# The record is the backend's memory of the last verdict: an entry present
# means a pending release is known. MERGE semantics — a definite verdict
# writes or deletes, an unknown verdict keeps the previous entry, and an
# app the walk could not see keeps its entry while its recorded path
# exists. --fetch-updates writes the record BEFORE the notify-send
# announcement, so a stale record can never re-announce the same release.
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import logging
import os
import sys

from .. import constants, net, procs, settings
from ..updaters import UpdateManagerChecker
from ..utils import run_command
from .envelope import (EXIT_ERROR, EXIT_OK, OperationError, UsageError,
                       _capped_version, _make_app_json, _print_json,
                       _print_table, _reject_unknown_options,
                       _updates_state_key, appimage_provider)


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
                from .. import cli  # the tests' patch seam lives on the facade
                result = cli.run_command(
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



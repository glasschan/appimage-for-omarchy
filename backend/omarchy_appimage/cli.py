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

import json
import logging
import os
import sys

from . import constants, net, settings
from .ini_config import Config
from .provider import AppImageProvider, AppImageUpdateLogic, InternalError
from .updaters import UpdateError, UpdateManagerChecker
from .utils import run_command

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# words accepted for boolean settings / update-manager config values
TRUE_WORDS = ('1', 'true', 'yes', 'on')
FALSE_WORDS = ('0', 'false', 'no', 'off')

USAGE = f"""Usage: python3 main.py [OPTION...]

Manage AppImages (integrate / list / update / remove) for {constants.APP_NAME}.

Commands:
  --integrate <path>       Integrate an AppImage file (extracts .desktop
                           and icon, moves the file into the managed
                           folder)
  --list-installed         List integrated apps
  --remove <target>        Trash an AppImage, its .desktop file and its
                           icon; <target> may be the AppImage path, the
                           desktop id (e.g. neovim.desktop) or the app name
  --list-updates           Check every installed app that has an update
                           source for a new release
  --update <target>        Download and install an update (--keep-both
                           keeps the old version, --force updates even
                           while the app is running)
  --set-update-source ...  Set a custom update source:
                           <target> --manager <name> [key=value ...]
                           (--unset removes it again)
  --list-update-managers   List the available update managers and the
                           config keys each one accepts
  --fetch-updates          One-shot background update check; sends a
                           desktop notification when something is new
                           (used by the panel's timer service)
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
  --delete                 Delete files permanently instead of trashing
  --help | -h              Show this help

Exit codes:
  0  success
  1  operation error (not installed, extraction failed, ...)
  2  usage error (unknown option, missing argument, ...)

Managed folder: {constants.DEFAULT_APPIMAGES_FOLDER} (configurable via
{os.path.relpath(constants.settings_path(), constants.xdg_config_home())}).
XDG_DATA_HOME / XDG_CONFIG_HOME are honoured."""

appimage_provider = AppImageProvider()


class UsageError(Exception):
    pass


class OperationError(Exception):
    pass


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


def _get_file_from_args(args) -> str:
    for a in args:
        if a.startswith('-'):
            continue
        if os.path.exists(a) and os.path.isfile(a) \
                and appimage_provider.can_install_file(a):
            return a

    raise OperationError('please specify a valid AppImage file')


def _make_app_json(el, manager=None, running=None) -> dict:
    if running is None:
        try:
            running = appimage_provider.is_app_running(el)
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
        'embedded_source': bool(manager and manager.embedded),
        'running': running,
    }


def _remove_flag_value(args: list, flags: list) -> list:
    return [a for a in args if a not in flags]


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

    raise OperationError(
        f'AppImage not integrated: {target!r} '
        f'(see --list-installed for valid names/ids)')


def _parse_bool_word(raw: str, what: str) -> bool:
    word = str(raw).strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise UsageError(f'{what} is not a boolean value, allowed values: '
                     f'{"/".join(TRUE_WORDS + FALSE_WORDS)}')


def _updates_state_key(el) -> str:
    """Key for the updates-state.json map: desktop id, falling back to the
    AppImage file name (settings.updates_state_path entries)."""
    if el.desktop_file_path:
        return os.path.basename(el.desktop_file_path)
    return os.path.basename(el.file_path or '')


def _update_signature(manager) -> str:
    """What changed about an update between two background checks."""
    return (str(manager.available_version or '') + '|'
            + str(manager.download_size or ''))


# ----------------------------------------------------------------- commands

def cmd_integrate(args: list) -> int:
    json_output = '--json' in args
    assume_yes = ('-y' in args) or ('--yes' in args)
    replace = '--replace' in args

    if json_output and not assume_yes:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'error': '--json mode requires --yes (no interactive prompts)',
        })
        return EXIT_USAGE

    file_path = _get_file_from_args(_remove_flag_value(
        args, ['--json', '--yes', '-y', '--replace', '--keep-both']))

    list_element = appimage_provider.create_list_element_from_file(file_path)
    if appimage_provider.is_installed(list_element):
        message = 'This AppImage is already integrated'
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'already-integrated',
                'app': _make_app_json(list_element),
            })
        else:
            print(message)
        return EXIT_OK

    list_element.update_logic = AppImageUpdateLogic.KEEP
    appimage_provider.refresh_data(list_element)

    # Another version of the same app is detected either by display name
    # or by the original (un-suffixed) name recorded in X-AppImage-Name —
    # GearLever's CLI only compares names, which never matches apps that
    # were integrated with the "Name (version)" keep-both suffix.
    already_installed = None
    for a in appimage_provider.list_installed():
        original_name = ''
        if a.desktop_entry:
            original_name = a.desktop_entry.get('X-AppImage-Name', '') or ''
        if a.name == list_element.name or original_name == list_element.name:
            already_installed = a
            break

    if replace:
        list_element.update_logic = AppImageUpdateLogic.REPLACE
        list_element.updating_from = already_installed
    elif not assume_yes:
        print(f'Name:        {list_element.name}')
        print(f'Version:     {list_element.version or "Not specified"}')
        print(f'Description: {list_element.description or "None"}')
        ans = _ask('\nDo you really want to integrate this AppImage? (y/N)',
                   ['y', 'Y', 'n', 'N'])
        if ans.lower() != 'y':
            return EXIT_OK

        if already_installed:
            ans = _ask('\nAnother version of this app is already integrated.\n'
                       'How do you want to proceed? ((K)eep both/(R)eplace):',
                       ['k', 'r', 'K', 'R'])
            if ans.lower() == 'r':
                list_element.update_logic = AppImageUpdateLogic.REPLACE
                list_element.updating_from = already_installed

    appimage_provider.install_file(list_element)

    # the installed desktop entry may carry a suffixed name (keep-both
    # logic); report the name as it was actually installed
    if list_element.desktop_entry:
        list_element.name = list_element.desktop_entry.getName()

    message = f'{list_element.file_path} was integrated successfully'
    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'integrated',
            'message': message,
            'app': _make_app_json(list_element),
        })
    else:
        print(message)
    return EXIT_OK


def cmd_list_installed(args: list) -> int:
    json_output = '--json' in args
    apps = appimage_provider.list_installed()

    if json_output:
        table = []
        for a in apps:
            # check_url_for_app is local-only (apps.ini config + the
            # embedded .upd_info ELF section) and fills the manager /
            # embedded_source fields; available_version / download_size
            # stay null until an actual check runs (--list-updates).
            table.append(_make_app_json(
                a, manager=UpdateManagerChecker.check_url_for_app(a)))
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'installed': table,
        })
        return EXIT_OK

    if not apps:
        print('No AppImages integrated yet')
        return EXIT_OK

    rows = []
    for a in apps:
        running = ' (running)' if appimage_provider.is_app_running(a) else ''
        rows.append([a.name + running,
                     a.version or 'Not specified',
                     a.file_path])

    _print_table(rows)
    return EXIT_OK


def cmd_remove(args: list) -> int:
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
    positional = [a for a in positional if not a.startswith('--')]
    if not positional:
        raise UsageError('missing argument: --remove <name-or-desktop-id>')

    el = _resolve_target(positional[0])

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

def _check_updates() -> list:
    """All installed apps that have an update source and report an update
    (manager.is_update_available() is True), as (element, manager) pairs.

    Ported from the check loop of upstream Cli.list_updates: apps without
    a source are skipped and per-app errors are logged and skipped so one
    broken source cannot hide the others. Callers decide on connectivity
    first (net.check_internet)."""
    updates = []
    for el in appimage_provider.list_installed():
        manager = UpdateManagerChecker.check_url_for_app(el)
        if not manager:
            continue

        logging.debug('checking app %s with %s', el.file_path, manager.name)
        try:
            status = manager.is_update_available()
        except Exception:
            logging.exception('update check failed for %s', el.file_path)
            continue

        # tri-state: only a definite True counts (None = not determinable)
        if status is True:
            updates.append((el, manager))
    return updates


def cmd_list_updates(args: list) -> int:
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

    updates = _check_updates()

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'updates': [_make_app_json(el, manager=manager)
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


def cmd_update(args: list) -> int:
    json_output = '--json' in args
    assume_yes = ('-y' in args) or ('--yes' in args)
    keep_both = '--keep-both' in args
    force = '--force' in args

    if json_output and not assume_yes:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'error',
            'error': '--json mode requires --yes (no interactive prompts)',
        })
        return EXIT_USAGE

    positional = _remove_flag_value(
        args, ['--json', '--yes', '-y', '--keep-both', '--force'])
    positional = [a for a in positional if not a.startswith('--')]
    if not positional:
        raise UsageError('missing argument: --update <name-or-desktop-id>')

    el = _resolve_target(positional[0])

    manager = UpdateManagerChecker.check_url_for_app(el)
    if not manager:
        raise OperationError('No update method was found for this AppImage '
                             '(set one with --set-update-source)')

    if appimage_provider.is_app_running(el) and not force:
        message = (f'{el.file_path} was skipped because the application is '
                   'running; use --force to override')
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'skipped-running',
                'message': message,
                'app': _make_app_json(el, manager=manager),
            })
        else:
            print(message)
        return EXIT_OK

    # tri-state: None (state not determinable) still attempts the download
    if manager.is_update_available() is False:
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'up-to-date',
                'app': _make_app_json(el, manager=manager),
            })
        else:
            print(f'{el.file_path} is up to date')
        return EXIT_OK

    logging.debug('downloading update with %s', manager.name)

    def _progress(fraction: float):
        logging.debug('download: %d%%', round(fraction * 100))

    new_el = appimage_provider.update_from_url(
        manager, el, keep_both=keep_both, progress_cb=_progress)
    downloaded_bytes = new_el.size or 0

    # the update was applied: drop the pending-notification marker so the
    # next --fetch-updates does not re-notify for the (now installed) release
    state = settings.load_updates_state()
    key = _updates_state_key(el)
    if key in state:
        del state[key]
        settings.save_updates_state(state)

    message = f'{el.file_path} was updated successfully'
    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'updated',
            'message': message,
            'app': _make_app_json(new_el),
            'downloaded_bytes': downloaded_bytes,
        })
    else:
        print(message)
    return EXIT_OK


def cmd_set_update_source(args: list) -> int:
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


def cmd_list_update_managers(args: list) -> int:
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

    updates = _check_updates()

    state = settings.load_updates_state()
    new_updates = [(el, manager) for el, manager in updates
                   if _update_signature(manager)
                   != state.get(_updates_state_key(el))]

    notified = False
    if new_updates:
        body = (f'{len(new_updates)} update(s) available — click the '
                'AppImage icon in the bar.')
        try:
            result = run_command(
                ['notify-send', '-a', constants.APP_NAME,
                 '--expire-time=600000', 'AppImage updates available', body],
                check=False)
            notified = result.returncode == 0
        except Exception as e:
            logging.warning('notify-send failed: %s', e)

    # remember every currently-available signature (new or not) so
    # unchanged updates do not re-notify on the next run. A state-file
    # failure must not turn a successful sweep into a failed exit
    # (CONTRACT.md: --fetch-updates always exits 0).
    try:
        settings.save_updates_state({
            _updates_state_key(el): _update_signature(manager)
            for el, manager in updates
        })
    except OSError as e:
        logging.warning('saving updates state failed: %s', e)

    if json_output:
        _print_json({
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'updates': [_make_app_json(el, manager=manager)
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

def cmd_settings(args: list) -> int:
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


def _print_table(table: list):
    longest_cols = [
        (max(len(str(row[i])) for row in table) + 3)
        for i in range(len(table[0]))
    ]
    row_format = ''.join(f'{{:<{w}}}' for w in longest_cols)
    for row in table:
        print(row_format.format(*row))


# -------------------------------------------------------------------- entry

COMMANDS = {
    'integrate': cmd_integrate,
    'list-installed': cmd_list_installed,
    'remove': cmd_remove,
    'list-updates': cmd_list_updates,
    'update': cmd_update,
    'set-update-source': cmd_set_update_source,
    'list-update-managers': cmd_list_update_managers,
    'fetch-updates': cmd_fetch_updates,
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

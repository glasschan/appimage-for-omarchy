# cli.py — command implementations (stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Ported from src/Cli.py: only the MVP command set (integrate /
# list-installed / remove) is kept; update-related commands are P1 and
# the hooks for them (Config update sections, update_logic/updating_from)
# are preserved so they can be added without restructuring.
#
# Contract: with --json, stdout carries ONLY the JSON document; every
# human-readable line and all logging goes to stderr (PRD §8).

import json
import logging
import os
import sys

from . import constants
from .provider import AppImageProvider, AppImageUpdateLogic, InternalError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

USAGE = f"""Usage: python3 main.py [OPTION...]

Manage AppImages (integrate / list / remove) for {constants.APP_NAME}.

Commands:
  --integrate <path>       Integrate an AppImage file (extracts .desktop
                           and icon, moves the file into the managed
                           folder)
  --list-installed         List integrated apps
  --remove <target>        Trash an AppImage, its .desktop file and its
                           icon; <target> may be the AppImage path, the
                           desktop id (e.g. neovim.desktop) or the app name

Options:
  --json                   Machine-readable JSON on stdout (requires --yes
                           for --integrate/--remove)
  --yes | -y               Skip any interactive question
  --replace                On name conflict, replace the already
                           integrated app instead of keeping both
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


def _make_app_json(el, running=None) -> dict:
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
        'available_version': None,   # update managers are P1
        'download_size': None,
        'manager': None,             # no update manager support in MVP
        'embedded_source': False,
        'running': running,
    }


def _remove_flag_value(args: list, flags: list) -> list:
    return [a for a in args if a not in flags]


def _resolve_remove_target(target: str):
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
        table = [_make_app_json(a) for a in apps]
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

    el = _resolve_remove_target(positional[0])

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
        return EXIT_USAGE
    except (OperationError, InternalError, FileNotFoundError) as e:
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

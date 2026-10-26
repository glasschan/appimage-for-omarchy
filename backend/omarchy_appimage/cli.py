# cli.py — the CLI facade: usage text, verb dispatch table and main (ticket #56).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Every verb lives in commands/ (one concern per module): envelope helpers
# in commands/envelope.py, the integrate/preview verbs in
# commands/integrate.py, listing/removal in commands/listing.py, the
# update checks in commands/checks.py, the update pipeline in
# commands/update.py, the async job runner in commands/jobs.py and the
# settings verbs in commands/settings.py. This module stays the PUBLIC
# import surface: every moved name is re-exported below, so
# `omarchy_appimage.cli.<name>` keeps working for backend/main.py, the
# tests and CONTRACT.md — including the monkeypatch seams the suite
# patches here (cli.run_update, cli.run_command, cli._stdin_is_tty,
# cli._spawn_update_job, cli._cmd_update_async).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Ported from src/Cli.py + src/BackgroudUpdatesFetcher.py.
#
# Contract: with --json, stdout carries ONLY the JSON document; every
# human-readable line and all logging goes to stderr (PRD §8).

import logging
import os
import sys

from . import constants, net
from .commands.envelope import (EXIT_ERROR, EXIT_OK, EXIT_USAGE,
                                MAX_PERSISTED_VERSION_CHARS, OperationError,
                                UsageError, VERIFICATIONS, _ask,
                                _ask_close_running, _capped_version,
                                _installable_of, _json_dump, _make_app_json,
                                _print_json, _print_table,
                                _reject_unknown_options, _remove_flag_value,
                                _resolve_target, _running_app_prompt,
                                _running_pid_count, _runtime_block,
                                _stdin_is_tty, _update_state_installable,
                                _update_state_version, _updates_state_key,
                                _verification_of, appimage_provider)
from .utils import run_command  # facade re-export: the tests' patch seam
from .commands.urls import (_as_url, _auto_set_codeberg_source,
                            _auto_set_forgejo_source, _auto_set_github_source,
                            _auto_set_gitlab_source, _auto_set_source,
                            _auto_set_static_source,
                            _classify_integrate_url, _glob_for_version,
                            _github_release_url_data, _is_url,
                            _lookup_release_asset, _release_api_url,
                            _release_download_url_data, _update_source_chosen)
from .commands.integrate import (_fetch_integrate_artifact,
                                 _fetch_integrate_artifact_through_source,
                                 _find_name_conflict, _get_file_from_args,
                                 _preview_destination, _verification_preview,
                                 cmd_integrate, cmd_preview_integrate)
from .commands.listing import cmd_list_installed, cmd_remove
from .commands.jobs import (_JOB_HANDSHAKE_TIMEOUT_SECONDS,
                            _cmd_update_async, _finish_job_document,
                            _job_desktop_id, _job_entrypoint,
                            _refuse_second_job, _spawn_update_job)
from .commands.update import (UpdateResult, _ManagerStageProxy,
                              _close_running_app, _merge_force_quit,
                              _retire_update_verdict, _run_update_body,
                              run_update, cmd_update)
from .commands.checks import (_check_updates, _merge_updates_state,
                              _save_updates_state, _source_was_consulted,
                              _update_signature, _update_state_entry,
                              _update_state_signature, cmd_fetch_updates,
                              cmd_list_updates)
from .commands.exec_scale import cmd_exec_with_scale
from .commands.settings import (FALSE_WORDS, TRUE_WORDS, _parse_bool_word,
                                cmd_get_update_source, cmd_list_update_managers,
                                cmd_pick_file, cmd_set_setting,
                                cmd_set_update_source, cmd_settings)
# shared singletons the tests reach through this facade
from . import janitor
from .provider import InternalError
from .updaters import UpdateManagerChecker  # facade re-export


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
  --exec-with-scale <i>    Launch the AppImage at <i> with GDK_SCALE set
                           to the focused monitor's scale factor (a
                           bounded, best-effort compositor query: when
                           it cannot answer, the child inherits the
                           caller's environment unchanged). The image's
                           arguments follow the path verbatim and never
                           pass through a shell; the process is replaced
                           by os.execve, so no wrapper outlives the app
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
    'exec-with-scale': cmd_exec_with_scale,
    'settings': cmd_settings,
    'set-setting': cmd_set_setting,
}


def _intercept_scope(args: list) -> list:
    """The tokens the global intercepts (`--help`, `--json`) may look at.

    For the `--exec-with-scale` verb only the first token is ours — the
    image path — and everything after it belongs to the APP (the launch
    contract). An app argument like `--help` or `--json` must never be
    eaten by a plugin-level intercept, so for that verb the intercepts
    see only the tokens before the image path (which can carry neither).
    """
    if args and args[0].lstrip('-') == 'exec-with-scale':
        return args[:1]
    return args


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

    scoped = _intercept_scope(args)
    if '--help' in scoped[1:]:
        print(USAGE)
        return EXIT_OK

    try:
        return command(args[1:])
    except UsageError as e:
        print(f'Error: {e}', file=sys.stderr)
        print(USAGE, file=sys.stderr)
        if '--json' in scoped:
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
        if '--json' in scoped:
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
        if '--json' in scoped:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'error',
                'error': str(e),
            })
        return EXIT_ERROR

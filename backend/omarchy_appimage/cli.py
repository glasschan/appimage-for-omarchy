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

from . import (constants, extractor, janitor, jobs, net, pick_file, settings,
               utils)
from .extractor import read_desktop_entry_in_place
from .ini_config import Config
from .provider import (AppImageListElement, AppImageProvider,
                       AppImageUpdateLogic, InternalError, InstalledStatus,
                       resolve_install_names)
from .updaters import UpdateError, UpdateManagerChecker
from .utils import get_file_hash, run_command

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

# words accepted for boolean settings / update-manager config values
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
                           while the app is running, --async runs the
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
        # the desktop entry's Exec arguments (field codes stay VERBATIM —
        # the UI strips them), "" when the app has none
        'exec_arguments': getattr(el, 'exec_arguments', '') or '',
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


def _update_signature(manager) -> str:
    """What changed about an update between two background checks."""
    return (str(manager.available_version or '') + '|'
            + str(manager.download_size or ''))


# ----------------------------------------------------------------- commands

def _is_url(value: str) -> bool:
    return value.startswith(('http://', 'https://'))


def _release_download_url_data(url: str):
    """Parse any host's release download URL into
    {'host','owner','repo','tag','filename'}, or None if it isn't one.

    Shape: <scheme>://<host>/<owner>/<repo>/releases/download/<tag>/<file>"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ('http', 'https'):
        return None
    paths = parts.path.split('/')
    if len(paths) < 7 or paths[3] != 'releases' or paths[4] != 'download':
        return None
    owner, repo, tag, filename = (paths[1], paths[2],
                                  unquote(paths[5]), paths[6])
    if not owner or not repo or not tag or not filename:
        return None
    return {'host': parts.netloc, 'owner': owner, 'repo': repo,
            'tag': tag, 'filename': filename}


def _github_release_url_data(url: str):
    """Parse a GitHub release download URL into
    {'owner','repo','tag','filename'}, or None if it isn't one.

    Shape: https://github.com/<owner>/<repo>/releases/download/<tag>/<file>"""
    data = _release_download_url_data(url)
    if not data or data['host'] != 'github.com':
        return None
    return {k: data[k] for k in ('owner', 'repo', 'tag', 'filename')}


def _classify_integrate_url(url: str):
    """Route a pasted --integrate URL to its download/verify path
    (first match wins — a deliberate, documented deviation from the
    strict digest rule used by --update; see CONTRACT.md):

    1. github.com /releases/download/  -> ('github',  <github src>)   sha256 mandatory
    2. any host, path ends in .zsync   -> ('zsync',   {'url': url})   SHA-1 when present
    3. host gitlab.com                 -> ('gitlab',  {'repo_url','repo','filename'})
    4. host codeberg.org               -> ('codeberg',{'repo','filename'})
    5. other host /releases/download/  -> ('forgejo', {'repo_url','repo','filename'})
    6. anything else                   -> ('static',  {'url': url})

    Cases 3-5 look the release up through the host's API; a failed lookup
    falls back to case 6 in _fetch_integrate_artifact."""
    github = _github_release_url_data(url)
    if github:
        return 'github', github

    try:
        parts = urlsplit(url)
    except ValueError:
        return 'static', {'url': url}
    if parts.scheme not in ('http', 'https'):
        return 'static', {'url': url}

    if parts.path.endswith('.zsync'):
        return 'zsync', {'url': url}

    paths = parts.path.split('/')
    owner = paths[1] if len(paths) > 1 else ''
    repo = paths[2] if len(paths) > 2 else ''
    filename = unquote(paths[-1])

    if parts.netloc == 'gitlab.com' and owner and repo:
        return 'gitlab', {'repo_url': f'https://gitlab.com/{owner}/{repo}',
                          'repo': f'{owner}/{repo}', 'filename': filename}

    if parts.netloc == 'codeberg.org' and owner and repo:
        return 'codeberg', {'repo': f'{owner}/{repo}', 'filename': filename}

    forgejo = _release_download_url_data(url)
    if forgejo:
        return 'forgejo', {
            'repo_url': f'https://{forgejo["host"]}/{forgejo["owner"]}/'
                        f'{forgejo["repo"]}',
            'repo': f'{forgejo["owner"]}/{forgejo["repo"]}',
            'filename': forgejo['filename']}

    return 'static', {'url': url}


def _download_and_verify_github(url: str, src: dict, dest_dir: str) -> str:
    """Download the GitHub release asset into `dest_dir`, verifying its
    sha256 digest from the GitHub API (mandatory) and the AppImage magic.
    Returns the downloaded file path; raises OperationError on any
    verification failure (partial file is cleaned up via dest_dir)."""
    api_url = ('https://api.github.com/repos/'
               f'{src["owner"]}/{src["repo"]}/releases/tags/'
               f'{quote(src["tag"], safe="")}')
    try:
        release = net.fetch_json(api_url)
    except net.NetworkError as e:
        raise OperationError(f'could not verify the release: {e}') from e

    asset = next((a for a in release.get('assets', [])
                  if a.get('name') == src['filename']), None)
    if not asset:
        raise OperationError(
            f'asset {src["filename"]!r} not found in release '
            f'{src["tag"]} of {src["owner"]}/{src["repo"]}')

    # authenticity is mandatory (same contract as GithubUpdater.download):
    # the release asset's sha256 digest — a digest is required, so a
    # malicious or metadata-less asset is refused before any byte is spent
    digest = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise OperationError('GitHub release asset exposes no sha256 '
                             'digest; refusing to install an unverified '
                             'artifact')

    dest = os.path.join(dest_dir, 'download.appimage')
    net.download_to_file(url, dest)

    expected = digest[len('sha256:'):]
    actual = get_file_hash(dest, alg='sha256')
    if actual != expected:
        raise OperationError('Downloaded AppImage failed the sha256 digest '
                             f'check (expected {expected}, got {actual})')
    if not appimage_provider.can_install_file(dest):
        raise OperationError('downloaded file is not an AppImage')
    return dest


def _glob_for_version(filename: str, version: str) -> str:
    """`filename` with the release `version` (tag minus a leading 'v')
    substituted by `*`, so a future release still matches. When the
    version isn't part of the name the exact filename is kept."""
    version = version or ''
    if version[:1] in ('v', 'V'):
        version = version[1:]
    if version:
        return filename.replace(version, '*', 1)
    return filename


def _update_source_chosen(el) -> bool:
    """True when the user already chose an update source (never clobber)."""
    return bool(Config.get_app_update_config(el).get('manager'))


def _auto_set_github_source(el, src: dict):
    """Prepopulate the per-app GithubUpdater config from the install URL,
    unless the user already chose a source (never clobber a choice).

    The filename is turned into a glob by substituting the release version
    (tag minus a leading 'v') with `*`, so a future release with a bumped
    version still matches. If the version isn't in the name, the exact
    filename is kept and the user can refine it in the panel."""
    if _update_source_chosen(el):
        return

    Config.set_app_update_config(el, 'GithubUpdater', {
        'repo': f'{src["owner"]}/{src["repo"]}',
        'repo_filename': _glob_for_version(src['filename'], src['tag']),
        'allow_prereleases': 'false',
    })
    logging.info('auto-configured GithubUpdater source for %s', el.name)


def _auto_set_static_source(el, url: str):
    """Auto-populate a StaticFileUpdater source (a plain URL or .zsync)."""
    if _update_source_chosen(el):
        return
    Config.set_app_update_config(el, 'StaticFileUpdater', {'url': url})
    logging.info('auto-configured StaticFileUpdater source for %s', el.name)


def _auto_set_gitlab_source(el, repo_url: str, pattern: str):
    if _update_source_chosen(el):
        return
    Config.set_app_update_config(el, 'GitlabUpdater', {
        'repo_url': repo_url,
        'repo_filename': pattern,
    })
    logging.info('auto-configured GitlabUpdater source for %s', el.name)


def _auto_set_codeberg_source(el, repo: str, pattern: str):
    if _update_source_chosen(el):
        return
    Config.set_app_update_config(el, 'CodebergUpdater', {
        'repo': repo,
        'repo_filename': pattern,
        'allow_prereleases': 'false',
    })
    logging.info('auto-configured CodebergUpdater source for %s', el.name)


def _auto_set_forgejo_source(el, repo_url: str, pattern: str):
    if _update_source_chosen(el):
        return
    Config.set_app_update_config(el, 'ForgejoUpdater', {
        'repo_url': repo_url,
        'repo_filename': pattern,
        'allow_prereleases': 'false',
    })
    logging.info('auto-configured ForgejoUpdater source for %s', el.name)


def _auto_set_source(el, kind: str, info: dict):
    """Apply the auto-source matching a classified URL (no-op when the
    user already chose one)."""
    if kind == 'github':
        _auto_set_github_source(el, info)
    elif kind == 'gitlab':
        _auto_set_gitlab_source(el, info['repo_url'], info['pattern'])
    elif kind == 'codeberg':
        _auto_set_codeberg_source(el, info['repo'], info['pattern'])
    elif kind == 'forgejo':
        _auto_set_forgejo_source(el, info['repo_url'], info['pattern'])
    else:  # 'zsync' / 'static': the exact URL the user pasted
        _auto_set_static_source(el, info['url'])


def _verify_reduced_assurance(dest: str, expected_size: int = None):
    """Post-download guard for sources that expose no publisher digest: the
    artifact must still be an AppImage and, when a size was advertised
    (release-API `size`; HTTP `Content-Length` is already enforced by
    net.download_to_file), exactly that many bytes. Logs that assurance
    was reduced — this relaxation applies only to --integrate <url>, never
    to --update."""
    if not appimage_provider.can_install_file(dest):
        raise OperationError('downloaded file is not an AppImage')
    actual = os.path.getsize(dest)
    if expected_size is not None and actual != expected_size:
        raise OperationError(
            'downloaded file size mismatch (expected '
            f'{expected_size} bytes, got {actual})')
    logging.warning(
        '%s: no publisher digest available; verified only the AppImage '
        'magic bytes%s', dest,
        ' and the advertised size' if expected_size is not None else '')


def _download_static(url: str, dest_dir: str, expected_size: int = None) -> str:
    """Download `url` verbatim and apply the reduced-assurance checks."""
    dest = os.path.join(dest_dir, 'download.appimage')
    net.download_to_file(url, dest)
    _verify_reduced_assurance(dest, expected_size)
    return dest


def _fetch_zsync_control(zsync_url: str) -> tuple:
    """(target_url, sha1_or_None) from a .zsync control file, mirroring
    StaticFileUpdater._fetch_zsync: the target is the absolute `URL:` line
    when present, else the .zsync name with the suffix stripped; sha1 is
    the control file's `SHA-1:` line when it carries one."""
    zsync = net.fetch_text(zsync_url)
    header = zsync.split('\n\n', 1)[0]
    match = re.search(r'URL:\s*(\S+)', header)
    if match:
        target = match.group(1)
        if target.startswith(('https://', 'http://')):
            resolved = target
        else:
            parts = urlsplit(zsync_url)
            path = posixpath.join(posixpath.dirname(parts.path), target)
            resolved = urlunsplit(parts._replace(path=path, query='',
                                                 fragment=''))
    else:
        resolved = re.sub(r'\.zsync$', '', zsync_url)
    sha = re.search(r'SHA-1:\s*([0-9a-f]{40})', header)
    return resolved, (sha.group(1) if sha else None)


def _download_and_verify_zsync(zsync_url: str, dest_dir: str) -> str:
    """Download the artifact a .zsync control file points at. The control
    file's SHA-1 is verified when present (a publisher digest — never
    weakened); otherwise the reduced-assurance checks apply."""
    target, expected_sha1 = _fetch_zsync_control(zsync_url)
    if not target:
        raise OperationError(
            f'{zsync_url}: zsync control file points at no download URL')
    dest = os.path.join(dest_dir, 'download.appimage')
    net.download_to_file(target, dest)
    if expected_sha1:
        actual = get_file_hash(dest, alg='sha1')
        if actual != expected_sha1:
            raise OperationError(
                'Downloaded AppImage failed the zsync SHA-1 check '
                f'(expected {expected_sha1}, got {actual})')
        if not appimage_provider.can_install_file(dest):
            raise OperationError('downloaded file is not an AppImage')
        return dest
    _verify_reduced_assurance(dest)
    return dest


def _release_api_url(kind: str, info: dict) -> str:
    """The release-list API endpoint for the gitlab/codeberg/forgejo
    cases, mirroring the matching updater's request shape."""
    if kind == 'gitlab':
        return ('https://gitlab.com/api/v4/projects/'
                f'{quote(info["repo"], safe="")}/releases')
    if kind == 'codeberg':
        return (f'https://codeberg.org/api/v1/repos/{info["repo"]}'
                '/releases?pre-release=exclude&draft=exclude')
    host = info['repo_url'][len('https://'):].split('/', 1)[0]
    return f'https://{host}/api/v1/repos/{info["repo"]}/releases/latest'


def _lookup_release_asset(kind: str, info: dict):
    """Look the release asset up through the host's release API.

    Returns {'url','tag','size','filename'} or None — a network/404 error
    or a release without a matching asset makes --integrate fall back to a
    static install of the pasted URL."""
    try:
        data = net.fetch_json(_release_api_url(kind, info))
    except net.NetworkError as e:
        logging.error('%s release lookup failed: %s', kind, e)
        return None

    if kind == 'gitlab':
        release = data[0] if data else None
        assets = release.get('assets', {}).get('links', []) if release else []
        url_key, size_key = 'direct_asset_url', None
    else:
        release = data[0] if isinstance(data, list) and data else data
        assets = release.get('assets', []) if isinstance(release, dict) else []
        url_key, size_key = 'browser_download_url', 'size'
    if not release:
        return None

    tag = release.get('tag_name')
    pattern = _glob_for_version(info['filename'], tag)
    asset = next((a for a in assets
                  if fnmatch.fnmatch(a.get('name', ''), pattern)), None)
    if not asset or not asset.get(url_key):
        return None
    return {'url': asset[url_key], 'tag': tag, 'filename': info['filename'],
            'size': asset.get(size_key) if size_key else None}


def _fetch_integrate_artifact(kind: str, info: dict, raw_url: str,
                              dest_dir: str):
    """Fetch the artifact for a classified URL, returning
    (file_path, kind, info) — `kind`/`info` are updated on the case 3-5
    fallback to a static install and carry the `pattern` computed from the
    release tag for the auto-source step."""
    if kind == 'github':
        return _download_and_verify_github(raw_url, info, dest_dir), kind, info
    if kind == 'zsync':
        return _download_and_verify_zsync(raw_url, dest_dir), kind, info
    if kind in ('gitlab', 'codeberg', 'forgejo'):
        asset = _lookup_release_asset(kind, info)
        if asset is None:
            logging.warning('%s: no release metadata for %s; installing the '
                            'pasted URL as a static source', kind, raw_url)
            return (_download_static(raw_url, dest_dir), 'static',
                    {'url': raw_url})
        dest = _download_static(asset['url'], dest_dir, asset['size'])
        info = dict(info,
                    pattern=_glob_for_version(info['filename'], asset['tag']))
        return dest, kind, info
    # case 6: a plain URL
    return _download_static(raw_url, dest_dir), 'static', {'url': raw_url}


def _find_name_conflict(list_element):
    """The installed element that is another version of the same app,
    detected either by display name or by the original (un-suffixed) name
    recorded in X-AppImage-Name — GearLever's CLI only compares names,
    which never matches apps that were integrated with the
    "Name (version)" keep-both suffix. Shared by --integrate and
    --preview-integrate."""
    already_installed = None
    for a in appimage_provider.list_installed():
        original_name = ''
        if a.desktop_entry:
            original_name = a.desktop_entry.get('X-AppImage-Name', '') or ''
        if a.name == list_element.name or original_name == list_element.name:
            already_installed = a
            break
    return already_installed


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

    positional = _remove_flag_value(
        args, ['--json', '--yes', '-y', '--replace', '--keep-both'])
    target_arg = positional[0] if positional else None

    url_kind = None
    url_info = None
    tmp_cleanup = None
    if _is_url(target_arg or ''):
        url_kind, url_info = _classify_integrate_url(target_arg)
        tmp_cleanup = tempfile.mkdtemp(prefix='omarchy-integrate-')

    try:
        if url_kind:
            file_path, url_kind, url_info = _fetch_integrate_artifact(
                url_kind, url_info, target_arg, tmp_cleanup)
        else:
            file_path = _get_file_from_args(positional)

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

        already_installed = _find_name_conflict(list_element)

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
                ans = _ask('\nAnother version of this app is already '
                           'integrated.\nHow do you want to proceed? '
                           '((K)eep both/(R)eplace):',
                           ['k', 'r', 'K', 'R'])
                if ans.lower() == 'r':
                    list_element.update_logic = AppImageUpdateLogic.REPLACE
                    list_element.updating_from = already_installed

        appimage_provider.install_file(list_element)

        # prepopulate the per-app update source from the install URL
        if url_kind:
            _auto_set_source(list_element, url_kind, url_info)

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
    finally:
        if tmp_cleanup:
            shutil.rmtree(tmp_cleanup, ignore_errors=True)


# ------------------------------------------------------- preview command

def _verification_preview(kind: str) -> dict:
    """The verification block of the preview document: what --integrate
    will verify at its own runtime, derived from the same offline URL
    classification. The digest itself is only fetched at integrate
    time; a .zsync control file's SHA-1 line is likewise only read then
    (the preview stays offline), so the zsync note spells out the
    conditional."""
    if kind == 'github':
        return {
            'method': 'github-sha256',
            'reduced_assurance': False,
            'note': "the release asset's sha256 digest is fetched from "
                    'the GitHub API and verified at integrate time; a '
                    'missing digest refuses the install',
        }
    if kind == 'zsync':
        return {
            'method': 'zsync-sha1',
            'reduced_assurance': False,
            'note': "the .zsync control file's SHA-1 line is verified at "
                    'integrate time when the publisher publishes one; '
                    'without it the install runs with reduced assurance '
                    '(AppImage magic bytes and the advertised size)',
        }
    if kind == 'local':
        return {
            'method': 'none',
            'reduced_assurance': False,
            'note': 'no download: the local file itself is integrated '
                    '(AppImage magic-byte check only)',
        }
    # gitlab / codeberg / forgejo / static: no publisher digest exists
    return {
        'method': 'none',
        'reduced_assurance': True,
        'note': 'no publisher digest exists for this source; the '
                'artifact installs with reduced assurance (AppImage '
                'magic bytes and the advertised size)',
    }


def _preview_destination(source_basename: str, desktop_name,
                         version: str) -> dict:
    """The destination block of the preview document: the exact file
    name --integrate would pick, derived with the install-time naming
    helper (GearLever conflict suffixing included)."""
    folder = settings.appimages_folder()
    try:
        existing_names = os.listdir(folder)
    except OSError:
        existing_names = []
    filename, _prefix = resolve_install_names(
        source_basename=source_basename,
        desktop_name=desktop_name,
        version=version or '',
        existing_names=existing_names)
    return {'folder': folder, 'filename': filename,
            'path': os.path.join(folder, filename)}


def _as_url(value: str):
    """urlsplit(value) when the argument looks like a URL ('scheme://'),
    else None (a plain path, possibly one that does not exist)."""
    if '://' not in value:
        return None
    try:
        return urlsplit(value)
    except ValueError:
        return None


def cmd_preview_integrate(args: list) -> int:
    """--preview-integrate <path-or-url>: the offline pre-install preview
    document. Purely advisory and read-only — no network access (the
    classification is by URL shape, the same classifier --integrate
    uses), no AppImage execution (the embedded desktop entry is read in
    place with the stdlib squashfs reader), nothing is installed,
    downloaded or written. The integrate command remains the single
    authority and re-classifies/re-verifies at its own runtime."""
    json_output = '--json' in args

    # CONTRACT.md's exit-code table: an unknown option is a usage error,
    # never a silently ignored token — the preview verb accepts --json
    # and nothing else.
    positional = []
    for a in args:
        if a == '--json':
            continue
        if a.startswith('--'):
            raise UsageError(f'unknown option: {a!r}')
        positional.append(a)
    if not positional:
        raise UsageError(
            'missing argument: --preview-integrate <path-or-url>')
    if len(positional) > 1:
        raise UsageError(f'unexpected argument: {positional[1]!r}')
    target = positional[0]

    parts = _as_url(target)
    if parts is not None:
        # the URL branch mirrors --integrate's egress policy: net
        # refuses anything but https, so the preview refuses it too
        if parts.scheme != 'https':
            raise OperationError(
                f'{target}: only https:// URLs are allowed')
        kind, _info = _classify_integrate_url(target)
        document = {
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'preview',
            'source': {'kind': kind, 'input': target},
            'verification': _verification_preview(kind),
            'destination': {
                'folder': settings.appimages_folder(),
                'filename': None,
                'path': None,
            },
            'app': None,
            'already_integrated': False,
            'name_conflict': False,
            'advisory': True,
        }
    else:
        if not os.path.exists(target):
            raise OperationError(f'no such file: {target}')
        if not os.path.isfile(target):
            raise OperationError(f'not a regular file: {target}')
        if not appimage_provider.can_install_file(target):
            raise OperationError(f'{target} is not an AppImage')

        list_element = appimage_provider.create_list_element_from_file(
            target)
        already_integrated = appimage_provider.is_installed(list_element)

        if already_integrated:
            # report the installation the file already is: integrate
            # would exit 0 with 'already-integrated', changing nothing
            name = list_element.name
            version = list_element.version
            destination = {
                'folder': os.path.dirname(list_element.file_path),
                'filename': os.path.basename(list_element.file_path),
                'path': list_element.file_path,
            }
            name_conflict = False
        else:
            entry_name, entry_version = read_desktop_entry_in_place(target)
            destination = _preview_destination(
                os.path.basename(target), entry_name or None,
                entry_version or '')
            if entry_name:
                conflict_probe = AppImageListElement(
                    name=entry_name, description='', provider='',
                    installed_status=InstalledStatus.NOT_INSTALLED,
                    file_path=target)
                name_conflict = _find_name_conflict(conflict_probe) \
                    is not None
                name = entry_name
            else:
                logging.info('preview: no embedded desktop entry could '
                             'be read in place from %s', target)
                name = None
                name_conflict = False
            version = entry_version or None

        document = {
            'schema_version': constants.JSON_SCHEMA_VERSION,
            'result': 'preview',
            'source': {'kind': 'local', 'input': target},
            'verification': _verification_preview('local'),
            'destination': destination,
            'app': ({'name': name, 'version': version}
                    if name else None),
            'already_integrated': already_integrated,
            'name_conflict': name_conflict,
            'advisory': True,
        }

    if json_output:
        _print_json(document)
    else:
        print(f'Source:       {document["source"]["input"]}')
        print(f'Kind:         {document["source"]["kind"]}')
        if document['app']:
            line = f'App:          {document["app"]["name"]}'
            if document['app']['version']:
                line += f' (version {document["app"]["version"]})'
            print(line)
        dest = document['destination']
        if dest['path']:
            print(f'Destination:  {dest["path"]}')
        else:
            print(f'Destination:  {dest["folder"]} '
                  '(final name decided at integrate time)')
        verification = document['verification']
        suffix = ' (reduced assurance)' \
            if verification['reduced_assurance'] else ''
        print(f'Verification: {verification["method"]}{suffix}')
        print(f'              {verification["note"]}')
        if document['already_integrated']:
            print('Already integrated: yes (integrate would report '
                  'already-integrated)')
        elif document['name_conflict']:
            print('Name conflict: another version of this app is '
                  'installed (integrate keeps both, or replaces with '
                  '--replace)')
    return EXIT_OK


def cmd_list_installed(args: list) -> int:
    json_output = '--json' in args
    # janitor sweep (ticket 03): fossils — terminal/interrupted job
    # documents past the acknowledgement window and orphan update temp
    # dirs past the grace window — go away on every listing. THROTTLED:
    # the panel polls this every 10 s and each sweep is an unbounded
    # listdir + stat walk of the shared temp dir in this process's
    # foreground (the job-start hook in _cmd_update_async stays
    # unthrottled). Best-effort by contract: never raises.
    janitor.sweep_throttled()
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


# ------------------------------------------------------------ update core

class UpdateResult:
    """What run_update decided. Callers (cmd_update here, the async job
    runner below) turn it into output; the core itself never writes
    stdout. `status` uses the CLI's update result values:
    'skipped-running' | 'up-to-date' | 'updated' — anything else fails
    closed (InternalError) in both output paths."""

    __slots__ = ('status', 'el', 'manager', 'message', 'downloaded_bytes')

    def __init__(self, status: str, el, manager=None, message: str = '',
                 downloaded_bytes: int = 0):
        self.status = status
        # the element to report: the old one for skipped-running /
        # up-to-date, the freshly installed one for updated
        self.el = el
        self.manager = manager
        # human-readable message for skipped-running / updated ('' for
        # up-to-date: its plain-mode line is derived from el.file_path)
        self.message = message
        self.downloaded_bytes = downloaded_bytes


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

    def download(self, dest_dir: str, progress_cb=None) -> str:
        result = self._manager.download(dest_dir, progress_cb)
        if self._on_replacing is not None:
            try:
                self._on_replacing()
            except Exception:
                logging.exception('stage callback failed')
        return result


def run_update(target: str, keep_both: bool = False,
               force: bool = False, progress_cb=None,
               on_stage=None) -> UpdateResult:
    """The shared update pipeline: resolve target → update-source check →
    running guard → availability check → download to temp → verify →
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
    """
    el = _resolve_target(target)

    manager = UpdateManagerChecker.check_url_for_app(el)
    if not manager:
        raise OperationError('No update method was found for this AppImage '
                             '(set one with --set-update-source)')

    if appimage_provider.is_app_running(el) and not force:
        return UpdateResult(
            status='skipped-running', el=el, manager=manager,
            message=(f'{el.file_path} was skipped because the application '
                     'is running; use --force to override'))

    # tri-state: None (state not determinable) still attempts the download
    if manager.is_update_available() is False:
        return UpdateResult(status='up-to-date', el=el, manager=manager)

    if on_stage is not None:
        on_stage('downloading', manager)
        manager = _ManagerStageProxy(manager, lambda: on_stage('replacing'))

    logging.debug('downloading update with %s', manager.name)

    if progress_cb is None:
        def progress_cb(fraction: float):
            logging.debug('download: %d%%', round(fraction * 100))

    temp_dirs_before = extractor.temp_dirs_snapshot()
    try:
        new_el = appimage_provider.update_from_url(
            manager, el, keep_both=keep_both, progress_cb=progress_cb)
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

    # the update was applied: drop the pending-notification marker so the
    # next --fetch-updates does not re-notify for the (now installed) release
    state = settings.load_updates_state()
    key = _updates_state_key(el)
    if key in state:
        del state[key]
        settings.save_updates_state(state)

    return UpdateResult(
        status='updated', el=new_el, manager=manager,
        message=f'{el.file_path} was updated successfully',
        downloaded_bytes=new_el.size or 0)


def cmd_update(args: list) -> int:
    """CLI wrapper over run_update: argv/flag parsing plus the result's
    JSON document / plain line and exit code (byte-identical to the
    pre-extraction inline pipeline). --async detaches the pipeline into a
    double-forked grandchild and reports `job-started` instead."""
    json_output = '--json' in args
    assume_yes = ('-y' in args) or ('--yes' in args)
    keep_both = '--keep-both' in args
    force = '--force' in args
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
               '--async'])
    positional = [a for a in positional if not a.startswith('--')]
    if not positional:
        raise UsageError('missing argument: --update <name-or-desktop-id>')

    if async_job:
        return _cmd_update_async(positional[0], keep_both=keep_both,
                                 force=force, json_output=json_output)

    result = run_update(positional[0], keep_both=keep_both, force=force)

    if result.status == 'skipped-running':
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'skipped-running',
                'message': result.message,
                'app': _make_app_json(result.el, manager=result.manager),
            })
        else:
            print(result.message)
        return EXIT_OK

    if result.status == 'up-to-date':
        if json_output:
            _print_json({
                'schema_version': constants.JSON_SCHEMA_VERSION,
                'result': 'up-to-date',
                'app': _make_app_json(result.el, manager=result.manager),
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
            'app': _make_app_json(result.el),
            'downloaded_bytes': result.downloaded_bytes,
        })
    else:
        print(result.message)
    return EXIT_OK


# ------------------------------------------------------- async job runner

_JOB_HANDSHAKE_TIMEOUT_SECONDS = 10


def _cmd_update_async(target: str, keep_both: bool, force: bool,
                      json_output: bool) -> int:
    """Parent side of --update --async: validate the target (local name/id
    resolution only), take the job's atomic claim, refuse when a job is
    running for the app, then spawn the detached grandchild (see
    _spawn_update_job) and report `job-started` immediately. The parent
    never runs the pipeline itself and never resolves the update manager:
    that is run_update's first step inside the grandchild, so a missing
    or broken source surfaces as a terminal `error` job document instead
    of a second, drifting manager resolution here."""
    el = _resolve_target(target)
    # fail closed BEFORE any side effect (no sweep, no fork): the id must
    # be able to name a job file, or this path cannot honour its contract
    desktop_id = _job_desktop_id(el)

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
                log_fd=log_fd)
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
                    force: bool) -> int:
    """The detached grandchild's body: run the whole update pipeline and
    publish one job document per phase (jobs.JobWriter). Never raises;
    returns a process exit code (the fork site os._exit()s with it).
    In-process callers (tests) use the same entrypoint, so the sync and
    async pipelines cannot drift."""
    writer = jobs.JobWriter(desktop_id)
    writer.start_heartbeat()
    try:
        try:
            result = run_update(target, keep_both=keep_both, force=force,
                                progress_cb=writer.progress_cb,
                                on_stage=writer.stage)
        except Exception as e:
            # the traceback lands in the bounded per-job log (stderr is
            # redirected there), never on stdout
            logging.exception('update job for %s failed', desktop_id)
            writer.finish_error(str(e) or type(e).__name__)
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
                      downloaded_bytes=result.downloaded_bytes)
        return EXIT_OK
    if result.status == 'up-to-date':
        writer.finish('up-to-date',
                      message=f'{result.el.file_path} is up to date')
        return EXIT_OK
    if result.status == 'skipped-running':
        writer.finish('skipped-running', message=result.message)
        return EXIT_OK
    writer.finish_error(f'unknown update result status: {result.status!r}')
    return EXIT_ERROR


def _spawn_update_job(desktop_id: str, target: str, keep_both: bool,
                      force: bool, log_fd: int) -> tuple:
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
                    desktop_id, target, keep_both=keep_both, force=force)
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
    json_output = '--json' in args

    positional = _remove_flag_value(args, ['--json'])
    positional = [a for a in positional if not a.startswith('--')]
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
                 '--expire-time=5000', 'AppImage updates available', body],
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

def cmd_pick_file(args: list) -> int:
    """Open a native single-file AppImage dialog in a child process
    (crash-isolated): the desktop portal first, zenity as fallback. The
    heavy lifting (probe order, deadline, URI validation, typed results)
    lives in pick_file.py; this wrapper only maps the payload to the
    output contract."""
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

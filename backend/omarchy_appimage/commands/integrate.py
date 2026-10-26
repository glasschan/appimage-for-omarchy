# integrate.py — the --integrate and --preview-integrate verbs (ticket #56).
#
# Everything URL-shaped lives here too: the release-URL classifier, the
# release-API asset lookup and the update-source auto-setup the install
# URL triggers. --integrate is the single authority: it downloads,
# verifies and installs; --preview-integrate is the offline, advisory
# preview of the same classification. The fetch itself lives in updaters
# under the REDUCED policy (issue #45); GitHub assets are demanded with
# the digest under both policies.
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import fnmatch
import logging
import os
import shutil
import tempfile
from urllib.parse import quote, unquote, urlsplit

from .. import constants, janitor, net, settings, updaters
from ..extractor import read_desktop_entry_in_place
from ..ini_config import Config
from ..provider import (AppImageListElement, AppImageUpdateLogic,
                        InstalledStatus)
from ..updaters import UpdateError
from .envelope import (EXIT_OK, EXIT_USAGE, OperationError, UsageError,
                       _ask, _make_app_json, _print_json,
                       _reject_unknown_options, _remove_flag_value,
                       appimage_provider)


def _get_file_from_args(args) -> str:
    for a in args:
        if a.startswith('-'):
            continue
        if os.path.exists(a) and os.path.isfile(a) \
                and appimage_provider.can_install_file(a):
            return a

    raise OperationError('please specify a valid AppImage file')


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
        # the tag parsed from the URL is a real version, and it travels
        # with the classification: apply_release_version uses it when the
        # download carries no X-AppImage-Version of its own (v0.9.4)
        return 'github', dict(github, version=github['tag'])

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
    # The fetch itself lives in updaters (issue #45); its refusals are
    # UpdateError, which this CLI layer reports as an operation failure.
    try:
        return _fetch_integrate_artifact_through_source(kind, info, raw_url,
                                                        dest_dir)
    except UpdateError as e:
        raise OperationError(str(e)) from e


def _fetch_integrate_artifact_through_source(kind: str, info: dict,
                                             raw_url: str, dest_dir: str):
    """Fetch the artifact for a classified URL, returning
    (file_path, kind, info) — `kind`/`info` are updated on the case 3-5
    fallback to a static install and carry the `pattern` computed from the
    release tag for the auto-source step."""
    # Every download below goes through updaters' fetchers under the
    # REDUCED policy (issue #45): the user pasted this URL, so its lack of
    # a publisher digest is their choice — while --update always fetches
    # under the strict one.
    policy = updaters.POLICY_REDUCED
    magic = appimage_provider.can_install_file
    if kind == 'github':
        # GitHub does publish digests, so its assets are demanded, digest
        # or no digest, whichever policy brought the caller here.
        return (updaters.download_github_asset(
            dict(info, url=raw_url), dest_dir, updaters.POLICY_STRICT,
            magic), kind, info)
    if kind == 'zsync':
        return (updaters.download_zsync(raw_url, dest_dir, policy, magic),
                kind, info)
    if kind in ('gitlab', 'codeberg', 'forgejo'):
        asset = _lookup_release_asset(kind, info)
        if asset is None:
            logging.warning('%s: no release metadata for %s; installing the '
                            'pasted URL as a static source', kind, raw_url)
            return (updaters.download_plain(raw_url, dest_dir, policy,
                                            is_appimage=magic), 'static',
                    {'url': raw_url})
        dest = updaters.download_plain(asset['url'], dest_dir, policy,
                                       asset['size'], magic)
        info = dict(info,
                    pattern=_glob_for_version(info['filename'], asset['tag']),
                    version=asset['tag'] or '')
        return dest, kind, info
    # case 6: a plain URL
    return (updaters.download_plain(raw_url, dest_dir, policy,
                                    is_appimage=magic), 'static',
            {'url': raw_url})


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
    _reject_unknown_options(args, {'--json', '--yes', '-y', '--replace'})
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
        args, ['--json', '--yes', '-y', '--replace'])
    target_arg = positional[0] if positional else None

    url_kind = None
    url_info = None
    tmp_cleanup = None
    if _is_url(target_arg or ''):
        url_kind, url_info = _classify_integrate_url(target_arg)
        tmp_cleanup = tempfile.mkdtemp(prefix=janitor.INTEGRATE_TEMP_PREFIX)

    try:
        if url_kind:
            file_path, url_kind, url_info = _fetch_integrate_artifact(
                url_kind, url_info, target_arg, tmp_cleanup)
        else:
            file_path = _get_file_from_args(positional)

        list_element = appimage_provider.create_list_element_from_file(file_path)
        if appimage_provider.is_installed(list_element):
            # already integrated: nothing is installed. When the URL names a
            # release tag and the installed entry states no version of its
            # own, record it there (metadata repair only, v0.9.4) —
            # re-integrating the same URL is otherwise a dead end for an
            # install made before the plugin knew how to fill it in.
            if url_kind and appimage_provider.apply_version_to_installed(
                    list_element, (url_info or {}).get('version', '')):
                logging.info('recorded release version %s on the installed '
                             '%s', list_element.version,
                             list_element.file_path)
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
        # A release tag is a real version even when the image forgets to
        # embed one (v0.9.4): integrating from
        # .../releases/download/v0.9.0/Terax_0.9.0_amd64.AppImage used to
        # produce an app with no version at all. The tag the user pasted
        # describes the file better than nothing, and install_file then
        # writes it as X-AppImage-Version and in the desktop Name suffix.
        if url_kind:
            appimage_provider.apply_release_version(
                list_element, (url_info or {}).get('version', ''))

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
            'method': 'sha256',
            'policy': updaters.POLICY_STRICT,
            'reduced_assurance': False,
            'note': "the release asset's sha256 digest is fetched from "
                    'the GitHub API and verified at integrate time; a '
                    'missing digest refuses the install',
        }
    if kind == 'zsync':
        return {
            'method': 'zsync-sha1',
            'policy': updaters.POLICY_REDUCED,
            'reduced_assurance': False,
            'note': "the .zsync control file's SHA-1 line is verified at "
                    'integrate time when the publisher publishes one; '
                    'without it the install runs with reduced assurance '
                    '(AppImage magic bytes and the advertised size)',
        }
    if kind == 'local':
        return {
            'method': 'none',
            'policy': None,
            'reduced_assurance': False,
            'note': 'no download: the local file itself is integrated '
                    '(AppImage magic-byte check only)',
        }
    # gitlab / codeberg / forgejo / static: no publisher digest exists
    return {
        'method': 'none',
        'policy': updaters.POLICY_REDUCED,
        'reduced_assurance': True,
        'note': 'no publisher digest exists for this source; the '
                'artifact installs with reduced assurance (AppImage '
                'magic bytes and the advertised size)',
    }


def _preview_destination(source_basename: str, desktop_name,
                         version: str) -> dict:
    """The destination block of the preview document.

    Produced by the install plan's own naming rule (issue #45), so a
    preview cannot promise a file name the install would not use."""
    planned = appimage_provider.plan_destination(
        source_basename, desktop_name, version)
    return {'folder': planned['folder'], 'filename': planned['filename'],
            'path': planned['path']}


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



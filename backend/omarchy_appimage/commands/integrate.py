# integrate.py — the --integrate and --preview-integrate verbs (ticket #56).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
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
import sys
import tempfile
from urllib.parse import quote, unquote, urlsplit

from .. import constants, janitor, settings, updaters
from ..extractor import read_desktop_entry_in_place
from ..provider import (AppImageListElement, AppImageUpdateLogic,
                        InstalledStatus)
from ..updaters import UpdateError
from .envelope import (EXIT_OK, EXIT_USAGE, OperationError, UsageError,
                       _ask, _make_app_json, _print_json,
                       _reject_unknown_options, _remove_flag_value,
                       appimage_provider)
from .urls import (_as_url, _auto_set_source, _classify_integrate_url,
                   _glob_for_version, _is_url, _lookup_release_asset,
                   _update_source_chosen)


def _get_file_from_args(args) -> str:
    for a in args:
        if a.startswith('-'):
            continue
        if os.path.exists(a) and os.path.isfile(a) \
                and appimage_provider.can_install_file(a):
            return a

    raise OperationError('please specify a valid AppImage file')


# Progress documents fire on percent changes of the declared
# Content-Length; a server that sends none cannot be divided into
# percent steps, so bytes are reported every _NO_LENGTH_REPORT_BYTES
# instead (ticket #58).
_NO_LENGTH_REPORT_BYTES = 32 * 1024 * 1024


class _IntegrateProgress:
    """The `event: progress` line emitter for `--integrate <url> --json`
    (ticket #58). A plain run constructs nothing, so its human output is
    untouched. Every document is flushed when written: stdout is
    block-buffered when piped, and unflushed events would arrive in one
    burst after the transfer and show nothing."""

    def __init__(self):
        self._percent = None
        self._reported = 0

    def emit(self, phase: str, **fields):
        doc = {'schema_version': constants.JSON_SCHEMA_VERSION,
               'event': 'progress', 'phase': phase}
        doc.update(fields)
        _print_json(doc)
        sys.stdout.flush()

    def downloading(self, done: int, total: int):
        """byte_cb for net.download_to_file: (bytes so far, declared
        Content-Length, 0 when the server sent none)."""
        if total > 0:
            percent = done * 100 // total
            if percent == self._percent:
                return
            self._percent = percent
        elif self._reported and \
                done - self._reported < _NO_LENGTH_REPORT_BYTES:
            return
        else:
            self._reported = done
        self.emit('downloading', bytes=done, total=total)


def _fetch_integrate_artifact(kind: str, info: dict, raw_url: str,
                              dest_dir: str, byte_cb=None):
    # The fetch itself lives in updaters (issue #45); its refusals are
    # UpdateError, which this CLI layer reports as an operation failure.
    try:
        return _fetch_integrate_artifact_through_source(kind, info, raw_url,
                                                        dest_dir, byte_cb)
    except UpdateError as e:
        raise OperationError(str(e)) from e


def _fetch_integrate_artifact_through_source(kind: str, info: dict,
                                             raw_url: str, dest_dir: str,
                                             byte_cb=None):
    """Fetch the artifact for a classified URL, returning
    (file_path, kind, info) — `kind`/`info` are updated on the case 3-5
    fallback to a static install and carry the `pattern` computed from the
    release tag for the auto-source step. `byte_cb` threads the transfer
    progress through to net.download_to_file (ticket #58)."""
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
            magic, byte_cb=byte_cb), kind, info)
    if kind == 'zsync':
        return (updaters.download_zsync(raw_url, dest_dir, policy, magic,
                                        byte_cb=byte_cb), kind, info)
    if kind in ('gitlab', 'codeberg', 'forgejo'):
        asset = _lookup_release_asset(kind, info)
        if asset is None:
            logging.warning('%s: no release metadata for %s; installing the '
                            'pasted URL as a static source', kind, raw_url)
            return (updaters.download_plain(raw_url, dest_dir, policy,
                                            is_appimage=magic,
                                            byte_cb=byte_cb), 'static',
                    {'url': raw_url})
        dest = updaters.download_plain(asset['url'], dest_dir, policy,
                                       asset['size'], magic,
                                       byte_cb=byte_cb)
        info = dict(info,
                    pattern=_glob_for_version(info['filename'], asset['tag']),
                    version=asset['tag'] or '')
        return dest, kind, info
    # case 6: a plain URL
    return (updaters.download_plain(raw_url, dest_dir, policy,
                                    is_appimage=magic, byte_cb=byte_cb),
            'static', {'url': raw_url})


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
    # The progress stream (ticket #58) exists only for a URL integrate
    # under --json: a plain run emits nothing, and a local-file integrate
    # has no transfer to report.
    progress = _IntegrateProgress() if (json_output and _is_url(target_arg
                                                                or '')) \
        else None
    if _is_url(target_arg or ''):
        url_kind, url_info = _classify_integrate_url(target_arg)
        tmp_cleanup = tempfile.mkdtemp(prefix=janitor.INTEGRATE_TEMP_PREFIX)

    try:
        if url_kind:
            file_path, url_kind, url_info = _fetch_integrate_artifact(
                url_kind, url_info, target_arg, tmp_cleanup,
                byte_cb=progress.downloading if progress else None)
        else:
            file_path = _get_file_from_args(positional)

        if progress:
            progress.emit('verifying')

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

        if progress:
            progress.emit('installing')
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



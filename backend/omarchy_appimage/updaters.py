# updaters.py — update-source managers (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Ported from src/models/UpdateManager.py, UpdateManagerChecker.py,
# GithubUpdater.py, GitlabUpdater.py, CodebergUpdater.py,
# ForgejoUpdater.py and StaticFileUpdater.py:
#   * the GTK form machinery (AdwEntryRow / Adw.SwitchRow) becomes plain
#     config-template dicts validated by validate_config()
#   * `requests` becomes net.py (urllib)
#   * the readelf-based embedded-URL discovery becomes a pure-Python ELF
#     section read (elf.read_upd_info)
#   * FTPUpdater is not ported (PRD F13 / P2)
# Selection semantics follow upstream: a per-app update-manager config in
# apps.ini overrides the AppImage's embedded .upd_info section.
#
# Verification ladder (marketplace security-review hardening): every
# download() must leave behind a *verified* artifact or raise UpdateError
# (and unlink the partial temp file) — zsync SHA-1, GitHub sha256 asset
# digest, or advertised-size equality, whichever the source exposes.
# Sources that accept arbitrary hosts (GitLab/Forgejo) stay that way: any
# *public* host is allowed, and the net.py SSRF guard is the transport
# boundary — private/loopback targets are unreachable at dial time.

import fnmatch
import logging
import os
import platform
import posixpath
import re
from urllib.parse import quote, urlsplit, urlunsplit

from . import elf, net
from .ini_config import Config
from .utils import get_file_hash

TRUE_VALUES = ('1', 'true', 'yes', 'on')


class UpdateError(Exception):
    """Raised for invalid --set-update-source configurations and for a
    downloaded update that failed verification (digest/size mismatch)."""


def _unlink_quietly(path: str):
    """Remove a failed download's partial artifact (best-effort)."""
    try:
        os.unlink(path)
    except OSError:
        pass


def _header_content_length(headers: dict) -> int:
    """Content-Length as int, 0 when absent or garbage (some servers
    send values int() would choke on; no size known -> no size check)."""
    try:
        return int(headers.get('content-length') or 0)
    except (TypeError, ValueError):
        return 0


def config_bool(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key)
    if value is None:
        return default
    return str(value).strip().lower() in TRUE_VALUES


class UpdateManager():
    """Base class: one subclass per update source.

    is_update_available() is tri-state like upstream: True (update
    available), False (definitely up to date / not checkable) or None
    (a source is configured but its state could not be determined —
    network error, no matching release asset, ...).

    A successful check may also fill self.available_version and
    self.download_size for the UI (--list-updates JSON)."""

    name = ''
    label = ''
    handles_embedded = None   # prefix that routes an embedded string here
    system_arch = platform.machine()
    is_x86 = re.compile(r'(\-|\_|\.)x86(\-|\_|\.)')
    is_arm = re.compile(r'(\-|\_|\.)((arm64)|(aarch64)|(armv7l))(\-|\_|\.)')

    def __init__(self, el, embedded: str = None):
        self.el = el
        self.embedded = embedded
        self.available_version = None
        self.download_size = None

    # ---- config ----------------------------------------------------------

    def get_config(self) -> dict:
        return Config.get_app_update_config(self.el)

    def config_template(self) -> dict:
        """Default config for this manager; keys define what
        --set-update-source accepts."""
        return {}

    def validate_config(self, config: dict):
        """Raise UpdateError when `config` is not usable."""

    # ---- checks / download ------------------------------------------------

    def is_update_available(self):
        return False

    def download(self, dest_dir: str, progress_cb=None) -> str:
        """Download the new release into `dest_dir`; returns its path."""
        raise NotImplementedError

    # ---- verification helpers ----------------------------------------------

    def _verify_sha1(self, path: str, expected: str):
        """SHA-1 of `path` must match `expected` (zsync control file)."""
        actual = get_file_hash(path, alg='sha1')
        if actual != expected:
            raise UpdateError(f'Downloaded file failed the SHA-1 check '
                              f'(expected {expected}, got {actual})')

    def _verify_sha256(self, path: str, expected_hex: str):
        """SHA-256 of `path` must match `expected_hex` (GitHub digest)."""
        actual = get_file_hash(path, alg='sha256')
        if actual != expected_hex:
            raise UpdateError(f'Downloaded file failed the sha256 digest '
                              f'check (expected {expected_hex}, got {actual})')

    def _verify_size(self, path: str, expected: int, source: str):
        """The artifact must be exactly `expected` bytes (the download
        itself already fails short bodies; this also catches an over-long
        artifact when the server reported a size)."""
        actual = os.path.getsize(path)
        if actual != expected:
            raise UpdateError(f'Downloaded file has {actual} bytes but '
                              f'{source} advertised {expected}')

    def _verify_advertised_size(self, url: str, path: str):
        """Enforce size equality when a HEAD of `url` reported a length
        (sources whose API exposes no digest: static URLs, GitLab)."""
        headers = net.head_headers(url)
        remote = _header_content_length(headers)
        if remote > 0:
            self._verify_size(path, remote, url)

    # ---- helpers ----------------------------------------------------------

    def _pick_asset_by_arch(self, possible: list) -> dict:
        """When several assets match the filename pattern, prefer the one
        matching the local architecture (upstream heuristic)."""
        if len(possible) == 1:
            return possible[0]
        if self.system_arch == 'x86_64':
            for asset in possible:
                if self.is_x86.search(asset['name']) \
                        or not self.is_arm.search(asset['name']):
                    logging.info('possible target: %s', asset['name'])
                    return asset
        return None

    def _local_size(self) -> int:
        return os.path.getsize(self.el.file_path)


class StaticFileUpdater(UpdateManager):
    label = 'Static URL'
    name = 'StaticFileUpdater'
    handles_embedded = 'zsync|'

    def config_template(self) -> dict:
        return {'url': ''}

    def validate_config(self, config: dict):
        # https-only (marketplace review): plain http update sources are
        # rejected; the net.py transport would refuse them anyway. The
        # test seam (net._TEST_ALLOW_LOCAL) also accepts http so the
        # suite's loopback fixture servers can be configured end-to-end.
        schemes = ('https://', 'http://') if net._TEST_ALLOW_LOCAL \
            else ('https://',)
        url = str(config.get('url', ''))
        if not url.startswith(schemes):
            raise UpdateError('Enter a valid HTTPS url')

    def get_embedded_url(self) -> str:
        if not self.embedded:
            return None
        if self.embedded.startswith(('https://', 'http://')):
            return self.embedded
        # strip the 'zsync|' prefix
        return self.embedded[len(self.handles_embedded):]

    def _fetch_zsync(self, zsync_url: str) -> tuple:
        """(target_url, sha1_or_None) from a .zsync control file.

        The target is the absolute URL: line when present, else the .zsync
        name with the suffix stripped (upstream
        StaticFileUpdater.download); sha1 is the control file's SHA-1
        line when it carries one."""
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

    def _resolve_zsync_target(self, zsync_url: str) -> str:
        """The AppImage URL a .zsync control file points at."""
        return self._fetch_zsync(zsync_url)[0]

    def is_update_available(self):
        e_url = self.get_embedded_url()
        dwnl_url = self.get_config().get('url')

        if not self.el or not self.el.file_path:
            return False

        if e_url:
            try:
                zsync = net.fetch_text(e_url)
                match = re.search(r'SHA-1:\s*([0-9a-f]{40})',
                                  zsync.split('\n\n', 1)[0])
                if match:
                    return match.group(1) != \
                        get_file_hash(self.el.file_path, alg='sha1')
                # control file without a SHA-1 line: the embedded source's
                # state stays indeterminate (tri-state None) — never a
                # definite False, --update still attempts the download
                # (CONTRACT.md)
                return None
            except net.NetworkError as e:
                logging.debug('zsync check failed: %s', e)
                return None

        headers = net.head_headers(dwnl_url) if dwnl_url else {}
        remote_size = _header_content_length(headers)
        if remote_size <= 0:
            return False

        self.download_size = remote_size
        return remote_size != self._local_size()

    def download(self, dest_dir: str, progress_cb=None) -> str:
        url = self.get_config().get('url')
        expected_sha1 = None
        e_url = self.get_embedded_url()
        if e_url:
            url, expected_sha1 = self._fetch_zsync(e_url)
        if not url:
            raise UpdateError('Missing download URL')

        dest = os.path.join(dest_dir, 'update.appimage')
        try:
            net.download_to_file(url, dest, progress_cb)
            if expected_sha1:
                self._verify_sha1(dest, expected_sha1)
            else:
                # no SHA-1 binding (plain configured URL, or a zsync
                # control file without a SHA-1 line): fall back to
                # enforcing the advertised size when the server
                # reported one
                self._verify_advertised_size(url, dest)
        except BaseException:
            _unlink_quietly(dest)
            raise
        return dest


class GithubUpdater(UpdateManager):
    label = 'Github'
    name = 'GithubUpdater'
    handles_embedded = 'gh-releases-zsync|'

    def config_template(self) -> dict:
        return {'repo': '', 'repo_filename': '', 'allow_prereleases': False}

    def validate_config(self, config: dict):
        if len(str(config.get('repo', '')).split('/')) != 2:
            raise UpdateError('Invalid data, please enter <username>/<repo>')

    def get_embedded_data(self):
        # Format: gh-releases-zsync|probono|AppImages|latest|Subsurface-*.AppImage.zsync
        # https://github.com/AppImage/AppImageSpec/blob/master/draft.md#github-releases
        if not self.embedded:
            return None
        items = self.embedded.split('|')
        if len(items) != 5:
            return None
        return {
            'username': items[1],
            'repo': items[2],
            'release': items[3],
            'filename': items[4],
        }

    def get_url_data(self, url: str):
        """Parse a gh-releases-zsync string or a github.com release URL
        into username/repo/filename (upstream get_url_data)."""
        tag_name = '*'
        if url.startswith('https://'):
            parts = urlsplit(url)
            if parts.netloc != 'github.com':
                return None
            paths = parts.path.split('/')
            if len(paths) < 7 \
                    or paths[3] != 'releases' or paths[4] != 'download':
                return None
            # rebuild an appimage-style update string from the URL
            url = f'|{paths[1]}|{paths[2]}|latest|{paths[6]}'

        items = url.split('|')
        if len(items) != 5:
            return None
        return {
            'username': items[1],
            'repo': items[2],
            'release': items[3],
            'filename': items[4],
        }

    def _does_allow_prereleases(self) -> bool:
        if self.embedded:
            data = self.get_embedded_data()
            if data:
                return data['release'] in ('latest-pre', 'latest-all')
            return False
        return config_bool(self.get_config(), 'allow_prereleases')

    def fetch_target_asset(self):
        """Find the release asset matching the configured filename.

        Returns {'asset': {...}, 'zsync': {...}|None} or None; also fills
        self.available_version / self.download_size for the UI."""
        if self.embedded:
            update_data = self.get_embedded_data()
        else:
            config = self.get_config()
            repo = str(config.get('repo', '')).split('/')
            if len(repo) < 2:
                return None
            update_data = {
                'username': repo[0],
                'repo': repo[1],
                'filename': config.get('repo_filename'),
            }

        if not update_data or not update_data.get('filename'):
            return None

        allow_prereleases = self._does_allow_prereleases()
        rel_url = (f'https://api.github.com/repos/{update_data["username"]}'
                   f'/{update_data["repo"]}/releases')
        if not allow_prereleases:
            rel_url += '/latest'

        try:
            releases = net.fetch_json(rel_url)
            if not allow_prereleases:
                releases = [releases]
        except net.NetworkError as e:
            logging.error('github releases: %s', e)
            return None

        for release in releases:
            if not allow_prereleases and release.get('draft'):
                continue

            possible = [a for a in release.get('assets', [])
                        if fnmatch.fnmatch(a.get('name', ''),
                                           update_data['filename'])]
            if not possible:
                continue

            if self.embedded:
                target = possible[0]
            else:
                target = self._pick_asset_by_arch(possible)

            if not target:
                return None

            is_zsync = bool(self.embedded) \
                and target['name'].endswith('.zsync')
            target_name = re.sub(r'\.zsync$', '', target['name'])

            for asset in release.get('assets', []):
                if asset['name'] == target_name:
                    self.available_version = release.get('tag_name')
                    self.download_size = asset.get('size')
                    return {'asset': asset,
                            'zsync': target if is_zsync else None}
        return None

    def is_update_available(self):
        if not self.el or not os.path.exists(self.el.file_path):
            return False

        target_asset = self.fetch_target_asset()
        if not target_asset:
            if self.embedded:
                # a config-less embedded source that could not be resolved
                # (offline, no matching release) stays indeterminate so
                # --update still attempts the download (CONTRACT.md)
                return None
            if self.get_config().get('repo_filename'):
                return None
            return False

        if target_asset['zsync']:
            try:
                zsync = net.fetch_text(
                    target_asset['zsync']['browser_download_url'])
            except net.NetworkError:
                return None
            match = re.search(r'SHA-1:\s*([0-9a-f]{40})',
                              zsync.split('\n\n', 1)[0])
            if match:
                return match.group(1) != \
                    get_file_hash(self.el.file_path, alg='sha1')
            return None

        digest = target_asset['asset'].get('digest', '')
        if digest and digest.startswith('sha256:'):
            current = get_file_hash(self.el.file_path, alg='sha256')
            return f'sha256:{current}' != digest

        return target_asset['asset'].get('size') != self._local_size()

    def download(self, dest_dir: str, progress_cb=None) -> str:
        target_asset = self.fetch_target_asset()
        if not target_asset:
            raise UpdateError(f'Missing target asset for {self.name}')

        asset = target_asset['asset']
        dest = os.path.join(dest_dir, 'update.appimage')
        try:
            net.download_to_file(
                asset['browser_download_url'], dest, progress_cb)

            # verification ladder (strongest binding first): the release
            # asset's sha256 digest, the embedded zsync's SHA-1, then the
            # advertised size; digest can be null in the API response
            # (guarded like the availability check does)
            digest = asset.get('digest', '')
            if digest and digest.startswith('sha256:'):
                self._verify_sha256(dest, digest[len('sha256:'):])

            if target_asset['zsync']:
                zsync = net.fetch_text(
                    target_asset['zsync']['browser_download_url'])
                match = re.search(r'SHA-1:\s*([0-9a-f]{40})',
                                  zsync.split('\n\n', 1)[0])
                if match:
                    self._verify_sha1(dest, match.group(1))

            size = asset.get('size') or 0
            if size > 0:
                self._verify_size(dest, size, 'the release asset')
        except BaseException:
            _unlink_quietly(dest)
            raise
        return dest


class GitlabUpdater(UpdateManager):
    label = 'Gitlab'
    name = 'GitlabUpdater'

    def config_template(self) -> dict:
        return {'repo_url': '', 'repo_filename': ''}

    def validate_config(self, config: dict):
        if not self.get_url_data(str(config.get('repo_url', ''))):
            raise UpdateError(f'Invalid {self.label} url')

    def get_url_data(self, url: str):
        if not url.startswith('https://'):
            return None
        parts = urlsplit(url)
        paths = parts.path.split('/')
        if len(paths) < 3:
            return None
        return {'netloc': parts.netloc, 'repo': '/'.join(paths[1:4])}

    def fetch_target_asset(self):
        url_data = self.get_url_data(str(self.get_config().get('repo_url', '')))
        if not url_data:
            return None

        rel_url = (f'https://{url_data["netloc"]}/api/v4/projects/'
                   f'{quote(url_data["repo"], safe="")}/releases')
        try:
            releases = net.fetch_json(rel_url)
        except net.NetworkError as e:
            logging.error('gitlab releases: %s', e)
            return None

        if not releases:
            return None

        pattern = self.get_config().get('repo_filename', '')
        possible = [link for link in releases[0].get('assets', {})
                    .get('links', [])
                    if fnmatch.fnmatch(link.get('name', ''), pattern)]
        asset = self._pick_asset_by_arch(possible) if possible else None
        if not asset:
            return None

        self.available_version = releases[0].get('tag_name')
        return asset

    def is_update_available(self):
        if not self.el or not os.path.exists(self.el.file_path):
            return False

        asset = self.fetch_target_asset()
        if asset:
            headers = net.head_headers(asset['direct_asset_url'])
            remote_size = _header_content_length(headers)
            if remote_size > 0:
                self.download_size = remote_size
                return remote_size != self._local_size()
            return None

        if self.get_config().get('repo_filename'):
            return None
        return False

    def download(self, dest_dir: str, progress_cb=None) -> str:
        asset = self.fetch_target_asset()
        if not asset:
            raise UpdateError(f'Missing target asset for {self.name}')

        dest = os.path.join(dest_dir, 'update.appimage')
        try:
            net.download_to_file(asset['direct_asset_url'], dest, progress_cb)
            # the GitLab API exposes no digests: enforce advertised-size
            # equality when the HEAD reported one (net.py caps bound the
            # rest)
            self._verify_advertised_size(asset['direct_asset_url'], dest)
        except BaseException:
            _unlink_quietly(dest)
            raise
        return dest


class _GiteaApiUpdater(UpdateManager):
    """Shared implementation for Codeberg and Forgejo (both expose the
    Gitea /api/v1 release API with browser_download_url assets)."""

    api_host = None  # subclass: netloc used when the repo lives there

    def config_template(self) -> dict:
        return {'repo_url': '', 'repo_filename': '', 'allow_prereleases': False}

    def get_url_data(self, url: str):
        if not url.startswith('https://'):
            return None
        parts = urlsplit(url)
        paths = parts.path.split('/')
        if len(paths) < 3:
            return None
        return {'netloc': parts.netloc, 'username': paths[1], 'repo': paths[2]}

    def _api_release(self):
        """(release_dict, rel_url) or (None, url) — the newest non-draft
        release, honouring allow_prereleases."""
        url_data = self.get_url_data(
            str(self.get_config().get('repo_url', '')))
        if not url_data:
            return None

        rel_url = (f'https://{url_data["netloc"]}/api/v1/repos/'
                   f'{url_data["username"]}/{url_data["repo"]}/releases')

        allow_prereleases = config_bool(self.get_config(),
                                        'allow_prereleases')
        try:
            if allow_prereleases:
                releases = net.fetch_json(rel_url + '?draft=exclude')
                for release in releases:
                    if not release.get('draft'):
                        return release
                return None
            return net.fetch_json(rel_url + '/latest')
        except net.NetworkError as e:
            logging.error('%s releases: %s', self.name, e)
            return None

    def fetch_target_asset(self):
        pattern = self.get_config().get('repo_filename', '')
        if not pattern:
            return None

        release = self._api_release()
        if not release:
            return None

        possible = [a for a in release.get('assets', [])
                    if fnmatch.fnmatch(a.get('name', ''), pattern)]
        asset = self._pick_asset_by_arch(possible) if possible else None
        if asset:
            self.available_version = release.get('tag_name')
            self.download_size = asset.get('size')
        return asset

    def is_update_available(self):
        if not self.el or not os.path.exists(self.el.file_path):
            return False

        asset = self.fetch_target_asset()
        if asset:
            return asset.get('size') != self._local_size()
        if self.get_config().get('repo_filename'):
            return None
        return False

    def download(self, dest_dir: str, progress_cb=None) -> str:
        asset = self.fetch_target_asset()
        if not asset:
            raise UpdateError(f'Missing target asset for {self.name}')

        dest = os.path.join(dest_dir, 'update.appimage')
        try:
            net.download_to_file(asset['browser_download_url'], dest,
                                 progress_cb)
            # the Gitea API exposes no digests: enforce the advertised
            # asset size when it reports one
            size = asset.get('size') or 0
            if size > 0:
                self._verify_size(dest, size, 'the release asset')
        except BaseException:
            _unlink_quietly(dest)
            raise
        return dest


class CodebergUpdater(_GiteaApiUpdater):
    label = 'Codeberg'
    name = 'CodebergUpdater'
    api_host = 'codeberg.org'

    def config_template(self) -> dict:
        return {'repo': '', 'repo_filename': '', 'allow_prereleases': False}

    def validate_config(self, config: dict):
        if len(str(config.get('repo', '')).split('/')) != 2:
            raise UpdateError('Invalid data, please enter <username>/<repo>')

    def get_url_data(self, url: str):
        if not url.startswith('https://'):
            return None
        parts = urlsplit(url)
        if parts.netloc != self.api_host:
            return None
        paths = parts.path.split('/')
        if len(paths) < 3:
            return None
        return {'netloc': parts.netloc, 'username': paths[1], 'repo': paths[2]}

    def _api_release(self):
        # Codeberg keeps the Gitea shape but takes its repo as
        # <username>/<repo> and filters pre-releases with query params.
        repo = str(self.get_config().get('repo', '')).split('/')
        if len(repo) < 2:
            return None

        allow_prereleases = config_bool(self.get_config(),
                                        'allow_prereleases')
        rel_url = f'https://{self.api_host}/api/v1/repos/' \
                  f'{repo[0]}/{repo[1]}/releases'
        if allow_prereleases:
            rel_url += '?draft=exclude'
        else:
            rel_url += '?pre-release=exclude&draft=exclude'

        try:
            releases = net.fetch_json(rel_url)
        except net.NetworkError as e:
            logging.error('codeberg releases: %s', e)
            return None
        return releases[0] if releases else None


class ForgejoUpdater(_GiteaApiUpdater):
    label = 'Forgejo'
    name = 'ForgejoUpdater'

    def validate_config(self, config: dict):
        if not self.get_url_data(str(config.get('repo_url', ''))):
            raise UpdateError(f'Invalid {self.label} url')


class UpdateManagerChecker():
    @staticmethod
    def get_models() -> list:
        return [StaticFileUpdater, GithubUpdater, GitlabUpdater,
                CodebergUpdater, ForgejoUpdater]

    @staticmethod
    def get_model_by_name(manager_name: str):
        for model in UpdateManagerChecker.get_models():
            if model.name == manager_name:
                return model
        raise UpdateError(f'Invalid model name: {manager_name}')

    @staticmethod
    def check_app_embedded_url(el) -> str:
        """The AppImage's embedded update string ('' when absent).

        Replaces GearLever's readelf subprocess with elf.read_upd_info;
        the accepted shapes are the two the AppImageSpec defines:
        gh-releases-zsync|... and zsync|http..."""
        try:
            info = elf.read_upd_info(el.file_path)
        except Exception as e:
            logging.debug('reading .upd_info failed: %s', e)
            return ''
        info = info.strip()
        if info.startswith('gh-releases-zsync|') \
                or info.startswith('zsync|http'):
            return info
        return ''

    @staticmethod
    def check_url_for_app(el):
        """The UpdateManager for `el`, or None.

        A stored per-app config wins; otherwise the embedded .upd_info
        section routes to the handler that understands it (upstream
        UpdateManagerChecker.check_url_for_app)."""
        app_conf = Config.get_app_update_config(el)
        manager_name = app_conf.get('manager')

        if manager_name:
            try:
                model = UpdateManagerChecker.get_model_by_name(manager_name)
            except UpdateError:
                logging.warning('unknown update manager: %s', manager_name)
                return None
            return model(el=el)

        embedded_url = UpdateManagerChecker.check_app_embedded_url(el)
        if not embedded_url:
            return None

        for model in UpdateManagerChecker.get_models():
            if model.handles_embedded and \
                    embedded_url.startswith(model.handles_embedded):
                logging.debug('checking embedded url with %s', model.__name__)
                return model(el=el, embedded=embedded_url)

        return None

    @staticmethod
    def manager_metadata() -> list:
        """Name/label/config-keys for --list-update-managers and the
        panel's settings UI."""
        return [
            {
                'name': m.name,
                'label': m.label,
                # config_template() is an instance method (it may read
                # self), so build a throwaway manager per model
                'config_keys': list(m(el=None).config_template().keys()),
            }
            for m in UpdateManagerChecker.get_models()
        ]

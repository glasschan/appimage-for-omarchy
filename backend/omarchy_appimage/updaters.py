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
# (and unlink the partial temp file) — cryptographic digests are
# mandatory, advertised-size equality alone never authorizes an install:
#   * GithubUpdater — the release asset's `sha256:<hex>` digest is
#     required (refusing before any byte is downloaded when missing);
#     the embedded zsync flow additionally cross-checks the control
#     file's SHA-1;
#   * StaticFileUpdater — installable only through a zsync control file
#     with a SHA-1 line (the AppImageSpec's publisher-attested binding);
#     plain static URLs and SHA-1-less control files refuse;
#   * GitlabUpdater / CodebergUpdater / ForgejoUpdater — their release
#     APIs expose no digests, so installation refuses outright (the
#     tri-state availability checks stay fully informative).
# SHA-1 rationale: in a zsync control file the digest is published by
# the same publisher as the artifact, so a collision attack requires
# publisher cooperation — which would equally defeat any digest scheme,
# sha256 included (the publisher also controls the release metadata).
# The binding's security therefore rests on second-preimage resistance,
# against which SHA-1 remains unbroken; we do NOT extend this claim to
# sizes. Sources that accept arbitrary hosts (GitLab/Forgejo) stay that
# way: any *public* host is allowed, and the net.py SSRF guard is the
# transport boundary — private/loopback targets are unreachable at dial
# time.

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

# ---- fetch policies (issue #45) --------------------------------------------
# One module per publisher, and the assurance a fetch may lean on is a
# PARAMETER of that fetch, not a decision each caller re-takes:
#   strict  — an artifact is only fetched when the publisher publishes a
#             digest for it; a source without one is refused before a
#             single byte is downloaded (every --update).
#   reduced — the artifact may arrive with no publisher digest and is then
#             accepted on the AppImage magic bytes plus its advertised
#             size (a URL the user pasted, which is itself the choice).
POLICY_STRICT = 'strict'
POLICY_REDUCED = 'reduced'
POLICIES = (POLICY_STRICT, POLICY_REDUCED)

# The verification methods that make an artifact provable.
DIGEST_METHODS = ('sha256', 'zsync-sha1')


def check_policy(policy: str) -> str:
    """The policy name, or UpdateError — an unknown policy must never fall
    back to the permissive one by accident."""
    if policy not in POLICIES:
        raise UpdateError(f'unknown fetch policy: {policy!r} (expected one '
                          f'of {", ".join(POLICIES)})')
    return policy


def verify_reduced_assurance(dest: str, expected_size: int = None,
                            is_appimage=None):
    """The reduced-assurance guard: an AppImage, of the advertised size.

    Used only under POLICY_REDUCED. `is_appimage` is the provider's
    magic-byte check, passed in because provider imports this module."""
    if is_appimage is not None and not is_appimage(dest):
        raise UpdateError('downloaded file is not an AppImage')
    actual = os.path.getsize(dest)
    if expected_size is not None and actual != expected_size:
        raise UpdateError(
            'downloaded file size mismatch (expected '
            f'{expected_size} bytes, got {actual})')
    logging.warning(
        '%s: no publisher digest available; verified only the AppImage '
        'magic bytes%s', dest,
        ' and the advertised size' if expected_size is not None else '')


def fetch_zsync_control(zsync_url: str) -> tuple:
    """(target_url, sha1_or_None) from a .zsync control file.

    The target is the absolute URL: line when present, else the .zsync name
    with the suffix stripped; sha1 is the control file's SHA-1 line when it
    carries one. The single implementation: StaticFileUpdater and the
    pasted-URL integrate path both read control files through it."""
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


def download_zsync(zsync_url: str, dest_dir: str, policy: str,
                   is_appimage=None) -> str:
    """Fetch the artifact a .zsync control file points at.

    A control file's SHA-1 line is a publisher digest and is always
    verified. Without one, only POLICY_REDUCED proceeds (reduced-assurance
    checks); POLICY_STRICT refuses before downloading."""
    check_policy(policy)
    target, expected_sha1 = fetch_zsync_control(zsync_url)
    if not target:
        raise UpdateError(
            f'{zsync_url}: zsync control file points at no download URL')
    if not expected_sha1 and policy == POLICY_STRICT:
        raise UpdateError(f'{zsync_url}: zsync control file has no SHA-1 '
                          'line; refusing to install an unverified artifact')
    dest = os.path.join(dest_dir, 'download.appimage')
    net.download_to_file(target, dest)
    if expected_sha1:
        actual = get_file_hash(dest, alg='sha1')
        if actual != expected_sha1:
            raise UpdateError(
                'Downloaded AppImage failed the zsync SHA-1 check '
                f'(expected {expected_sha1}, got {actual})')
        if is_appimage is not None and not is_appimage(dest):
            raise UpdateError('downloaded file is not an AppImage')
        return dest
    verify_reduced_assurance(dest, None, is_appimage)
    return dest


def download_plain(url: str, dest_dir: str, policy: str,
                   expected_size: int = None, is_appimage=None) -> str:
    """Fetch `url` verbatim. POLICY_STRICT refuses outright — a plain URL
    carries no digest — so only a pasted-URL integrate reaches the
    reduced-assurance checks."""
    check_policy(policy)
    if policy == POLICY_STRICT:
        raise UpdateError(
            'a plain URL provides no cryptographic digest; refusing to '
            'install an unverified artifact')
    dest = os.path.join(dest_dir, 'download.appimage')
    net.download_to_file(url, dest)
    verify_reduced_assurance(dest, expected_size, is_appimage)
    return dest


def download_github_asset(src: dict, dest_dir: str, policy: str,
                          is_appimage=None) -> str:
    """Fetch one release asset, verifying the release's sha256 digest when
    the API publishes one (`src` carries owner/repo/tag/filename/url).

    Under POLICY_STRICT a missing digest refuses before any download; under
    POLICY_REDUCED it falls back to the reduced-assurance checks."""
    check_policy(policy)
    api_url = ('https://api.github.com/repos/'
               f'{src["owner"]}/{src["repo"]}/releases/tags/'
               f'{quote(src["tag"], safe="")}')
    try:
        release = net.fetch_json(api_url)
    except net.NetworkError as e:
        raise UpdateError(f'could not verify the release: {e}') from e

    asset = next((a for a in release.get('assets', [])
                  if a.get('name') == src['filename']), None)
    if not asset:
        raise UpdateError(
            f'asset {src["filename"]!r} not found in release '
            f'{src["tag"]} of {src["owner"]}/{src["repo"]}')

    # GitHub publishes a digest for every current release asset, so a
    # missing one is an anomaly, not a policy choice: refuse under BOTH
    # policies. POLICY_REDUCED exists for the publishers that never publish
    # a digest at all (GitLab, the Gitea family, a plain URL).
    digest = asset.get('digest') or ''
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise UpdateError('GitHub release asset exposes no sha256 digest; '
                          'refusing to install an unverified artifact')

    dest = os.path.join(dest_dir, 'download.appimage')
    net.download_to_file(src['url'], dest)
    expected = digest[len('sha256:'):]
    actual = get_file_hash(dest, alg='sha256')
    if actual != expected:
        raise UpdateError('Downloaded AppImage failed the sha256 digest '
                          f'check (expected {expected}, got {actual})')
    if is_appimage is not None and not is_appimage(dest):
        raise UpdateError('downloaded file is not an AppImage')
    return dest



class UpdateError(Exception):
    """Raised for invalid --set-update-source configurations and for a
    downloaded update that failed verification, or a source that exposes
    no cryptographic digest at all (install-time refusals)."""


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
        # True once a check reached a verdict (True or False, never None):
        # only then can installability be reported instead of guessed.
        self.checked = False

    # ---- installability (issue #39) ---------------------------------------

    # How this source can prove the artifact it offers: 'sha256' (a
    # published digest), 'zsync-sha1' (a control file), 'size' (a byte count
    # and nothing else) or 'none'. Only the two digest methods are accepted
    # by download(), so the verdict lives here once instead of being
    # restated by every caller that wants to know.
    verification = 'none'

    def verification_method(self) -> str:
        return self.verification

    def installable(self):
        """True when a fetch under the required policy would be attempted,
        False when that fetch must refuse, None before any verdict exists.

        Reporting only: this never relaxes the policy, it lets the UI stop
        offering an update that the download path would reject."""
        if not self.checked:
            return None
        return self.verification_method() in ('sha256', 'zsync-sha1')

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

    def describe(self) -> dict:
        """Offline: what this source would install, and how it could
        prove the artifact (issue #45). No network, no side effects."""
        return {
            'manager': self.name,
            'embedded': bool(self.embedded),
            'available_version': self.available_version,
            'download_size': self.download_size,
            'verification': self.verification_method(),
            'installable': self.installable(),
        }

    def fetch(self, dest_dir: str, policy: str, progress_cb=None,
              is_appimage=None) -> str:
        """Download the artifact under `policy` (issue #45).

        POLICY_STRICT refuses a source without a digest before any byte is
        fetched; POLICY_REDUCED lets the publisher's own reduced path
        decide. Verification is never skipped where a digest exists."""
        check_policy(policy)
        if self.verification_method() in DIGEST_METHODS:
            return self.download(dest_dir, progress_cb)
        if policy == POLICY_STRICT:
            raise UpdateError(f'{self.label}: this source exposes no usable '
                              'digest; refusing to install an unverified '
                              'artifact')
        return self.fetch_unverified(dest_dir, progress_cb, is_appimage)

    def fetch_unverified(self, dest_dir: str, progress_cb=None,
                         is_appimage=None) -> str:
        """POLICY_REDUCED path for a source with no digest. Publishers
        that can serve a plain artifact override this."""
        raise UpdateError(f'{self.label}: no artifact can be fetched '
                          'without a digest from this source')

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
        """The artifact must be exactly `expected` bytes. Size is never
        an authenticity binding, but a mismatch with the size the source
        advertised means the release metadata and the artifact disagree,
        so it is still refused (used only as a cross-check on top of a
        verified digest)."""
        actual = os.path.getsize(path)
        if actual != expected:
            raise UpdateError(f'Downloaded file has {actual} bytes but '
                              f'{source} advertised {expected}')

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
        # test seam (net._ALLOW_LOCAL_FOR_TESTS) also accepts http so
        # the suite's loopback fixture servers can be configured
        # end-to-end.
        schemes = ('https://', 'http://') if net._ALLOW_LOCAL_FOR_TESTS \
            else ('https://',)
        url = str(config.get('url', ''))
        if not url.startswith(schemes):
            raise UpdateError('Enter a valid HTTPS url')

    def verification_method(self) -> str:
        # A plain static URL has no digest, so download() refuses it; only
        # the control file's SHA-1 line binds the artifact. When the last
        # check read a control file that carried no SHA-1 line, that is a
        # fact about this source, not a guess: report the size-only method
        # so installable goes false instead of promising a digest.
        if getattr(self, '_zsync_sha1', None) is False:
            return 'size'
        if self.get_embedded_url() or \
                (self.get_config().get('url') or '').endswith('.zsync'):
            return 'zsync-sha1'
        return 'size'

    def fetch_unverified(self, dest_dir: str, progress_cb=None,
                         is_appimage=None) -> str:
        url = self.get_embedded_url() or (self.get_config().get('url') or '')
        if not url:
            raise UpdateError('Missing download URL')
        return download_plain(url, dest_dir, POLICY_REDUCED,
                              self.download_size or None, is_appimage)

    def get_embedded_url(self) -> str:
        if not self.embedded:
            return None
        if self.embedded.startswith(('https://', 'http://')):
            return self.embedded
        # strip the 'zsync|' prefix
        return self.embedded[len(self.handles_embedded):]

    def _fetch_zsync(self, zsync_url: str) -> tuple:
        """Delegates to the module's single control-file reader."""
        return fetch_zsync_control(zsync_url)

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
                self._zsync_sha1 = bool(match)
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

        if dwnl_url and dwnl_url.endswith('.zsync'):
            # A configured control file is checked the way the embedded one
            # is: its SHA-1 line is the only binding download() accepts, so
            # comparing byte counts would report an update that the install
            # then refuses.
            try:
                zsync = net.fetch_text(dwnl_url)
            except net.NetworkError as e:
                logging.debug('zsync check failed: %s', e)
                return None
            match = re.search(r'SHA-1:\s*([0-9a-f]{40})',
                              zsync.split('\n\n', 1)[0])
            self._zsync_sha1 = bool(match)
            if not match:
                return None
            return match.group(1) != \
                get_file_hash(self.el.file_path, alg='sha1')

        headers = net.head_headers(dwnl_url) if dwnl_url else {}
        remote_size = _header_content_length(headers)
        if remote_size <= 0:
            return False

        self.download_size = remote_size
        return remote_size != self._local_size()

    def download(self, dest_dir: str, progress_cb=None) -> str:
        url = self.get_config().get('url')
        e_url = self.get_embedded_url()
        if e_url:
            url = e_url
        if not url:
            raise UpdateError('Missing download URL')

        # Authenticity is mandatory (marketplace review round 2): a
        # static source is only installable through a zsync control
        # file, whose SHA-1 line is the AppImageSpec's publisher-attested
        # binding for the target artifact. A plain static URL exposes no
        # digest at all, so it is refused without downloading anything.
        if not (e_url or url.endswith('.zsync')):
            raise UpdateError('static URL sources provide no cryptographic '
                              'digest; refusing to install an unverified '
                              'artifact')

        target_url, expected_sha1 = self._fetch_zsync(url)
        if not expected_sha1:
            # fail closed: without the control file's SHA-1 line there
            # is nothing cryptographic to verify the artifact against
            raise UpdateError(f'{url}: zsync control file has no SHA-1 '
                              'line; refusing to install an unverified '
                              'artifact')
        if not target_url:
            raise UpdateError('Missing download URL')

        dest = os.path.join(dest_dir, 'update.appimage')
        try:
            net.download_to_file(target_url, dest, progress_cb)
            self._verify_sha1(dest, expected_sha1)
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
        # kept for verification_method(): the answer is what the check just
        # looked at, not a second network round trip
        self._target_asset = target_asset
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

    def verification_method(self) -> str:
        target = getattr(self, '_target_asset', None)
        if not target:
            return 'none'
        digest = (target.get('asset') or {}).get('digest') or ''
        # download() requires this digest even when the flow also picked up
        # a .zsync sibling, so the pair alone never makes it installable.
        return 'sha256' if re.fullmatch(r'sha256:[0-9a-f]{64}', digest) \
            else 'size'

    def download(self, dest_dir: str, progress_cb=None) -> str:
        target_asset = self.fetch_target_asset()
        if not target_asset:
            raise UpdateError(f'Missing target asset for {self.name}')

        asset = target_asset['asset']

        # Authenticity is mandatory (marketplace review round 2): the
        # release asset's `sha256:<64 hex>` digest is the only
        # authoritative binding the GitHub API exposes, so it is
        # required — missing or malformed refuses BEFORE any byte is
        # downloaded. (The embedded flow's target is the non-zsync
        # sibling asset, so its digest is what must be present.)
        digest = asset.get('digest') or ''
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise UpdateError('release asset has no sha256 digest; '
                              'refusing to install an unverified artifact')

        dest = os.path.join(dest_dir, 'update.appimage')
        try:
            net.download_to_file(
                asset['browser_download_url'], dest, progress_cb)
            self._verify_sha256(dest, digest[len('sha256:'):])

            # belt and braces: when the embedded flow selected a .zsync
            # twin, the control file's SHA-1 line is a second
            # publisher-attested binding for the same artifact
            if target_asset['zsync']:
                zsync = net.fetch_text(
                    target_asset['zsync']['browser_download_url'])
                match = re.search(r'SHA-1:\s*([0-9a-f]{40})',
                                  zsync.split('\n\n', 1)[0])
                if match:
                    self._verify_sha1(dest, match.group(1))

            # size is no longer an authenticity binding, but a mismatch
            # with the advertised size still means the release metadata
            # and the artifact disagree — refuse
            size = asset.get('size') or 0
            if size > 0:
                self._verify_size(dest, size, 'the release asset')
        except BaseException:
            _unlink_quietly(dest)
            raise
        return dest


class GitlabUpdater(UpdateManager):
    verification = 'size'
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
        # Authenticity is mandatory (marketplace review round 2): the
        # GitLab release API exposes no digest metadata, so there is no
        # authoritative binding to verify an artifact against — refuse
        # instead of trusting an uncorroborated download. The availability
        # check stays fully informative; only installation refuses.
        raise UpdateError(f'{self.label} release sources expose no digest '
                          'metadata; refusing to install an unverified '
                          'artifact')


class _GiteaApiUpdater(UpdateManager):
    verification = 'size'
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
        # Authenticity is mandatory (marketplace review round 2): the
        # Gitea API exposes no digest metadata, so there is no
        # authoritative binding to verify an artifact against — refuse
        # instead of trusting an uncorroborated download. The
        # availability check stays fully informative; only installation
        # refuses.
        raise UpdateError(f'{self.label} release sources expose no digest '
                          'metadata; refusing to install an unverified '
                          'artifact')


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

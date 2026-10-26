# urls.py — the --integrate URL layer (ticket #56): the release-URL
# classifier, the release-API asset lookup and the update-source auto-setup
# the install URL triggers. Pure decision logic over URL shape plus the
# per-app config writes; the downloads live in updaters, the verbs in
# commands/integrate.py.
#
# Derived from GearLever (c) mijorus, GPL-3.0.

import fnmatch
import logging
from urllib.parse import quote, unquote, urlsplit

from .. import net
from ..ini_config import Config


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


def _as_url(value: str):
    """urlsplit(value) when the argument looks like a URL ('scheme://'),
    else None (a plain path, possibly one that does not exist)."""
    if '://' not in value:
        return None
    try:
        return urlsplit(value)
    except ValueError:
        return None



# net.py — HTTP helpers on top of urllib (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# The `requests` calls scattered across GearLever's updaters
# (models/*Updater.py) are funnelled through this module so every request
# shares one User-Agent, one timeout policy and one error shape.

import json
import logging
import urllib.error
import urllib.request

from . import constants

USER_AGENT = (f'{constants.APP_ID}/{constants.BACKEND_VERSION} '
              '(+https://github.com/glasschan/appimage-for-omarchy)')

DEFAULT_TIMEOUT = 20

# Small, fast endpoints used only to answer "is there a network at all"
# (mirrors GearLever's lib/utils.check_internet).
_CONNECTIVITY_PROBES = (
    'https://api.github.com/',
    'https://fedoraproject.org/static/hotspot.txt',
)


class NetworkError(Exception):
    """A request failed (DNS, timeout, HTTP error status)."""


def _open(url: str, method: str = 'GET', timeout: float = DEFAULT_TIMEOUT):
    request = urllib.request.Request(
        url, method=method, headers={'User-Agent': USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def fetch_text(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """GET `url` and return the body as text (raises NetworkError)."""
    try:
        with _open(url, timeout=timeout) as response:
            return response.read().decode('utf-8', errors='replace')
    except Exception as e:
        raise NetworkError(f'{url}: {e}') from e


def fetch_json(url: str, timeout: float = DEFAULT_TIMEOUT):
    """GET `url` and parse the body as JSON (raises NetworkError)."""
    try:
        return json.loads(fetch_text(url, timeout=timeout))
    except json.JSONDecodeError as e:
        raise NetworkError(f'{url}: invalid JSON ({e})') from e


def head_headers(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Headers for `url` via HEAD, falling back to an un-read GET stream
    (some servers reject HEAD), like GearLever's StaticFileUpdater.

    Keys are lower-cased so callers can look them up without worrying
    about the server's header casing (a plain dict lookup would be
    case-sensitive). Returns {} when both attempts fail."""
    for method in ('HEAD', 'GET'):
        try:
            with _open(url, method=method, timeout=timeout) as response:
                return {k.lower(): v
                        for k, v in response.headers.items()}
        except Exception as e:
            logging.debug('%s %s failed: %s', method, url, e)
    return {}


def download_to_file(url: str, dest_path: str, progress_cb=None,
                     timeout: float = DEFAULT_TIMEOUT) -> int:
    """Stream `url` to `dest_path` in 1 MiB chunks; returns the byte count.

    progress_cb(fraction) fires at most every ~1% with a value in [0, 1]
    (or not at all when the server sends no Content-Length). Raises
    NetworkError on transport failure or a short body."""
    try:
        with _open(url, timeout=timeout) as response, \
                open(dest_path, 'wb') as out:
            total = int(response.headers.get('Content-Length') or 0)
            done = 0
            next_notify = 0.01
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total and progress_cb and done / total >= next_notify:
                    next_notify += 0.01
                    try:
                        progress_cb(done / total)
                    except Exception:
                        progress_cb = None
    except NetworkError:
        raise
    except Exception as e:
        raise NetworkError(f'{url}: {e}') from e

    if total and done < total:
        raise NetworkError(f'{url}: connection closed early '
                           f'({done} of {total} bytes)')
    return done


def check_internet(timeout: float = 3) -> bool:
    """True when any connectivity probe answers (best-effort, no raise)."""
    for url in _CONNECTIVITY_PROBES:
        try:
            with _open(url, method='HEAD', timeout=timeout):
                return True
        except urllib.error.HTTPError:
            # Any HTTP answer still proves there is a network path.
            return True
        except Exception:
            continue
    return False

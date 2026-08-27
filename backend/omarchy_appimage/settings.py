# settings.py — user settings stored as plain JSON (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Replaces GearLever's GSettings-backed models/Settings.py with a JSON
# file at $XDG_CONFIG_HOME/io.github.glasschan.appimage/settings.json,
# per PRD §7.2. Key names mirror the upstream GSettings keys so the
# semantics stay comparable.

import json
import logging
import os

from . import constants
from .utils import atomic_write

_DEFAULTS = {
    'appimages_default_folder': constants.DEFAULT_APPIMAGES_FOLDER,
    'manage_files_outside_default_folder': constants.DEFAULT_MANAGE_OUTSIDE,
    'move_appimage_on_integration': constants.DEFAULT_MOVE_ON_INTEGRATION,
    # F8 background update checks (PRD decision #3): first check shortly
    # after login, then every 6 hours by default.
    'update_check_enabled': constants.DEFAULT_UPDATE_CHECK_ENABLED,
    'update_check_interval_minutes': constants.DEFAULT_UPDATE_CHECK_INTERVAL,
    'update_check_delay_minutes': constants.DEFAULT_UPDATE_CHECK_DELAY,
}


def load_settings() -> dict:
    settings = dict(_DEFAULTS)
    path = constants.settings_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            settings.update({k: v for k, v in data.items() if k in _DEFAULTS})
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning('Could not read %s: %s', path, e)
    return settings


def save_settings(settings: dict):
    merged = dict(_DEFAULTS)
    merged.update({k: v for k, v in settings.items() if k in _DEFAULTS})
    atomic_write(constants.settings_path(),
                 json.dumps(merged, indent=2).encode('utf-8'))


def get_setting(key: str, default=None):
    value = load_settings().get(key, _DEFAULTS.get(key))
    return default if value is None else value


def appimages_folder() -> str:
    """Absolute path of the managed AppImages folder (default ~/AppImages),
    with `~` expanded like GearLever's _get_appimages_default_destination_path."""
    folder = load_settings()['appimages_default_folder']
    return os.path.abspath(os.path.expanduser(folder))


# ------------------------------------------------------- update-check state
#
# updates-state.json remembers the signature (available_version +
# download_size) of the last update --fetch-updates saw for each app, so
# the desktop notification only fires for NEWLY available updates
# (upstream BackgroudUpdatesFetcher notifies on every run instead; the
# dedupe is a deviation asked for by this project's spec).

def updates_state_path() -> str:
    return os.path.join(constants.plugin_config_dir(), 'updates-state.json')


def load_updates_state() -> dict:
    """The stored {desktop_id: signature} map ({} on any read problem)."""
    path = updates_state_path()
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        pass
    except Exception as e:
        logging.warning('Could not read %s: %s', path, e)
    return {}


def save_updates_state(state: dict):
    atomic_write(updates_state_path(),
                 json.dumps(state, indent=2, ensure_ascii=False).encode('utf-8'))

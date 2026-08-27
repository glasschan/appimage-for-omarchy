# constants.py — application constants.
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Managed-folder and desktop-file defaults mirror GearLever's GSettings
# schema (data/it.mijorus.gearlever.gschema.xml).

import os

APP_ID = 'io.github.glasschan.appimage'
APP_NAME = 'AppImage for Omarchy'

# Mirror of GearLever's `appimages-default-folder` GSettings default.
DEFAULT_APPIMAGES_FOLDER = '~/AppImages'

# Mirror of GearLever's `manage-files-outside-default-folder` default.
DEFAULT_MANAGE_OUTSIDE = True

# Mirror of GearLever's `move-appimage-on-integration` default.
DEFAULT_MOVE_ON_INTEGRATION = True

# Background update checks (F8, PRD decision #3): enabled, first check 5
# minutes after the service starts, then every 6 hours.
DEFAULT_UPDATE_CHECK_ENABLED = True
DEFAULT_UPDATE_CHECK_INTERVAL = 360
DEFAULT_UPDATE_CHECK_DELAY = 5

# update-check scheduling bounds enforced by --set-setting (a shorter
# interval would flirt with the PRD §2 no-polling budget; 15 minutes is
# the floor, matching the fastest sensible manual-refresh cadence).
MIN_UPDATE_CHECK_INTERVAL = 15

BACKEND_VERSION = '0.2.0'

JSON_SCHEMA_VERSION = 1


def home_dir() -> str:
    return os.path.expanduser('~')


def xdg_data_home() -> str:
    """Respect $XDG_DATA_HOME (default ~/.local/share).

    NOTE: GearLever hard-codes ~/.local/share for desktop files (it does
    NOT honour XDG_DATA_HOME); honouring it is a deliberate deviation so
    that tests and sandboxes can isolate the backend."""
    value = os.environ.get('XDG_DATA_HOME')
    if value:
        return os.path.abspath(os.path.expanduser(value))
    return os.path.join(home_dir(), '.local', 'share')


def xdg_config_home() -> str:
    """Respect $XDG_CONFIG_HOME (default ~/.config)."""
    value = os.environ.get('XDG_CONFIG_HOME')
    if value:
        return os.path.abspath(os.path.expanduser(value))
    return os.path.join(home_dir(), '.config')


def user_applications_dir() -> str:
    return os.path.join(xdg_data_home(), 'applications')


def plugin_config_dir() -> str:
    return os.path.join(xdg_config_home(), APP_ID)


def settings_path() -> str:
    return os.path.join(plugin_config_dir(), 'settings.json')


def app_config_path() -> str:
    return os.path.join(plugin_config_dir(), 'apps.ini')

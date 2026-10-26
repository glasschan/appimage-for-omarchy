# provider.py — AppImage integration provider (stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Ported from src/providers/AppImageProvider.py with the infra layer
# replaced:
#   * Gio.File / content-type queries  -> os.path + magic-byte checks (elf.py)
#   * GLib.get_home_dir() + hard-coded ~/.local/share -> XDG helpers (constants.py)
#   * Gio.Settings                     -> JSON settings (settings.py)
#   * pyxdg DesktopEntry + desktop_entry_lib -> desktop_entry.DesktopEntry
#   * 7zz/unsquashfs/appimage-extract  -> extractor.py (never executes the AppImage)
#   * Gio.File.trash()                 -> trash.py (FreeDesktop spec)
# Everything else (naming, conflict resolution, desktop entry rewriting,
# update logic hooks) follows upstream behaviour.

import dataclasses
import filecmp
import logging
import os
import re
import shlex
import stat
import tempfile
from types import SimpleNamespace
from typing import List, Optional

from . import elf, extractor, settings, trash, updaters
from .desktop_entry import DesktopEntry
from .ini_config import Config
from .provider_types import (AppImageListElement, AppImageUpdateLogic,
                             InstallPlan, InstalledStatus, InternalError,
                             _HASH_SUFFIX_RE)
from .install_writes import (_atomic_install, _atomic_write_bytes,
                              _replace_destination, _resolve_install_names,
                              _user_applications_dir, resolve_install_names)
from .provider_listing import ListingMixin
from .provider_install import InstallMixin
from .provider_update import UpdateMixin
from .provider_uninstall import UninstallMixin
from .utils import (atomic_write, extract_terminal_arguments, get_file_hash,
                    is_app_running, remove_special_chars, run_command)

# Split modules (ticket 57): these names moved verbatim and are re-exported
# here so the existing import surface (`omarchy_appimage.provider`) is
# unchanged for cli.py and the test suite.


class AppImageProvider(ListingMixin, InstallMixin, UpdateMixin,
                       UninstallMixin):
    name = 'AppImage'
    desktop_exec_codes = ['%f', '%F', '%u', '%U', '%i', '%c', '%k']

    def __init__(self):
        # Desktop entries this provider instance has repaired; survives
        # across list_installed() calls (the panel polls) so a desktop
        # that keeps clobbering itself isn't re-written on every call.
        self._repaired_desktop_paths = set()

    @property
    def user_desktop_files_path(self) -> str:
        """XDG_DATA_HOME/applications, resolved on every access so that
        tests (and sandboxes) can redirect it via the environment."""
        return _user_applications_dir()

    def _get_appimages_default_destination_path(self) -> str:
        return settings.appimages_folder()

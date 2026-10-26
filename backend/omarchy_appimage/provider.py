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
from .utils import (atomic_write, extract_terminal_arguments, get_file_hash,
                    is_app_running, remove_special_chars, run_command)

# Split modules (ticket 57): these names moved verbatim and are re-exported
# here so the existing import surface (`omarchy_appimage.provider`) is
# unchanged for cli.py and the test suite.


class AppImageProvider(ListingMixin, InstallMixin, UpdateMixin):
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

    # ------------------------------------------------------------ uninstall

    def uninstall(self, el: AppImageListElement, force_delete=False,
                  remove_configuration=True):
        """Remove an integrated app.

        Deviation from GearLever: upstream only trashes the AppImage and
        hard-deletes the .desktop file and icon; per this project's spec
        (PRD F3) all three artifacts are sent to the trash (with a
        delete fallback, like upstream's binary handling).

        Marketplace-review hardening: the desktop entry is mutable, so a
        path taken from it (Icon= especially) is only removed when it is
        bound to this app — either equal to the path recorded at install
        time in apps.ini, or inside the managed-folder layout (AppImage
        in the managed folder, desktop file in the user applications
        dir, icon in <managed folder>/.icons with a stem matching the
        desktop id). Anything unbound is logged and skipped; directories
        and FIFOs are never removed."""
        logging.info('Removing %s', el.file_path)

        def _remove(path: str):
            if not path:
                return
            try:
                st = os.lstat(path)
            except OSError:
                return
            # only regular files and symlinks qualify
            if not (stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)):
                logging.warning('refusing to remove non-regular path: %s',
                                path)
                return
            if force_delete:
                os.remove(path)
                return
            try:
                trash.send_to_trash(path)
            except Exception as e:
                logging.warning('Trashing %s failed (%s); removing it '
                                'instead...', path, e)
                os.remove(path)

        def _inside(real: str, container: str) -> bool:
            return bool(container) and \
                (real == container or real.startswith(container + os.sep))

        app_config = Config.get_app_config(el)
        default_folder = os.path.realpath(
            self._get_appimages_default_destination_path())
        desktop_path = (el.desktop_entry.getFileName()
                        if el.desktop_entry else None)

        # 1) the AppImage: recorded path or inside the managed folder
        if el.file_path:
            real = os.path.realpath(el.file_path)
            recorded = app_config.get('appimage_path') or ''
            if (bool(recorded) and real == os.path.realpath(recorded)) \
                    or _inside(real, default_folder):
                _remove(el.file_path)
            else:
                logging.warning('not removing unbound AppImage path: %s',
                                el.file_path)

        # 2) the .desktop file: recorded path or in user applications
        if desktop_path:
            real = os.path.realpath(desktop_path)
            applications_dir = os.path.realpath(_user_applications_dir())
            recorded = app_config.get('desktop_file_path') or ''
            if (bool(recorded) and real == os.path.realpath(recorded)) \
                    or _inside(real, applications_dir):
                try:
                    _remove(desktop_path)
                except FileNotFoundError:
                    logging.warning('desktop file already gone')
            else:
                logging.warning('not removing unbound desktop file: %s',
                                desktop_path)

        # 3) the icon, only when tied to this app's entry: recorded path,
        #    or inside <managed folder>/.icons with the desktop id's stem
        #    A stale record (its .desktop file is gone) has no entry to
        #    read: the icon path recorded at install time is used instead.
        icon = el.desktop_entry.getIcon() if el.desktop_entry else ''
        if not icon:
            icon = app_config.get('icon_path') or ''
        if icon and '/' in icon:
            real = os.path.realpath(icon)
            icons_dir = os.path.join(default_folder, '.icons')
            desktop_stem = os.path.splitext(
                os.path.basename(desktop_path or ''))[0]
            stem_matches = (desktop_stem
                            == os.path.splitext(
                                os.path.basename(real))[0])
            recorded = app_config.get('icon_path') or ''
            if (bool(recorded) and real == os.path.realpath(recorded)) \
                    or (_inside(real, icons_dir) and stem_matches):
                _remove(icon)
            else:
                logging.warning('not removing unbound icon path: %s',
                                icon)

        if remove_configuration:
            Config.delete_app_update_config(el)

        el.set_installed_status(InstalledStatus.NOT_INSTALLED)
        Config.delete_app_config(el)
        Config.delete_app_update_config(el)

    def _get_appimages_default_destination_path(self) -> str:
        return settings.appimages_folder()

# provider_uninstall.py — app removal (ticket 57).
#
# Split out of provider.py as a mixin: `AppImageProvider` composes it, so
# `uninstall` keeps its exact behaviour — the containment-bound removal
# (recorded-path or managed-layout only, never directories/FIFOs,
# trash with delete fallback) moved verbatim.

import logging
import os
import stat

from . import trash
from .ini_config import Config
from .install_writes import _user_applications_dir
from .provider_types import InstalledStatus


class UninstallMixin:
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

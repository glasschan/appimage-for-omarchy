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
from .utils import (atomic_write, extract_terminal_arguments, get_file_hash,
                    is_app_running, remove_special_chars, run_command)

# Split modules (ticket 57): these names moved verbatim and are re-exported
# here so the existing import surface (`omarchy_appimage.provider`) is
# unchanged for cli.py and the test suite.


class AppImageProvider(ListingMixin, InstallMixin):
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

    def apply_release_version(self, el: AppImageListElement,
                              version: str) -> bool:
        """Record a release tag as the app's version when the downloaded
        AppImage does not embed one (v0.9.4).

        `X-AppImage-Version` is the only version source (see
        _get_app_version), and plenty of real AppImages ship without it —
        Terax and OpenTypeless, for instance, carry no version anywhere in
        the image. Their release tag is the only version information there
        is, so `--integrate .../releases/download/v0.9.0/Terax_0.9.0_amd64.
        AppImage` used to produce an app with no version at all and the
        panel had nothing to show. Both `--integrate` (whose classifier
        knows the tag from the URL or from the release API) and
        `update_from_url` (whose manager reports the release it just
        downloaded) call this before install_file, so the desktop entry's
        `Name (version)` suffix and the reported current_version describe
        the release actually installed.

        Never overrides a version the image states itself and never invents
        one: a blank tag is a no-op. Returns whether it applied."""
        version = (version or '').strip()
        if not version:
            return False
        extracted = el.extracted or self._load_appimage_metadata(el)
        entry = extracted.desktop_entry if extracted else None
        if entry is None or entry.get('X-AppImage-Version'):
            return False
        entry.set_custom('X-AppImage-Version', version)
        if el.desktop_entry is not None and el.desktop_entry is not entry:
            el.desktop_entry.set_custom('X-AppImage-Version', version)
        el.version = version
        return True

    def apply_version_to_installed(self, el: AppImageListElement,
                                    version: str) -> bool:
        """Record a release tag on an ALREADY integrated app that states no
        version of its own (v0.9.4 follow-up).

        Re-integrating the very same URL is a no-op (`already-integrated`,
        nothing installed), so an app integrated by an older plugin — one
        that predates apply_release_version, or simply carries no
        X-AppImage-Version — would keep its blank version until a newer
        release happened to arrive. When the pasted URL names a tag, write
        it into the installed desktop entry instead: metadata only, the
        AppImage itself is untouched. A leftover GearLever md5 suffix in
        the display Name (`Terax (77efdf)`) is cleaned off at the same
        time so the launcher and the panel agree. Returns whether anything
        was written."""
        version = (version or '').strip()
        if not version:
            return False
        # `el` is the temp download whose file_path is_installed() just
        # pointed at the managed copy; the installed element is the one that
        # carries the desktop entry / desktop file path.
        installed = next((a for a in self.list_installed()
                          if a.file_path == el.file_path), None)
        if installed is None or not installed.desktop_entry \
                or not installed.desktop_file_path:
            return False
        if installed.desktop_entry.get('X-AppImage-Version'):
            return False
        entry = DesktopEntry(path=installed.desktop_file_path)
        entry.set_custom('X-AppImage-Version', version)
        name = entry.getName()
        match = _HASH_SUFFIX_RE.match(name)
        if match:
            entry.Name.default_text = match.group(1).rstrip()
        entry.write_file(installed.desktop_file_path)
        installed.desktop_entry = DesktopEntry(path=installed.desktop_file_path)
        installed.name = installed.desktop_entry.getName()
        installed.version = installed.desktop_entry.get('X-AppImage-Version')
        # the caller reports THIS element
        el.desktop_entry = installed.desktop_entry
        el.desktop_file_path = installed.desktop_file_path
        el.name = installed.name
        el.version = installed.version
        return True

    def update_from_url(self, manager, el: AppImageListElement,
                        keep_both=False, progress_cb=None,
                        before_replace=None) -> AppImageListElement:
        """Download an update through `manager` and install it over `el`.

        Ported from AppImageProvider.update_from_url with the Gio.File
        calls replaced by plain paths and the GTK progress callback
        replaced by an optional progress_cb(fraction). Deviations:
          * the download lands in a temp dir from
            extractor.new_temp_dir('appimage-update-'), which the shared
            update core (cli.run_update) removes — scoped to the dirs
            this call registered — right after the call; the global
            cleanup_temp_dirs() wipe stays in main.py's process-exit
            finally-block as a backstop (upstream kept it in a per-run
            extraction folder);
          * `keep_both` lets the caller pick AppImageUpdateLogic.KEEP —
            upstream's CLI path always installs with REPLACE;
          * on success we only clear updating_from here, update_logic is
            left set so callers can tell KEEP from REPLACE installs;
          * `before_replace` is handed to install_file (see there): the
            running-app grant of ticket 26 needs one hook immediately
            before the atomic replace.
        Raises InternalError when the download is not an AppImage."""
        dest_dir = extractor.new_temp_dir(prefix='appimage-update-')
        # An update is the strict path (issue #45): a source that cannot
        # prove its artifact is refused before any byte is downloaded.
        update_file_path = manager.fetch(dest_dir, updaters.POLICY_STRICT,
                                         progress_cb,
                                         is_appimage=self.can_install_file)

        if not self.can_install_file(update_file_path):
            raise InternalError('The downloaded file is not a valid AppImage, '
                                'please check if the provided URL is correct')

        list_element = self.create_list_element_from_file(update_file_path,
                                                          return_new_el=True)

        list_element.update_logic = (AppImageUpdateLogic.KEEP if keep_both
                                     else AppImageUpdateLogic.REPLACE)
        list_element.updating_from = el
        # The manager knows the release it downloaded: use its tag as the
        # version when the image carries none of its own (v0.9.4).
        self.apply_release_version(list_element,
                                   getattr(manager, 'available_version', ''))
        self.install_file(list_element, before_replace=before_replace)

        list_element.updating_from = None

        # report the name/version as actually installed (the raw name is
        # the downloaded file's basename, e.g. 'update'; cmd_integrate
        # applies the same re-read after install_file)
        if list_element.desktop_entry:
            list_element.name = list_element.desktop_entry.getName()
            list_element.version = list_element.desktop_entry.get(
                'X-AppImage-Version')

        return list_element

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

    # ------------------------------------------------------------- metadata

    def _load_appimage_metadata(self, el: AppImageListElement) -> extractor.ExtractedAppImage:
        if el.extracted:
            return el.extracted

        result = extractor.load_appimage_metadata(el.file_path)
        el.extracted = result
        el.desktop_entry = result.desktop_entry
        return result

    def _get_appimages_default_destination_path(self) -> str:
        return settings.appimages_folder()

    def _get_app_version(self, extracted_appimage=None,
                         desktop_entry: DesktopEntry = None):
        """The app's real version: X-AppImage-Version only.

        GearLever's md5 fallback (first six hex chars of the file hash) is
        deliberately gone: it is a hash, not a version, and it used to
        leak into the installed desktop entry's Name as
        "Terax (77efdf)" — where the launcher and the panel showed it as
        the app's version. An image that ships no X-AppImage-Version has
        no version, and every caller now renders nothing instead."""
        if not desktop_entry:
            desktop_entry = extracted_appimage.desktop_entry \
                if extracted_appimage else None

        if desktop_entry:
            return desktop_entry.get('X-AppImage-Version', '') or ''
        return ''

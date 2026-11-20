# provider_update.py — update-from-URL, version recording and image
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
# metadata.

import logging

from . import extractor, updaters
from .desktop_entry import DesktopEntry
from .provider_types import (AppImageListElement, AppImageUpdateLogic,
                             InternalError, _HASH_SUFFIX_RE)


class UpdateMixin:
    def apply_release_version(self, el: AppImageListElement,
                              version: str) -> bool:
        """Record a release tag as the app's version when the downloaded
        AppImage does not embed one.

        `X-AppImage-Version` is the only version source (see
        _get_app_version), and plenty of real AppImages ship without it,
        so their release tag is the only version information there is.
        `--integrate` and `update_from_url` both call this before
        install_file, so the `Name (version)` suffix and the reported
        current_version describe the release actually installed.

        Never overrides a version the image states itself and never
        invents one: a blank tag is a no-op."""
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
        version of its own.

        Re-integrating the same URL is a no-op, so an app carrying no
        X-AppImage-Version would keep its blank version until a newer
        release arrived. When the pasted URL names a tag, write it into the
        installed desktop entry: metadata only, the AppImage is untouched.
        A leftover GearLever md5 suffix in the display Name is cleaned off
        at the same time so the launcher and the panel agree."""
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

        Ported from AppImageProvider.update_from_url with plain paths and
        an optional progress_cb(fraction). Deviations:
          * the download lands in a temp dir from
            extractor.new_temp_dir('appimage-update-'), which the shared
            update core removes right after the call; the global
            cleanup_temp_dirs() wipe stays in main.py's process-exit
            backstop;
          * `keep_both` lets the caller pick AppImageUpdateLogic.KEEP,
            where upstream's CLI path always installs with REPLACE;
          * on success we only clear updating_from here; update_logic is
            left set so callers can tell KEEP from REPLACE installs;
          * `before_replace` is handed to install_file: the running-app
            grant needs one hook immediately before the atomic replace.
        Raises InternalError when the download is not an AppImage."""
        dest_dir = extractor.new_temp_dir(prefix='appimage-update-')
        # An update is the strict path: a source that cannot prove its
        # artifact is refused before any byte is downloaded.
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
        # version when the image carries none of its own.
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

    def _get_app_version(self, extracted_appimage=None,
                         desktop_entry: DesktopEntry = None):
        """The app's real version: X-AppImage-Version only.

        GearLever's md5 fallback is deliberately gone: it is a hash, not a
        version, and it used to leak into the desktop entry's Name where
        the launcher and the panel showed it as the version. An image that
        ships no X-AppImage-Version has no version."""
        if not desktop_entry:
            desktop_entry = extracted_appimage.desktop_entry \
                if extracted_appimage else None

        if desktop_entry:
            return desktop_entry.get('X-AppImage-Version', '') or ''
        return ''

    def _load_appimage_metadata(self, el: AppImageListElement) -> extractor.ExtractedAppImage:
        if el.extracted:
            return el.extracted

        result = extractor.load_appimage_metadata(el.file_path)
        el.extracted = result
        el.desktop_entry = result.desktop_entry
        return result

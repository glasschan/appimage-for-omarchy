# provider_update.py — update-from-URL, version recording and image
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
# metadata (ticket 57).
#
# Split out of provider.py as a mixin: `AppImageProvider` composes it, so
# `update_from_url` and the version helpers keep their exact behaviour.
# The definitions moved verbatim.

import logging

from . import extractor, updaters
from .desktop_entry import DesktopEntry
from .provider_types import (AppImageListElement, AppImageUpdateLogic,
                             InternalError, _HASH_SUFFIX_RE)


class UpdateMixin:
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

    def _load_appimage_metadata(self, el: AppImageListElement) -> extractor.ExtractedAppImage:
        if el.extracted:
            return el.extracted

        result = extractor.load_appimage_metadata(el.file_path)
        el.extracted = result
        el.desktop_entry = result.desktop_entry
        return result

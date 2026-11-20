# provider.py — AppImage integration provider (stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
#
# Every GearLever infra layer is replaced: magic-byte checks instead of
# Gio content types, XDG helpers for paths, JSON settings for Gio.Settings,
# desktop_entry.DesktopEntry for pyxdg, extractor.py for the external
# extractors, and trash.py for Gio.File.trash(). Naming, conflict
# resolution and desktop entry rewriting follow upstream behaviour.
#
# The provider is split by responsibility; this module is the composition
# root and stable import surface:
#   provider_types.py     data types
#   install_writes.py     install name resolution + atomic file writes
#   provider_listing.py   ListingMixin
#   provider_install.py   InstallMixin (plan_install / commit_install)
#   provider_update.py    UpdateMixin (update_from_url, versions, metadata)
#   provider_uninstall.py UninstallMixin (containment-bound removal)
# The names below are re-exported verbatim so existing importers keep
# working unchanged.

from . import settings
from .install_writes import (_atomic_install, _atomic_write_bytes,
                             _replace_destination, _resolve_install_names,
                             _user_applications_dir, resolve_install_names)
from .provider_install import InstallMixin
from .provider_listing import ListingMixin
from .provider_types import (AppImageListElement, AppImageUpdateLogic,
                             InstallPlan, InstalledStatus, InternalError)
from .provider_uninstall import UninstallMixin
from .provider_update import UpdateMixin
from .utils import remove_special_chars


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

# provider_types.py — the provider's shared data types (stdlib only).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Split out of provider.py (ticket 57): InstalledStatus / InternalError /
# AppImageListElement / InstallPlan are referenced by every provider
# submodule, so they live in a module with no dependencies on the
# behaviour mixins. Behaviour is unchanged — the definitions moved
# verbatim; provider.py re-exports them.

import dataclasses
import logging
import os
import re
import stat
from enum import Enum
from typing import List, Optional

from . import extractor
from .desktop_entry import DesktopEntry
from .ini_config import Config


class InstalledStatus(Enum):
    NOT_INSTALLED = 0
    INSTALLED = 1
    INSTALLING = 6


class AppImageUpdateLogic(Enum):
    REPLACE = 'REPLACE'
    KEEP = 'KEEP'


class InternalError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# A display Name ending in GearLever's md5-prefix "version" artifact, e.g.
# "Terax (77efdf)": six lowercase hex chars, the same shape the plugin's
# lib/Model.js displayName() strips for the panel.
_HASH_SUFFIX_RE = re.compile(r'^(.*?) \(([0-9a-f]{6})\)$')


@dataclasses.dataclass
class AppImageListElement():
    name: str
    description: str
    provider: str
    installed_status: InstalledStatus
    file_path: str
    trusted: bool = False
    is_updatable_from_url = False
    env_variables: List[str] = dataclasses.field(default_factory=lambda: [])
    exec_arguments: str = ''
    desktop_entry: Optional[DesktopEntry] = None
    update_logic: Optional[AppImageUpdateLogic] = None
    architecture: Optional[str] = None
    updating_from: Optional[any] = None  # AppImageListElement
    version: Optional[str] = None
    extracted: Optional[extractor.ExtractedAppImage] = None
    local_file: Optional[bool] = None
    external_folder: bool = False
    desktop_file_path: Optional[str] = None
    size: int = 0

    def set_installed_status(self, installed_status: InstalledStatus):
        self.installed_status = installed_status

    def set_trusted(self):
        # defense in depth: only ever chmod a regular file we own
        # (the file is ours post-rename, but lstat it anyway)
        st = os.lstat(self.file_path)
        if not stat.S_ISREG(st.st_mode):
            raise InternalError('refusing to chmod a non-regular file: '
                                + self.file_path)
        logging.debug('Chmod file ' + self.file_path)
        os.chmod(self.file_path, 0o755)
        self.trusted = True

    def get_config(self):
        return Config.get_app_config(self)


@dataclasses.dataclass
class InstallPlan:
    """Everything an install will write, resolved and validated up front.

    `AppImageProvider.plan_install` returns one of these after reading the
    source image (extraction into a temp dir is a read) and proving the
    whole install can succeed; `commit_install` then performs every write
    in the order the ported GearLever flow used. Splitting the two means a
    refused install — no root desktop entry, no desktop file to point at,
    an Exec line that does not parse — leaves the managed folder, `.icons`,
    the applications directory and apps.ini untouched."""
    source_appimage: str
    dest_appimage_file: str
    desktop_id: str                       # app/desktop/icon file stem
    dest_desktop_file_path: str
    dest_icon_file: Optional[str]
    icon_bytes: Optional[bytes]
    desktop_content: bytes
    exec_arguments: str
    default_exec_arguments: str
    env_variables: List[str]
    app_config: dict
    version: str
    remove_original: bool

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
import shlex
import shutil
from enum import Enum
from typing import List, Optional

from . import elf, extractor, settings, trash
from .desktop_entry import DesktopEntry
from .ini_config import Config
from .utils import (extract_terminal_arguments, get_file_hash,
                    is_app_running, remove_special_chars, run_command)


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
        logging.debug('Chmod file ' + self.file_path)
        os.chmod(self.file_path, 0o755)
        self.trusted = True

    def get_config(self):
        return Config.get_app_config(self)


class AppImageProvider():
    name = 'AppImage'
    desktop_exec_codes = ['%f', '%F', '%u', '%U', '%i', '%c', '%k']

    def __init__(self):
        pass

    @property
    def user_desktop_files_path(self) -> str:
        """XDG_DATA_HOME/applications, resolved on every access so that
        tests (and sandboxes) can redirect it via the environment."""
        return _user_applications_dir()

    # ------------------------------------------------------------ listing

    def list_installed(self) -> list:
        default_folder_path = self._get_appimages_default_destination_path()
        manage_from_outside = settings.load_settings()[
            'manage_files_outside_default_folder']
        output = []

        if not os.path.exists(self.user_desktop_files_path):
            return output

        for file_name in os.listdir(self.user_desktop_files_path):
            desktop_path = os.path.join(self.user_desktop_files_path,
                                        file_name)

            try:
                if not os.path.isfile(desktop_path):
                    continue

                entry = DesktopEntry(path=desktop_path)
                if not entry.getName():
                    continue

                exec_location = entry.getTryExec()
                exec_command_data = extract_terminal_arguments(entry.getExec())
                exec_arguments = ' '.join(exec_command_data['arguments'])

                if os.path.isfile(exec_location):
                    exec_in_default_folder = os.path.isfile(os.path.join(
                        default_folder_path, os.path.basename(exec_location)))
                    exec_in_folder = True if manage_from_outside \
                        else exec_in_default_folder
                    app_version = self._get_app_version(
                        None, desktop_entry=entry, return_hash=False)
                    file_size = os.stat(exec_location).st_size

                    if exec_in_folder and self.can_install_file(exec_location):
                        list_element = AppImageListElement(
                            name=entry.getName(),
                            desktop_file_path=desktop_path,
                            description=entry.getComment(),
                            version=(app_version or None),
                            installed_status=InstalledStatus.INSTALLED,
                            file_path=exec_location,
                            provider=self.name,
                            desktop_entry=entry,
                            trusted=True,
                            external_folder=(not exec_in_default_folder),
                            exec_arguments=exec_arguments,
                            env_variables=exec_command_data['env_vars'],
                            size=file_size,
                        )
                        list_element.architecture = None
                        output.append(list_element)
                    else:
                        logging.debug('%s skipped because %s is not a '
                                      'supported file type',
                                      desktop_path, exec_location)
                else:
                    logging.debug('%s skipped because %s does not exist',
                                  desktop_path, exec_location)

            except Exception as e:
                logging.warning('%s: %s', desktop_path, e)

        return output

    def is_installed(self, el: AppImageListElement) -> bool:
        if el.file_path and os.path.exists(self._get_appimages_default_destination_path()):
            default_folder = self._get_appimages_default_destination_path()
            for file_name in os.listdir(default_folder):
                installed_path = os.path.join(default_folder, file_name)

                if self.can_install_file(installed_path):
                    try:
                        if filecmp.cmp(installed_path, el.file_path,
                                       shallow=False):
                            el.file_path = installed_path
                            return True
                    except OSError as e:
                        logging.debug('compare failed: %s', e)

        return False

    def is_app_running(self, el: AppImageListElement) -> bool:
        return is_app_running(el.file_path)

    # ---------------------------------------------------------- file checks

    def can_install_file(self, file_path: str) -> bool:
        """Replaces GearLever's shared-mime-info check with AppImage
        magic-byte detection (type 1 and type 2)."""
        try:
            return elf.get_appimage_type(file_path) in ('1', '2')
        except OSError:
            return False

    def get_appimage_type(self, el: AppImageListElement) -> str:
        return elf.get_appimage_type(el.file_path)

    def get_elf_arch(self, el: AppImageListElement) -> str:
        return elf.get_elf_arch(el.file_path)

    def create_list_element_from_file(self, file_path: str,
                                      return_new_el=False) -> AppImageListElement:
        if not self.can_install_file(file_path):
            raise InternalError('This file type is not supported')

        app_name = os.path.basename(file_path)
        file_size = os.stat(file_path).st_size

        el = AppImageListElement(
            name=os.path.splitext(app_name)[0],
            description='',
            version=None,
            provider=self.name,
            installed_status=InstalledStatus.NOT_INSTALLED,
            file_path=file_path,
            size=file_size,
            desktop_entry=None,
            local_file=True,
            trusted=True,
        )
        el.name = _strip_appimage_ext(app_name)
        el.architecture = self.get_elf_arch(el)

        if return_new_el:
            return el

        if self.is_installed(el):
            for installed in self.list_installed():
                try:
                    if filecmp.cmp(installed.file_path, el.file_path,
                                   shallow=False):
                        return installed
                except OSError:
                    continue

        return el

    # ------------------------------------------------------------- install

    def refresh_data(self, el: AppImageListElement):
        extracted = self._load_appimage_metadata(el)
        if extracted.desktop_entry:
            el.name = extracted.desktop_entry.getName()
            el.version = extracted.desktop_entry.get('X-AppImage-Version')

    def install_file(self, el: AppImageListElement):
        """Ported from AppImageProvider.install_file (naming, conflict
        resolution and desktop entry rewriting follow upstream)."""
        logging.info('Installing appimage: %s', el.file_path)
        el.installed_status = InstalledStatus.INSTALLING
        appimages_destination_path = self._get_appimages_default_destination_path()
        os.makedirs(appimages_destination_path, exist_ok=True)

        extracted_appimage = self._load_appimage_metadata(el)
        version = self._get_app_version(extracted_appimage, return_hash=False)

        # how the appimage will be called
        if el.update_logic == AppImageUpdateLogic.REPLACE \
                and el.updating_from is not None:
            appimage_filename = os.path.basename(el.updating_from.file_path)
            desktop_file_path = os.path.basename(
                el.updating_from.desktop_file_path)
            prefixed_filename = os.path.splitext(desktop_file_path)[0]
        else:
            source_basename = os.path.basename(el.file_path)
            appimage_filename = 'gearlever_' + os.path.splitext(source_basename)[0]

            if extracted_appimage.desktop_entry:
                appimage_filename = extracted_appimage.desktop_entry.getName()
                appimage_filename = appimage_filename.lower().replace(' ', '_')

            # (GearLever's 'exec-as-name-for-terminal-apps' setting defaults
            # to false, so the extension is always appended here)
            appimage_filename = f'{appimage_filename}.appimage'

            # NOTE: like upstream, app_name_without_ext still contains the
            # extension at this point
            app_name_without_ext = appimage_filename
            appimage_filename = remove_special_chars(appimage_filename).lower()

            i = 0
            files_in_dest_dir = os.listdir(appimages_destination_path)

            # if there is already an app with the same name,
            # we try not to overwrite
            while appimage_filename in files_in_dest_dir:
                if i == 0 and version:
                    appimage_filename = (app_name_without_ext + '_'
                                         + version.replace('.', '_'))
                else:
                    appimage_filename = app_name_without_ext + f'_{i}'

                appimage_filename = appimage_filename + '.appimage'
                i += 1

            prefixed_filename = os.path.splitext(appimage_filename)[0]

        dest_appimage_file = os.path.join(appimages_destination_path,
                                          appimage_filename)
        original_appimage_path = extracted_appimage.appimage_file

        shutil.copyfile(original_appimage_path, dest_appimage_file)
        logging.debug('file copied to %s', appimages_destination_path)

        el.file_path = dest_appimage_file
        el.set_trusted()

        # copy the icon file
        dest_appimage_icon_file = None
        icon_file = extracted_appimage.icon_file
        if icon_file and os.path.exists(icon_file):
            icons_folder = os.path.join(appimages_destination_path, '.icons')
            os.makedirs(icons_folder, exist_ok=True)

            icon_file_ext = os.path.splitext(icon_file)[1]
            dest_appimage_icon_file = os.path.join(
                appimages_destination_path, '.icons',
                prefixed_filename + icon_file_ext)
            shutil.copyfile(icon_file, dest_appimage_icon_file)

        # Move .desktop file to its default location
        os.makedirs(self.user_desktop_files_path, exist_ok=True)
        dest_desktop_file_path = os.path.join(
            self.user_desktop_files_path, prefixed_filename + '.desktop')
        dest_desktop_file_path = dest_desktop_file_path.replace(' ', '_')

        # Get default exec arguments shipped by this version of the appimage
        term_arguments = extract_terminal_arguments(
            extracted_appimage.desktop_entry.getExec())
        exec_arguments = term_arguments['arguments']
        new_default_exec_arguments = ' '.join(exec_arguments)

        # Only preserve the previous exec arguments if the user actually
        # customized them (upstream comment preserved).
        if el.updating_from and el.updating_from.exec_arguments:
            previous_config = Config.get_app_config(el.updating_from)
            previous_default_exec_arguments = previous_config.get(
                'default_exec_arguments', '')

            if el.updating_from.exec_arguments != previous_default_exec_arguments:
                exec_arguments = shlex.split(el.updating_from.exec_arguments)

        el.exec_arguments = ' '.join(exec_arguments)

        if not extracted_appimage.desktop_file:
            raise InternalError(
                'Could not find a .desktop file inside this AppImage')

        jd_desktop_entry = DesktopEntry(path=extracted_appimage.desktop_file)

        jd_desktop_entry.TryExec = dest_appimage_file
        jd_desktop_entry.Exec = shlex.join([dest_appimage_file,
                                            *exec_arguments])

        if el.update_logic is AppImageUpdateLogic.KEEP:
            v_version = self._get_app_version(extracted_appimage,
                                              return_hash=True)
            final_app_name = (extracted_appimage.desktop_entry.getName()
                              + f' ({v_version})')
            jd_desktop_entry.Name.default_text = final_app_name

        desktop_icon = 'applications-other'
        if dest_appimage_icon_file:
            desktop_icon = dest_appimage_icon_file

        jd_desktop_entry.Icon = desktop_icon

        for _action_id, action in jd_desktop_entry.Actions.items():
            a_exec_args = extract_terminal_arguments(action.Exec)
            action.Icon = desktop_icon
            action.Exec = shlex.join([dest_appimage_file,
                                      *a_exec_args['arguments']])

        if version:
            jd_desktop_entry.set_custom('X-AppImage-Version', version)
        jd_desktop_entry.set_custom(
            'X-AppImage-Name', extracted_appimage.desktop_entry.getName())

        # finally, write the new .desktop file (atomically)
        jd_desktop_entry.write_file(dest_desktop_file_path)

        if os.path.exists(dest_desktop_file_path):
            el.desktop_entry = DesktopEntry(path=dest_desktop_file_path)
            el.desktop_file_path = dest_desktop_file_path
            el.installed_status = InstalledStatus.INSTALLED

        if el.updating_from and el.updating_from.env_variables:
            el.env_variables = el.updating_from.env_variables
            self.update_desktop_file(el)

        has_desktop_integration = any(
            v.startswith('DESKTOPINTEGRATION=') for v in el.env_variables)
        if not has_desktop_integration:
            el.env_variables.append('DESKTOPINTEGRATION=1')
            self.update_desktop_file(el)

        app_config = Config.get_app_config(el)
        app_config['default_exec_arguments'] = new_default_exec_arguments
        Config.set_app_config(el, app_config)

        if settings.load_settings()['move_appimage_on_integration']:
            if os.path.dirname(original_appimage_path) != appimages_destination_path:
                logging.info('Deleting original appimage file from: %s',
                             original_appimage_path)
                os.unlink(original_appimage_path)

        try:
            run_command(['update-desktop-database',
                         self.user_desktop_files_path, '-q'],
                        check=False, timeout=30)
        except Exception as e:
            logging.debug('update-desktop-database failed: %s', e)

        el.updating_from = None

    def update_from_url(self, manager, el: AppImageListElement,
                        keep_both=False, progress_cb=None) -> AppImageListElement:
        """Download an update through `manager` and install it over `el`.

        Ported from AppImageProvider.update_from_url with the Gio.File
        calls replaced by plain paths and the GTK progress callback
        replaced by an optional progress_cb(fraction). Deviations:
          * the download lands in a temp dir from
            extractor.new_temp_dir('appimage-update-'), which main.py's
            finally-block removes via extractor.cleanup_temp_dirs()
            (upstream kept it in a per-run extraction folder);
          * `keep_both` lets the caller pick AppImageUpdateLogic.KEEP —
            upstream's CLI path always installs with REPLACE;
          * on success we only clear updating_from here, update_logic is
            left set so callers can tell KEEP from REPLACE installs.
        Raises InternalError when the download is not an AppImage."""
        dest_dir = extractor.new_temp_dir(prefix='appimage-update-')
        update_file_path = manager.download(dest_dir, progress_cb)

        if not self.can_install_file(update_file_path):
            raise InternalError('The downloaded file is not a valid AppImage, '
                                'please check if the provided URL is correct')

        list_element = self.create_list_element_from_file(update_file_path,
                                                          return_new_el=True)

        list_element.update_logic = (AppImageUpdateLogic.KEEP if keep_both
                                     else AppImageUpdateLogic.REPLACE)
        list_element.updating_from = el
        self.install_file(list_element)

        list_element.updating_from = None

        # report the name/version as actually installed (the raw name is
        # the downloaded file's basename, e.g. 'update'; cmd_integrate
        # applies the same re-read after install_file)
        if list_element.desktop_entry:
            list_element.name = list_element.desktop_entry.getName()
            list_element.version = list_element.desktop_entry.get(
                'X-AppImage-Version')

        return list_element

    def update_desktop_file(self, el: AppImageListElement):
        if not el.desktop_file_path:
            raise Exception('desktop_file_path not specified')

        jd_desktop_file = DesktopEntry(path=el.desktop_file_path)

        tryexec_command = jd_desktop_file.TryExec
        exec_arguments = el.exec_arguments
        env_vars = ' '.join(el.env_variables)

        if exec_arguments:
            exec_arguments = f' {exec_arguments}'

        if env_vars:
            env_vars = f'env {env_vars} '

        exec_command = ''.join([
            env_vars,
            shlex.quote(tryexec_command),
            exec_arguments
        ])

        jd_desktop_file.Exec = exec_command
        jd_desktop_file.write_file(el.desktop_file_path)

        el.desktop_entry = DesktopEntry(path=el.desktop_file_path)

    # ------------------------------------------------------------ uninstall

    def uninstall(self, el: AppImageListElement, force_delete=False,
                  remove_configuration=True):
        """Remove an integrated app.

        Deviation from GearLever: upstream only trashes the AppImage and
        hard-deletes the .desktop file and icon; per this project's spec
        (PRD F3) all three artifacts are sent to the trash (with a
        delete fallback, like upstream's binary handling)."""
        logging.info('Removing %s', el.file_path)

        def _remove(path: str):
            if not path:
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

        _remove(el.file_path)

        if el.desktop_entry and el.desktop_entry.getFileName():
            logging.info('Removing %s', el.desktop_entry.getFileName())
            try:
                _remove(el.desktop_entry.getFileName())
            except FileNotFoundError:
                logging.warning('desktop file already gone')

        if el.desktop_entry:
            icon = el.desktop_entry.getIcon()
            if icon and '/' in icon and os.path.isfile(icon):
                _remove(icon)

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
                         desktop_entry: DesktopEntry = None,
                         return_hash=True):
        if not desktop_entry:
            desktop_entry = extracted_appimage.desktop_entry \
                if extracted_appimage else None

        version = ''
        if desktop_entry:
            version = desktop_entry.get('X-AppImage-Version', '') or ''

        if (not version) and extracted_appimage and return_hash:
            version = extracted_appimage.md5[0:6]

        return version


def _user_applications_dir() -> str:
    from . import constants
    return constants.user_applications_dir()


def _strip_appimage_ext(name: str) -> str:
    import re
    return re.sub(r'\.appimage$', '', name, 1, re.IGNORECASE)

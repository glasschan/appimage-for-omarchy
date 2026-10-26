# provider_listing.py — listing, stale records, running-process
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
# bookkeeping and ELF/type probes (ticket 57).
#
# Split out of provider.py as a mixin: `AppImageProvider` composes it, so
# every method keeps its exact name and behaviour; the definitions moved
# verbatim. Methods that belong to other responsibilities (destination
# paths, version reads) are still resolved through `self` at runtime.

import filecmp
import logging
import os
import shlex
from types import SimpleNamespace

from . import constants, elf, launch, settings
from .desktop_entry import DesktopEntry
from .ini_config import Config
from .provider_types import AppImageListElement, InstalledStatus, InternalError
from .utils import extract_terminal_arguments, is_app_running


class ListingMixin:
    @staticmethod
    def _recorded_exec_arguments(record) -> str:
        """The Exec arguments recorded at install time (apps.ini's
        `default_exec_arguments`), or '' when the record carries none.

        The recorded value is a plain space-joined argument list, so it is
        read back with shlex.split — the desktop field codes (%f %F %u %U
        %i %c %k) stay VERBATIM; stripping them is the UI's job."""
        default_args = (record or {}).get('default_exec_arguments', '') or ''
        if not default_args.strip():
            return ''
        return ' '.join(shlex.split(default_args))

    def list_installed(self) -> list:
        default_folder_path = self._get_appimages_default_destination_path()
        manage_from_outside = settings.load_settings()[
            'manage_files_outside_default_folder']
        output = []

        # desktop_file_path -> recorded install config, scanned once. The
        # desktop entry is mutable (apps like VS Code forks rewrite their
        # own .desktop on first launch), so list here is desktop-driven but
        # can fall back to the paths recorded at install time in apps.ini.
        desktop_to_config = {}
        for _section_key, rec in Config.iter_app_records():
            dp = rec.get('desktop_file_path', '')
            if dp and dp not in desktop_to_config:
                desktop_to_config[dp] = rec

        # A record whose .desktop file is gone is invisible to the
        # desktop-driven scan below — and could never be removed either
        # (the CLI only resolves what this listing knows). Log it with the
        # orphaned AppImage path so the user can reclaim the file. Nothing
        # is deleted here: removal stays an explicit --remove.
        for desktop_path, rec in desktop_to_config.items():
            if os.path.exists(desktop_path):
                continue
            logging.warning(
                'stale integration record: the desktop file %s is gone; '
                'orphaned AppImage: %s (remove it with --remove %s)',
                desktop_path,
                rec.get('appimage_path', '') or rec.get('file_path', ''),
                os.path.basename(desktop_path))

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
                # The entry-point shape (ticket #34: `env ... python
                # main.py --exec-with-scale <image> [args]`) is parsed by
                # the launch module's reader, which recovers the image's
                # own arguments verbatim; anything else (a legacy
                # raw-image Exec written by an older plugin, or an
                # app-clobbered entry) falls back to the heuristic
                # GearLever parser.
                entry_point = launch.parse_entry_point_exec(entry.getExec())
                if entry_point is not None:
                    exec_arguments = ' '.join(entry_point['arguments'])
                    exec_env_variables = entry_point['env_variables']
                else:
                    exec_command_data = extract_terminal_arguments(
                        entry.getExec())
                    exec_arguments = ' '.join(exec_command_data['arguments'])
                    exec_env_variables = exec_command_data['env_vars']
                recovered = False

                if not os.path.isfile(exec_location):
                    # Exec/TryExec point at a path that is gone — typically a
                    # mount-time binary whose /tmp/.mount_* dir was removed
                    # after the app quit. Recover from the record made at
                    # install time if it still resolves.
                    recorded = desktop_to_config.get(desktop_path)
                    recorded_appimage = (recorded or {}).get(
                        'appimage_path', '')
                    if (recorded_appimage
                            and os.path.isfile(recorded_appimage)
                            and self.can_install_file(recorded_appimage)):
                        recovered = True
                        exec_location = recorded_appimage
                        exec_arguments = self._recorded_exec_arguments(
                            recorded)
                        # env vars from the clobbered entry are unreliable;
                        # the repaired entry below restores the standard ones
                        exec_env_variables = []
                    else:
                        logging.debug('%s skipped because %s does not exist',
                                      desktop_path, exec_location)
                        continue

                if not exec_arguments.strip():
                    # The current desktop entry carries no arguments: fall
                    # back to the ones recorded at install time (the entry
                    # may have been rewritten without them). The current
                    # entry always wins when it has arguments.
                    exec_arguments = self._recorded_exec_arguments(
                        desktop_to_config.get(desktop_path))

                exec_in_default_folder = os.path.isfile(os.path.join(
                    default_folder_path, os.path.basename(exec_location)))
                exec_in_folder = True if manage_from_outside \
                    else exec_in_default_folder
                app_version = self._get_app_version(
                    None, desktop_entry=entry)
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
                        env_variables=exec_env_variables,
                        size=file_size,
                    )
                    list_element.architecture = None
                    output.append(list_element)
                    if recovered:
                        self._repair_clobbered_desktop(desktop_path,
                                                       recorded)
                    self._repair_entry_point_dir(
                        desktop_path, desktop_to_config.get(desktop_path),
                        entry, exec_location, exec_arguments,
                        exec_env_variables)
                else:
                    logging.debug('%s skipped because %s is not a '
                                  'supported file type',
                                  desktop_path, exec_location)

            except Exception as e:
                logging.warning('%s: %s', desktop_path, e)

        return output

    def list_stale_records(self) -> list:
        """Integration records whose .desktop file is gone.

        list_installed() is desktop-driven, so such a record is invisible
        there — and could never be removed either, because the CLI only
        resolves targets the listing knows. These elements are the
        explicit handle for an orphan: nothing is deleted automatically,
        the user has to ask for the removal (--remove <desktop id>)."""
        stale = []
        for _section_key, rec in Config.iter_app_records():
            desktop_path = rec.get('desktop_file_path', '')
            if not desktop_path or os.path.exists(desktop_path):
                continue
            appimage_path = (rec.get('appimage_path', '')
                             or rec.get('file_path', ''))
            stale.append(AppImageListElement(
                name=rec.get('name', '') or os.path.basename(appimage_path),
                description='',
                provider=self.name,
                installed_status=InstalledStatus.INSTALLED,
                file_path=appimage_path,
                desktop_file_path=desktop_path,
                trusted=True,
                exec_arguments=self._recorded_exec_arguments(rec),
            ))
        return stale

    def _repair_clobbered_desktop(self, desktop_path: str, recorded: dict):
        """Point a clobbered .desktop's Exec/TryExec/Icon back at the
        recorded install paths, preserving every other field.

        Only called from list_installed() when the apps.ini fallback
        matched, and exactly once per desktop path per provider instance,
        so the panel/CLI never spin on the disk for an entry that keeps
        self-clobbering while it runs."""
        if desktop_path in self._repaired_desktop_paths:
            return
        try:
            entry = DesktopEntry(path=desktop_path)
        except Exception as e:
            logging.warning('repair: could not read %s: %s', desktop_path, e)
            return

        appimage = recorded.get('appimage_path', '')
        icon = recorded.get('icon_path') or 'applications-other'
        default_args = recorded.get('default_exec_arguments', '') or ''
        args = shlex.split(default_args) if default_args.strip() else []
        entry.TryExec = appimage
        # the scale-aware entry point (ticket #34), built by the ONE
        # launch builder with the CURRENT backend directory
        entry.Exec = shlex.join(launch.entry_point_argv(
            constants.backend_dir(), appimage, args))
        entry.Icon = icon
        entry.write_file(desktop_path)
        self._repaired_desktop_paths.add(desktop_path)
        logging.info('repaired clobbered desktop entry: %s', desktop_path)

    def _repair_entry_point_dir(self, desktop_path, recorded, entry,
                                appimage_path, exec_arguments,
                                env_variables):
        """Re-point an entry-point Exec at the CURRENT plugin backend
        directory (ticket #34).

        The directory recorded at integrate time (`launch_backend_dir` in
        apps.ini) is compared against constants.backend_dir() on every
        listing; a plugin update that moved the directory (or an
        uninstall-leftover recorded path) re-points the entry instead of
        leaving a dead Exec behind. Entries recorded before ticket #34
        carry no key and are left alone — they adopt the entry point on
        their next update or re-integrate, never from a passive listing.
        The re-point is persisted in the record, so it happens once; the
        per-instance set only guards a record write that keeps failing."""
        recorded_dir = (recorded or {}).get('launch_backend_dir') or ''
        if not recorded_dir:
            return
        current_dir = constants.backend_dir()
        if recorded_dir == current_dir and os.path.isfile(
                launch.entry_point_script(current_dir)):
            return
        if desktop_path in self._repaired_desktop_paths:
            return
        try:
            args = (shlex.split(exec_arguments)
                    if exec_arguments.strip() else [])
        except ValueError as e:
            logging.warning('entry-point repair: could not parse the '
                            'arguments of %s: %s', desktop_path, e)
            return
        try:
            entry.Exec = launch.entry_point_exec_text(
                current_dir, appimage_path, args,
                env_variables=list(env_variables or []))
            entry.write_file(desktop_path)
        except Exception as e:
            logging.warning('entry-point repair: could not rewrite %s: %s',
                            desktop_path, e)
            return
        self._repaired_desktop_paths.add(desktop_path)
        if recorded is not None:
            updated = dict(recorded)
            updated['launch_backend_dir'] = current_dir
            Config.set_app_config(
                SimpleNamespace(file_path=appimage_path,
                                name=recorded.get('name', '') or ''),
                updated)
        logging.info('re-pointed desktop entry at the current plugin '
                     'directory: %s', desktop_path)

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

    def is_app_running(self, el: AppImageListElement, probe=None) -> bool:
        return is_app_running(el.file_path, probe=probe)

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


def _strip_appimage_ext(name: str) -> str:
    import re
    return re.sub(r'\.appimage$', '', name, 1, re.IGNORECASE)

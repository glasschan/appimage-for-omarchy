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
from .utils import (atomic_write, extract_terminal_arguments, get_file_hash,
                    is_app_running, remove_special_chars, run_command)

# Split modules (ticket 57): these names moved verbatim and are re-exported
# here so the existing import surface (`omarchy_appimage.provider`) is
# unchanged for cli.py and the test suite.


class AppImageProvider():
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

    # ------------------------------------------------------------ listing

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
                exec_command_data = extract_terminal_arguments(entry.getExec())
                exec_arguments = ' '.join(exec_command_data['arguments'])
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
                        exec_command_data['env_vars'] = []
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
                        env_variables=exec_command_data['env_vars'],
                        size=file_size,
                    )
                    list_element.architecture = None
                    output.append(list_element)
                    if recovered:
                        self._repair_clobbered_desktop(desktop_path,
                                                       recorded)
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
        entry.Exec = shlex.join(
            ['env', 'DESKTOPINTEGRATION=1', appimage, *args])
        entry.Icon = icon
        entry.write_file(desktop_path)
        self._repaired_desktop_paths.add(desktop_path)
        logging.info('repaired clobbered desktop entry: %s', desktop_path)

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

    def plan_destination(self, source_basename: str, desktop_name,
                         version: str) -> dict:
        """The names an install would use: ONE naming rule, shared by
        plan_install and the preview document (issue #45).

        Reading the folder's existing names is a read; nothing here
        creates the folder or touches a file. The conflict suffix comes
        from GearLever's helper (round-4 review: desktop Name and
        X-AppImage-Version are embedded, attacker-controlled fields)."""
        folder = self._get_appimages_default_destination_path()
        existing_names = (os.listdir(folder)
                          if os.path.isdir(folder) else [])
        filename, desktop_prefix = _resolve_install_names(
            source_basename=source_basename,
            desktop_name=desktop_name,
            version=version or '',
            existing_names=existing_names)
        return {'folder': folder, 'filename': filename,
                'desktop_prefix': desktop_prefix,
                'path': os.path.join(folder, filename)}

    def plan_install(self, el: AppImageListElement) -> InstallPlan:
        """Read and validate everything an install is about to write.

        Extraction into a temporary folder is a READ (the extractor never
        executes the image), so nothing here touches the managed folder,
        `.icons`, the applications directory or apps.ini. Every refusal is
        an InternalError with a human message: an image with no root
        desktop entry, a desktop file that cannot be read, or an Exec
        command that does not parse is rejected BEFORE the first write —
        never an AttributeError halfway through, and never a half-applied
        install.

        The returned record is complete: destination names, the exact
        desktop-entry bytes, the sanitised icon bytes, the apps.ini keys,
        and which source file (if any) to move away after the replace.
        `commit_install` performs every write."""
        extracted_appimage = self._load_appimage_metadata(el)

        # A root-level .desktop is mandatory: its Exec arguments and its
        # Icon drive the installed entry. Checking it BEFORE any write is
        # the whole point of the plan (ticket 40; the old guard sat after
        # the binary replace, where the missing entry had already
        # crashed on .getExec()).
        if extracted_appimage.desktop_entry is None \
                or not extracted_appimage.desktop_file:
            raise InternalError(
                'Could not find a .desktop file inside this AppImage')
        if not os.path.isfile(extracted_appimage.desktop_file):
            raise InternalError(
                'Could not read the .desktop file inside this AppImage')
        try:
            jd_desktop_entry = DesktopEntry(
                path=extracted_appimage.desktop_file)
        except OSError as e:
            raise InternalError(
                'Could not read the .desktop file inside this AppImage: '
                f'{e}') from e

        source_appimage = extracted_appimage.appimage_file
        if not source_appimage or not os.path.isfile(source_appimage):
            raise InternalError(
                f'Could not read the AppImage file: {source_appimage}')

        version = self._get_app_version(extracted_appimage)
        appimages_destination_path = \
            self._get_appimages_default_destination_path()

        # how the appimage will be called
        if el.update_logic == AppImageUpdateLogic.REPLACE \
                and el.updating_from is not None:
            if not el.updating_from.file_path \
                    or not el.updating_from.desktop_file_path:
                raise InternalError(
                    'Could not resolve the app this install replaces')
            appimage_filename = os.path.basename(el.updating_from.file_path)
            desktop_file_path = os.path.basename(
                el.updating_from.desktop_file_path)
            prefixed_filename = os.path.splitext(desktop_file_path)[0]
        else:
            # The naming rule is plan_destination's, so the preview cannot
            # disagree with the install (issue #45).
            planned_names = self.plan_destination(
                os.path.basename(el.file_path),
                extracted_appimage.desktop_entry.getName(), version)
            appimage_filename = planned_names['filename']
            prefixed_filename = planned_names['desktop_prefix']

        dest_appimage_file = os.path.join(appimages_destination_path,
                                          appimage_filename)

        # The icon bytes are read NOW, so what commit writes is exactly
        # what this plan validated (the extractor already sanitised them;
        # see icon.sanitize_icon).
        dest_appimage_icon_file = None
        icon_bytes = None
        icon_file = extracted_appimage.icon_file
        if icon_file and os.path.exists(icon_file):
            try:
                with open(icon_file, 'rb') as f:
                    icon_bytes = f.read()
            except OSError as e:
                raise InternalError(
                    f'Could not read the icon inside this AppImage: '
                    f'{e}') from e

            icon_file_ext = os.path.splitext(icon_file)[1]
            dest_appimage_icon_file = os.path.join(
                appimages_destination_path, '.icons',
                prefixed_filename + icon_file_ext)

        # Move .desktop file to its default location
        dest_desktop_file_path = os.path.join(
            self.user_desktop_files_path, prefixed_filename + '.desktop')
        dest_desktop_file_path = dest_desktop_file_path.replace(' ', '_')

        # Get default exec arguments shipped by this version of the appimage
        try:
            term_arguments = extract_terminal_arguments(
                extracted_appimage.desktop_entry.getExec())
        except ValueError as e:
            raise InternalError(
                'Could not parse the Exec command in the .desktop file '
                f'inside this AppImage: {e}') from e
        exec_arguments = term_arguments['arguments']
        new_default_exec_arguments = ' '.join(exec_arguments)

        # Only preserve the previous exec arguments if the user actually
        # customized them (upstream comment preserved).
        if el.updating_from and el.updating_from.exec_arguments:
            previous_config = Config.get_app_config(el.updating_from)
            previous_default_exec_arguments = previous_config.get(
                'default_exec_arguments', '')

            if el.updating_from.exec_arguments != previous_default_exec_arguments:
                try:
                    exec_arguments = shlex.split(
                        el.updating_from.exec_arguments)
                except ValueError as e:
                    raise InternalError(
                        'Could not parse the recorded Exec arguments for '
                        f'this app: {e}') from e

        jd_desktop_entry.TryExec = dest_appimage_file
        jd_desktop_entry.Exec = shlex.join([dest_appimage_file,
                                            *exec_arguments])

        if el.update_logic is AppImageUpdateLogic.KEEP:
            # The display suffix carries the REAL version only — an image
            # without an X-AppImage-Version keeps its plain name (no md5
            # hash suffix; see _get_app_version).
            final_app_name = extracted_appimage.desktop_entry.getName()
            if version:
                final_app_name += f' ({version})'
            jd_desktop_entry.Name.default_text = final_app_name

        desktop_icon = 'applications-other'
        if dest_appimage_icon_file:
            desktop_icon = dest_appimage_icon_file

        jd_desktop_entry.Icon = desktop_icon

        try:
            for _action_id, action in jd_desktop_entry.Actions.items():
                a_exec_args = extract_terminal_arguments(action.Exec)
                action.Icon = desktop_icon
                action.Exec = shlex.join([dest_appimage_file,
                                          *a_exec_args['arguments']])
        except ValueError as e:
            raise InternalError(
                'Could not parse a desktop action in the .desktop file '
                f'inside this AppImage: {e}') from e

        if version:
            jd_desktop_entry.set_custom('X-AppImage-Version', version)
        jd_desktop_entry.set_custom(
            'X-AppImage-Name', extracted_appimage.desktop_entry.getName())

        # install_file used to rewrite the entry once more through
        # update_desktop_file() so it runs with `env VAR=...`. Compute
        # that FINAL text here instead of writing the file twice, keeping
        # the exact same precedence: the previous app's env vars are
        # adopted for an update, and DESKTOPINTEGRATION=1 is appended when
        # no variable carries it.
        env_variables = list(el.env_variables)
        rewrite_exec = False
        if el.updating_from and el.updating_from.env_variables:
            env_variables = list(el.updating_from.env_variables)
            rewrite_exec = True
        has_desktop_integration = any(
            v.startswith('DESKTOPINTEGRATION=') for v in env_variables)
        if not has_desktop_integration:
            env_variables.append('DESKTOPINTEGRATION=1')
            rewrite_exec = True

        exec_arguments_text = ' '.join(exec_arguments)
        if rewrite_exec:
            env_vars_prefix = (f'env {" ".join(env_variables)} '
                               if env_variables else '')
            jd_desktop_entry.Exec = ''.join([
                env_vars_prefix,
                shlex.quote(dest_appimage_file),
                f' {exec_arguments_text}' if exec_arguments_text else '',
            ])

        desktop_content = jd_desktop_entry.get_text().encode('utf-8')

        # The record is keyed by the INSTALLED path (el.file_path becomes
        # `dest_appimage_file`); an existing record for the same path (the
        # REPLACE case) is merged exactly like Config.get_app_config(el)
        # did after the binary was in place.
        app_config = dict(Config.get_app_config(
            SimpleNamespace(file_path=dest_appimage_file)))
        app_config['default_exec_arguments'] = new_default_exec_arguments
        # provenance for the containment-bound uninstall: the desktop
        # entry is mutable, so uninstall never trusts its Icon/TryExec
        # values alone — it cross-checks what was recorded here
        app_config['appimage_path'] = dest_appimage_file
        app_config['desktop_file_path'] = dest_desktop_file_path
        app_config['icon_path'] = dest_appimage_icon_file or ''

        remove_original = (
            settings.load_settings()['move_appimage_on_integration']
            and os.path.dirname(source_appimage)
            != appimages_destination_path)

        return InstallPlan(
            source_appimage=source_appimage,
            dest_appimage_file=dest_appimage_file,
            desktop_id=prefixed_filename,
            dest_desktop_file_path=dest_desktop_file_path,
            dest_icon_file=dest_appimage_icon_file,
            icon_bytes=icon_bytes,
            desktop_content=desktop_content,
            exec_arguments=exec_arguments_text,
            default_exec_arguments=new_default_exec_arguments,
            env_variables=env_variables,
            app_config=app_config,
            version=version,
            remove_original=remove_original,
        )

    def commit_install(self, el: AppImageListElement, plan: InstallPlan,
                       before_replace=None):
        """Perform every write of an install, in the ported flow's order.

        Binary replace first (that is where `before_replace` fires — the
        force-quit grant of ticket 26), then its mode, the icon and its
        stale siblings, the desktop entry, apps.ini, the original
        download's removal and the desktop-database refresh."""
        appimages_destination_path = os.path.dirname(
            plan.dest_appimage_file)
        os.makedirs(appimages_destination_path, exist_ok=True)

        _atomic_install(plan.source_appimage, plan.dest_appimage_file,
                        0o755, before_replace=before_replace)
        logging.debug('file copied to %s', appimages_destination_path)

        el.file_path = plan.dest_appimage_file
        el.exec_arguments = plan.exec_arguments
        el.env_variables = list(plan.env_variables)
        el.set_trusted()

        # copy the icon file (the bytes the plan validated)
        if plan.icon_bytes is not None and plan.dest_icon_file:
            os.makedirs(os.path.dirname(plan.dest_icon_file), exist_ok=True)
            _atomic_write_bytes(plan.icon_bytes, plan.dest_icon_file, 0o644)

        # A previous icon of the same stem must not survive: the panel
        # renders .icons/<stem>.png|svg by file name (not through the
        # desktop entry's Icon=), so a stale file would keep being decoded
        # by the shared shell even after an update whose new icon is
        # refused -- and a stale .png would win the panel's png-first probe
        # over a freshly installed .svg.
        icons_folder = os.path.join(appimages_destination_path, '.icons')
        keep_ext = (os.path.splitext(plan.dest_icon_file)[1]
                    if plan.dest_icon_file else None)
        for stale_ext in ('.png', '.svg', '.xpm'):
            if stale_ext == keep_ext:
                continue
            try:
                os.remove(os.path.join(icons_folder,
                                       plan.desktop_id + stale_ext))
            except OSError:
                pass

        # Write the new .desktop file (atomically) at its default
        # location; the plan rendered the exact final content once.
        os.makedirs(self.user_desktop_files_path, exist_ok=True)
        atomic_write(plan.dest_desktop_file_path, plan.desktop_content)

        el.desktop_entry = DesktopEntry(path=plan.dest_desktop_file_path)
        el.desktop_file_path = plan.dest_desktop_file_path
        el.installed_status = InstalledStatus.INSTALLED

        Config.set_app_config(el, plan.app_config)

        if plan.remove_original:
            logging.info('Deleting original appimage file from: %s',
                         plan.source_appimage)
            os.unlink(plan.source_appimage)

        try:
            run_command(['update-desktop-database',
                         self.user_desktop_files_path, '-q'],
                        check=False, timeout=30)
        except Exception as e:
            logging.debug('update-desktop-database failed: %s', e)

        el.updating_from = None

    def install_file(self, el: AppImageListElement, before_replace=None):
        """Ported from AppImageProvider.install_file (naming, conflict
        resolution and desktop entry rewriting follow upstream).

        Since ticket 40 this is plan + commit: `plan_install` reads and
        validates the whole install without writing anything, then
        `commit_install` performs every write in the original order.

        `before_replace` is forwarded to the AppImage's own
        _atomic_install only (never to the icon write): it is the last
        moment before the new file becomes visible at its final name, so
        an update that was granted permission to close the running app
        can close whatever was (re)started during the download. Default
        None keeps every existing caller byte-identical."""
        logging.info('Installing appimage: %s', el.file_path)
        el.installed_status = InstalledStatus.INSTALLING
        plan = self.plan_install(el)
        self.commit_install(el, plan, before_replace=before_replace)

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


def _strip_appimage_ext(name: str) -> str:
    import re
    return re.sub(r'\.appimage$', '', name, 1, re.IGNORECASE)

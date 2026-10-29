# provider_install.py — install planning + commit (ticket 57).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Split out of provider.py as a mixin: `AppImageProvider` composes it, so
# `plan_install` / `commit_install` keep their exact signatures (cli.py and
# the preview path call them). The definitions moved verbatim; the plan/
# commit contract is unchanged — plan reads and validates without writing,
# commit performs every write, and a refusal writes nothing.

import logging
import os
import shlex
import sys
from types import SimpleNamespace

from .install_writes import _atomic_install, _atomic_write_bytes
from .desktop_entry import DesktopEntry
from . import constants, launch, settings
from .ini_config import Config
from .install_writes import _resolve_install_names
from .provider_types import (AppImageListElement, AppImageUpdateLogic,
                             InstallPlan, InstalledStatus, InternalError)
from .utils import atomic_write, extract_terminal_arguments, run_command


def _recorded_app_name(entry, version, dest_appimage_file: str) -> str:
    """The app name apps.ini records for an install/update.

    The element's own name is not usable on the update path (the element IS
    the downloaded file, so its name is the temp file's basename), so the
    name comes from the desktop entry the commit just wrote, minus the
    `` (<version>)`` suffix the KEEP update logic appends."""
    name = ''
    if entry is not None:
        try:
            name = (entry.getName() or '').strip()
        except Exception:
            name = ''
    suffix = f' ({version})' if version else ''
    if suffix and name.endswith(suffix):
        name = name[:-len(suffix)].strip()
    if name == '':
        name = os.path.splitext(os.path.basename(dest_appimage_file or ''))[0]
    return name


class InstallMixin:
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

        # The launch path is the scale-aware entry point (ticket #34):
        # Exec invokes `<python> <backend>/main.py --exec-with-scale
        # <image> [args]` through the ONE launch builder, so a launcher
        # click gets the focused monitor's scale. The plugin backend
        # directory is recorded in apps.ini and rebuilt from the CURRENT
        # directory on every install/repair, so a plugin update that
        # moves the directory re-points the entry instead of breaking it.
        # TryExec stays the raw image: the listing uses it to locate the
        # installed file.
        launch_backend_dir = constants.backend_dir()
        env_variables, _ = launch.merge_env_variables(
            el.env_variables,
            el.updating_from.env_variables if el.updating_from else None)

        jd_desktop_entry.TryExec = dest_appimage_file
        jd_desktop_entry.Exec = launch.entry_point_exec_text(
            launch_backend_dir, dest_appimage_file, exec_arguments,
            env_variables=env_variables,
            python_executable=sys.executable)

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
                # Desktop actions launch the app too: the same
                # scale-aware entry point, with no env prefix (actions
                # never carried one).
                action.Exec = launch.entry_point_exec_text(
                    launch_backend_dir, dest_appimage_file,
                    a_exec_args['arguments'], env_variables=[],
                    python_executable=sys.executable)
        except ValueError as e:
            raise InternalError(
                'Could not parse a desktop action in the .desktop file '
                f'inside this AppImage: {e}') from e

        if version:
            jd_desktop_entry.set_custom('X-AppImage-Version', version)
        jd_desktop_entry.set_custom(
            'X-AppImage-Name', extracted_appimage.desktop_entry.getName())

        # install_file used to rewrite the entry once more through
        # update_desktop_file() so it runs with `env VAR=...`; the final
        # Exec text is computed above through the ONE launch-command
        # builder (tickets #32/#34). merge_env_variables still owns the
        # DESKTOPINTEGRATION precedence for the recorded env list
        # (tests/test_launch.py pins its behaviour).

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
        # where the entry-point Exec points (ticket #34): recorded once
        # here, re-checked against constants.backend_dir() on every
        # listing so a plugin update that moves the directory repairs
        # the entry instead of breaking it
        app_config['launch_backend_dir'] = launch_backend_dir

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
            exec_arguments=' '.join(exec_arguments),
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

        # Record the name the desktop entry now carries — NOT el.name,
        # which on the update path is the downloaded file's basename
        # ("update"). install_file appends " (<version>)" under the KEEP
        # logic, so exactly that suffix is dropped again; anything else stays
        # (an app really named "Foo (Fork)" survives). The listing falls back
        # to this name when an app's desktop entry is unreadable, so
        # "update" would otherwise surface as an app called "update".
        plan.app_config['name'] = _recorded_app_name(
            el.desktop_entry, plan.version, plan.dest_appimage_file)
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

    def update_desktop_file(self, el: AppImageListElement):
        if not el.desktop_file_path:
            raise Exception('desktop_file_path not specified')

        jd_desktop_file = DesktopEntry(path=el.desktop_file_path)

        tryexec_command = jd_desktop_file.TryExec
        exec_arguments = el.exec_arguments or ''
        try:
            exec_argument_tokens = shlex.split(exec_arguments)
        except ValueError as e:
            raise InternalError(
                f'Could not parse the recorded Exec arguments for this '
                f'app: {e}') from e

        # the scale-aware entry point (ticket #34), built by the ONE
        # launch builder with the CURRENT backend directory
        jd_desktop_file.Exec = launch.entry_point_exec_text(
            constants.backend_dir(), tryexec_command,
            exec_argument_tokens, env_variables=list(el.env_variables or []),
            python_executable=sys.executable)
        jd_desktop_file.write_file(el.desktop_file_path)

        el.desktop_entry = DesktopEntry(path=el.desktop_file_path)

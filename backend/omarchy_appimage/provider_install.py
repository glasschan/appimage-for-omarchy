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
from types import SimpleNamespace

from .install_writes import _atomic_install, _atomic_write_bytes
from .desktop_entry import DesktopEntry
from . import launch, settings
from .ini_config import Config
from .install_writes import _resolve_install_names
from .provider_types import (AppImageListElement, AppImageUpdateLogic,
                             InstallPlan, InstalledStatus, InternalError)
from .utils import atomic_write, extract_terminal_arguments, run_command


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
        # that FINAL text here instead of writing the file twice —
        # through the ONE launch-command builder (ticket #32), which
        # owns both the DESKTOPINTEGRATION precedence and the Exec
        # serialization; tests/test_launch.py pins its bytes.
        env_variables, rewrite_exec = launch.merge_env_variables(
            el.env_variables,
            el.updating_from.env_variables if el.updating_from else None)

        if rewrite_exec:
            jd_desktop_entry.Exec = launch.launch_exec_text(
                dest_appimage_file, exec_arguments, env_variables)

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

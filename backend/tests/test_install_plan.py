# test_install_plan.py — ticket 40: an install is planned before it is
# committed. Test suite written for this plugin. Released under the GNU
# GPL-3.0 — see the repo's LICENSE for terms.
#
# The old install_file wrote as it went: metadata load, binary replace,
# icon, then `extract_terminal_arguments(desktop_entry.getExec())` — which
# crashed with AttributeError for an image carrying no root-level
# .desktop. The guard existed only after that crash point. These tests
# pin the new shape: plan_install() reads and validates everything and
# writes NOTHING; commit_install() performs every write in the ported
# order, with before_replace still firing only at the binary replace.
#

import os
import stat
import unittest
from pathlib import Path

from helpers import FakeXDGTestCase

from omarchy_appimage import extractor
from omarchy_appimage.desktop_entry import DesktopEntry
from omarchy_appimage.ini_config import Config
from omarchy_appimage.provider import (AppImageListElement, AppImageProvider,
                                       AppImageUpdateLogic, InstalledStatus,
                                       InternalError)

PNG = b'\x89PNG\r\n\x1a\n' + b'icon-payload'

DESKTOP = '''[Desktop Entry]
Name=Demo App
Exec=demo --flag %F
TryExec=demo
Icon=demo
Type=Application
Terminal=false
X-AppImage-Version=1.2.3
'''


class InstallPlanTestCase(FakeXDGTestCase):
    """Shared sandbox: one source AppImage, one embedded-style .desktop
    and one icon, all inside the test sandbox."""

    def setUp(self):
        super().setUp()
        self.provider = AppImageProvider()
        self.work = os.path.join(self.sandbox, 'work')
        os.makedirs(self.work)

        self.source_bytes = (b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
                             + b'payload')
        self.source = os.path.join(self.work, 'demo.appimage')
        Path(self.source).write_bytes(self.source_bytes)
        os.chmod(self.source, 0o644)

        self.desktop_src = os.path.join(self.work, 'app.desktop')
        Path(self.desktop_src).write_text(DESKTOP)

        self.icon_src = os.path.join(self.work, 'icon.png')
        Path(self.icon_src).write_bytes(PNG)

    def element(self, desktop_content=DESKTOP, desktop_entry=True,
                desktop_file=True, icon=True) -> AppImageListElement:
        """An element whose extraction result is already available (the
        sandbox has no real squashfs image; only the install path is
        under test)."""
        if desktop_content != DESKTOP:
            Path(self.desktop_src).write_text(desktop_content)
        entry = DesktopEntry(content=desktop_content) if desktop_entry \
            else None
        el = AppImageListElement(
            name='Demo App', description='', provider='AppImage',
            installed_status=InstalledStatus.NOT_INSTALLED,
            file_path=self.source)
        el.update_logic = AppImageUpdateLogic.KEEP
        el.extracted = extractor.ExtractedAppImage(
            appimage_file=self.source,
            desktop_entry=entry,
            desktop_file=self.desktop_src if desktop_file else None,
            icon_file=self.icon_src if icon else None)
        return el

    def assert_nothing_written(self):
        self.assertFalse(os.path.exists(self.managed_dir))
        self.assertFalse(os.path.exists(
            os.path.join(self.managed_dir, '.icons')))
        self.assertFalse(os.path.exists(self.applications_dir))
        self.assertFalse(os.path.exists(Config.path()))


class PlanRefusalTests(InstallPlanTestCase):
    """A refusal is typed, human, and leaves every install location
    untouched — the old flow had already replaced the binary by then."""

    def test_missing_root_desktop_entry_is_refused_before_any_write(self):
        el = self.element(desktop_entry=False, desktop_file=False)
        with self.assertRaises(InternalError) as raised:
            self.provider.install_file(el)
        self.assertIn('.desktop', raised.exception.message)
        self.assertNotIsInstance(raised.exception, AttributeError)
        self.assert_nothing_written()

    def test_unreadable_desktop_file_is_refused_before_any_write(self):
        el = self.element()
        el.extracted.desktop_file = os.path.join(self.work, 'gone.desktop')
        with self.assertRaises(InternalError) as raised:
            self.provider.install_file(el)
        self.assertIn('.desktop', raised.exception.message)
        self.assert_nothing_written()

    def test_unparseable_exec_is_refused_before_any_write(self):
        el = self.element(desktop_content=(
            '[Desktop Entry]\n'
            'Name=Demo App\n'
            'Exec=demo "unclosed\n'
            'Type=Application\n'))
        with self.assertRaises(InternalError) as raised:
            self.provider.install_file(el)
        self.assertIn('Exec', raised.exception.message)
        self.assert_nothing_written()

    def test_unreadable_appimage_source_is_refused(self):
        el = self.element()
        el.extracted.appimage_file = os.path.join(self.work, 'gone.appimage')
        with self.assertRaises(InternalError):
            self.provider.install_file(el)
        self.assert_nothing_written()


class PlanOnlyTests(InstallPlanTestCase):
    def test_plan_returns_a_complete_record_and_writes_nothing(self):
        el = self.element()

        plan = self.provider.plan_install(el)

        self.assert_nothing_written()
        # nothing was mutated on disk or in the element's installed state
        self.assertEqual(el.file_path, self.source)
        self.assertEqual(el.installed_status, InstalledStatus.NOT_INSTALLED)

        apps = self.managed_dir
        self.assertEqual(plan.dest_appimage_file,
                         os.path.join(apps, 'demo_app.appimage'))
        self.assertEqual(plan.desktop_id, 'demo_app')
        self.assertEqual(plan.dest_desktop_file_path,
                         os.path.join(self.applications_dir,
                                      'demo_app.desktop'))
        self.assertEqual(plan.dest_icon_file,
                         os.path.join(apps, '.icons', 'demo_app.png'))
        self.assertEqual(plan.icon_bytes, PNG)
        self.assertEqual(plan.exec_arguments, '--flag %F')
        self.assertEqual(plan.default_exec_arguments, '--flag %F')
        self.assertEqual(plan.env_variables, ['DESKTOPINTEGRATION=1'])
        self.assertEqual(plan.version, '1.2.3')
        self.assertTrue(plan.remove_original)
        self.assertEqual(plan.app_config['appimage_path'],
                         plan.dest_appimage_file)
        self.assertEqual(plan.app_config['desktop_file_path'],
                         plan.dest_desktop_file_path)
        self.assertEqual(plan.app_config['icon_path'], plan.dest_icon_file)
        self.assertEqual(plan.app_config['default_exec_arguments'],
                         '--flag %F')
        self.assertIn(b'Name=Demo App (1.2.3)\n', plan.desktop_content)


class SuccessfulInstallTests(InstallPlanTestCase):
    def test_bytes_content_and_config_are_exactly_what_install_wrote(self):
        el = self.element()

        self.provider.install_file(el)

        appimage = os.path.join(self.managed_dir, 'demo_app.appimage')
        with open(appimage, 'rb') as f:
            self.assertEqual(f.read(), self.source_bytes)
        self.assertEqual(stat.S_IMODE(os.stat(appimage).st_mode), 0o755)

        icon = os.path.join(self.managed_dir, '.icons', 'demo_app.png')
        with open(icon, 'rb') as f:
            self.assertEqual(f.read(), PNG)
        self.assertEqual(stat.S_IMODE(os.stat(icon).st_mode), 0o644)

        desktop = os.path.join(self.applications_dir, 'demo_app.desktop')
        with open(desktop) as f:
            self.assertEqual(f.read(), (
                '[Desktop Entry]\n'
                'Name=Demo App (1.2.3)\n'
                f'Exec=env DESKTOPINTEGRATION=1 {appimage} --flag %F\n'
                f'TryExec={appimage}\n'
                f'Icon={icon}\n'
                'Type=Application\n'
                'Terminal=false\n'
                'X-AppImage-Version=1.2.3\n'
                'X-AppImage-Name=Demo App\n'))

        # the source was moved away (GearLever move-on-integration)
        self.assertFalse(os.path.exists(self.source))

        config = Config.get_app_config(el)
        self.assertEqual(config['appimage_path'], appimage)
        self.assertEqual(config['desktop_file_path'], desktop)
        self.assertEqual(config['icon_path'], icon)
        self.assertEqual(config['default_exec_arguments'], '--flag %F')

        self.assertEqual(el.exec_arguments, '--flag %F')
        self.assertEqual(el.env_variables, ['DESKTOPINTEGRATION=1'])
        self.assertEqual(el.desktop_file_path, desktop)
        self.assertEqual(el.installed_status, InstalledStatus.INSTALLED)

    def test_stale_icon_of_the_same_stem_is_removed(self):
        icons = os.path.join(self.managed_dir, '.icons')
        os.makedirs(icons)
        stale = os.path.join(icons, 'demo_app.svg')
        Path(stale).write_bytes(b'<svg/>')

        self.provider.install_file(self.element())

        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(
            os.path.join(icons, 'demo_app.png')))


class BeforeReplaceOrderingTests(InstallPlanTestCase):
    def test_callback_fires_once_with_the_complete_file_not_yet_visible(self):
        el = self.element()
        dest = os.path.join(self.managed_dir, 'demo_app.appimage')
        seen = []

        def before_replace():
            temps = [name for name in os.listdir(self.managed_dir)
                     if name.startswith('.gearlever-tmp-')]
            # the complete payload is staged next to the destination…
            self.assertEqual(len(temps), 1)
            with open(os.path.join(self.managed_dir, temps[0]), 'rb') as f:
                self.assertEqual(f.read(), Path(self.source).read_bytes())
            # …but the final name is not visible yet
            seen.append(os.path.exists(dest))
            self.assertFalse(os.path.exists(dest))

        self.provider.install_file(el, before_replace=before_replace)

        self.assertEqual(seen, [False])
        self.assertTrue(os.path.isfile(dest))

    def test_exception_from_callback_aborts_before_the_binary_appears(self):
        el = self.element()
        dest = os.path.join(self.managed_dir, 'demo_app.appimage')

        def before_replace():
            raise RuntimeError('force-quit declined')

        with self.assertRaises(RuntimeError):
            self.provider.install_file(el, before_replace=before_replace)

        self.assertFalse(os.path.exists(dest))
        # nothing beyond the temporary file (removed) is written
        self.assertEqual(
            [n for n in os.listdir(self.managed_dir)
             if n.startswith('.gearlever-tmp-')], [])
        self.assertFalse(os.path.exists(
            os.path.join(self.applications_dir, 'demo_app.desktop')))
        self.assertFalse(os.path.exists(Config.path()))


if __name__ == '__main__':
    unittest.main()

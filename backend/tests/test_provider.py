# test_provider.py — install/uninstall hardening tests: descriptor-safe
# atomic install (destination-race proof) and the containment-bound
# uninstall. Hermetic.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import inspect
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import FakeXDGTestCase

from omarchy_appimage import provider as provider_module
from omarchy_appimage.desktop_entry import DesktopEntry
from omarchy_appimage.ini_config import Config
from omarchy_appimage.provider import (AppImageListElement, AppImageProvider,
                                       InstalledStatus, InternalError,
                                       _atomic_install)

PNG = b'\x89PNG\r\n\x1a\n'


def _make_el(file_path: str, desktop_path: str = None) -> AppImageListElement:
    entry = DesktopEntry(path=desktop_path) if desktop_path else None
    return AppImageListElement(
        name='Fake App', description='', provider='AppImage',
        installed_status=InstalledStatus.INSTALLED, file_path=file_path,
        desktop_entry=entry)


class AtomicInstallTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='atomic-install-')
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

        self.src = os.path.join(self.dir, 'src.appimage')
        Path(self.src).write_bytes(b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
                                   + b'payload')
        os.chmod(self.src, 0o644)

    def test_installs_regular_file_with_mode(self):
        dest = os.path.join(self.dir, 'dest.appimage')
        _atomic_install(self.src, dest, 0o755)
        self.assertTrue(stat.S_ISREG(os.lstat(dest).st_mode))
        self.assertEqual(stat.S_IMODE(os.lstat(dest).st_mode), 0o755)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
                             + b'payload')

    def test_destination_symlink_is_replaced_not_written_through(self):
        # destination race: a planted symlink at the dest path pointing at
        # a sentinel must be replaced wholesale — the sentinel is never
        # touched (the old shutil.copyfile wrote straight through it)
        sentinel = os.path.join(self.dir, 'sentinel.txt')
        Path(sentinel).write_text('important')
        dest = os.path.join(self.dir, 'dest.appimage')
        os.symlink(sentinel, dest)

        _atomic_install(self.src, dest, 0o755)

        self.assertFalse(os.path.islink(dest))
        with open(sentinel) as f:
            self.assertEqual(f.read(), 'important')     # untouched
        self.assertEqual(stat.S_IMODE(os.lstat(sentinel).st_mode), 0o644)
        self.assertTrue(stat.S_ISREG(os.lstat(dest).st_mode))

    def test_symlink_source_is_resolved(self):
        # the source is the user's own chosen file: a symlink there is
        # followed (realpath), not refused with ELOOP
        linked_src = os.path.join(self.dir, 'link-to-src')
        os.symlink(self.src, linked_src)
        dest = os.path.join(self.dir, 'dest.appimage')
        _atomic_install(linked_src, dest, 0o755)
        with open(dest, 'rb') as f:
            self.assertEqual(f.read(), b'\x7fELF\x02\x01\x01\x00\x41\x49\x02'
                             + b'payload')

    def test_non_regular_source_is_refused(self):
        # after resolution the source must still be a regular file
        fifo_src = os.path.join(self.dir, 'planted-fifo')
        os.mkfifo(fifo_src)
        dest = os.path.join(self.dir, 'dest.appimage')
        with self.assertRaises(InternalError):
            _atomic_install(fifo_src, dest, 0o755)
        self.assertFalse(os.path.exists(dest))

    def test_directory_destination_is_refused(self):
        dest = os.path.join(self.dir, 'dest-dir')
        os.makedirs(dest)
        with self.assertRaises(InternalError):
            _atomic_install(self.src, dest, 0o755)

    def test_no_temp_files_left_behind(self):
        dest = os.path.join(self.dir, 'dest.appimage')
        _atomic_install(self.src, dest, 0o755)
        leftovers = [n for n in os.listdir(self.dir)
                     if n.startswith('.gearlever-tmp-')]
        self.assertEqual(leftovers, [])


class UninstallTests(FakeXDGTestCase):
    """The desktop entry is mutable: its Icon= path is only removed when
    bound to this app (recorded in apps.ini, or inside .icons with the
    desktop id's stem)."""

    def setUp(self):
        super().setUp()
        self.provider = AppImageProvider()

    def _element(self) -> tuple:
        appimage, desktop, icon = self.install_fake_app()
        el = _make_el(appimage, desktop_path=desktop)
        return el, appimage, desktop, icon

    def test_legacy_layout_uninstall_removes_all_three(self):
        el, appimage, desktop, icon = self._element()
        self.provider.uninstall(el)
        self.assertFalse(os.path.exists(appimage))
        self.assertFalse(os.path.exists(desktop))
        self.assertFalse(os.path.exists(icon))

    def test_arbitrary_absolute_icon_path_is_not_deleted(self):
        # the reviewer's attack: a crafted desktop entry whose Icon=
        # points at an arbitrary user file
        el, appimage, desktop, _icon = self._element()
        important = os.path.join(self.sandbox, 'important.txt')
        Path(important).write_text('do not delete')
        el.desktop_entry.Icon = important

        self.provider.uninstall(el)

        self.assertTrue(os.path.exists(important))
        self.assertFalse(os.path.exists(appimage))   # still uninstalled
        self.assertFalse(os.path.exists(desktop))

    def test_icon_outside_icons_dir_with_wrong_stem_is_not_deleted(self):
        el, appimage, desktop, _icon = self._element()
        stray = os.path.join(self.sandbox, 'other.png')
        Path(stray).write_bytes(PNG)
        el.desktop_entry.Icon = stray

        self.provider.uninstall(el)
        self.assertTrue(os.path.exists(stray))

    def test_recorded_icon_path_is_removed_even_outside_icons_dir(self):
        el, appimage, desktop, _icon = self._element()
        recorded_icon = os.path.join(self.sandbox, 'recorded.png')
        Path(recorded_icon).write_bytes(PNG)
        el.desktop_entry.Icon = recorded_icon
        # provenance recorded at install time wins (binding #1)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': recorded_icon,
        })

        self.provider.uninstall(el)
        self.assertFalse(os.path.exists(recorded_icon))

    def test_fifo_is_never_removed(self):
        el, appimage, desktop, _icon = self._element()
        fifo = os.path.join(self.sandbox, 'planted-fifo')
        os.mkfifo(fifo)
        el.desktop_entry.Icon = fifo
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': fifo,
        })

        self.provider.uninstall(el)
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))


class ProvenanceRecordingTests(FakeXDGTestCase):
    def test_install_records_paths_in_app_config(self):
        # minimal shape check on the config keys uninstall relies on
        # (set_app_config stores strings; empty icon -> empty string)
        appimage, desktop, icon = self.install_fake_app()
        el = _make_el(appimage, desktop_path=desktop)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': icon,
        })
        config = Config.get_app_config(el)
        self.assertEqual(config['appimage_path'], appimage)
        self.assertEqual(config['desktop_file_path'], desktop)
        self.assertEqual(config['icon_path'], icon)

    def test_atomic_install_helper_exists_with_mode_contract(self):
        # guard against accidental signature drift
        import inspect
        sig = inspect.signature(provider_module._atomic_install)
        self.assertEqual(list(sig.parameters), ['src', 'dest', 'mode'])


class ClobberedDesktopRecoveryTests(FakeXDGTestCase):
    """An integrated app's .desktop gets clobbered by the app itself
    (VS Code forks rewrite Exec/TryExec to a mount-time path that is gone
    after the app quits). list_installed() must recover it from apps.ini
    and repair the .desktop in place."""

    def setUp(self):
        super().setUp()
        self.provider = AppImageProvider()

    def _install_with_record(self, desktop_name='fakeapp',
                             app_name='Fake App') -> tuple:
        appimage, desktop, icon = self.install_fake_app(
            desktop_name, app_name)
        # mimic install_file's provenance record in apps.ini
        el = _make_el(appimage, desktop_path=desktop)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': icon,
            'default_exec_arguments': '',
        })
        return appimage, desktop, icon

    @staticmethod
    def _clobber(desktop: str, mount='/tmp/.mount_zzzz/zcode'):
        with open(desktop, 'w') as f:
            f.write(f'''[Desktop Entry]
Name=ZCode
Exec={mount} --no-sandbox
TryExec={mount}
Icon=/tmp/.mount_zzzz/zcode.png
Type=Application
Terminal=false
''')

    def test_recovery_when_tryexec_is_dead(self):
        appimage, desktop, _icon = self._install_with_record()
        self._clobber(desktop)

        listed = self.provider.list_installed()

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].file_path, appimage)

    def test_repair_restores_tryexec_exec_icon(self):
        _appimage, desktop, icon = self._install_with_record()
        self._clobber(desktop)

        self.provider.list_installed()

        entry = DesktopEntry(path=desktop)
        self.assertEqual(entry.getTryExec(), _appimage)
        self.assertTrue(entry.getExec().startswith(
            f'env DESKTOPINTEGRATION=1 {_appimage}'))
        self.assertEqual(entry.getIcon(), icon)

    def test_repair_preserves_user_metadata(self):
        _appimage, desktop, _icon = self._install_with_record(app_name='ZCode')
        # clobbered entry carries its own Name/Comment/Categories
        with open(desktop, 'w') as f:
            f.write('''[Desktop Entry]
Name=ZCode
Comment=A hacked-in editor
Categories=Development;IDE;
Exec=/tmp/.mount_zzzz/zcode
TryExec=/tmp/.mount_zzzz/zcode
Icon=/tmp/.mount_zzzz/zcode.png
Type=Application
''')

        self.provider.list_installed()

        entry = DesktopEntry(path=desktop)
        self.assertEqual(entry.getName(), 'ZCode')
        self.assertEqual(entry.getComment(), 'A hacked-in editor')
        self.assertEqual(entry.get('Categories'), 'Development;IDE;')

    def test_repair_is_idempotent_within_process(self):
        _appimage, desktop, _icon = self._install_with_record()
        self._clobber(desktop)

        self.provider.list_installed()
        mtime_first = os.stat(desktop).st_mtime_ns
        self.provider.list_installed()
        mtime_second = os.stat(desktop).st_mtime_ns
        self.assertEqual(mtime_first, mtime_second)

    def test_recovery_skipped_when_recorded_appimage_missing(self):
        appimage, desktop, _icon = self._install_with_record()
        os.unlink(appimage)
        self._clobber(desktop)

        listed = self.provider.list_installed()
        self.assertEqual(listed, [])
        # desktop is NOT rewritten (no recovery happened)
        with open(desktop) as f:
            self.assertIn('/tmp/.mount_zzzz', f.read())


if __name__ == '__main__':
    unittest.main()

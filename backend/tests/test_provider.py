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
        # guard against accidental signature drift; `before_replace` is
        # the ticket-26 seam (defaulted, so it changes no caller)
        import inspect
        sig = inspect.signature(provider_module._atomic_install)
        self.assertEqual(list(sig.parameters),
                         ['src', 'dest', 'mode', 'before_replace'])
        self.assertIsNone(sig.parameters['before_replace'].default)


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


class ExecArgumentsTests(FakeXDGTestCase):
    """The desktop entry's Exec arguments reach the list element: the
    current entry wins, the install-time record is the fallback."""

    def setUp(self):
        super().setUp()
        self.provider = AppImageProvider()

    def _rewrite_exec(self, desktop: str, appimage: str, args: str):
        with open(desktop, 'w') as f:
            f.write(f'''[Desktop Entry]
Name=Fake App
Exec=env DESKTOPINTEGRATION=1 {appimage} {args}
TryExec={appimage}
Type=Application
Terminal=false
X-AppImage-Version=1.0
''')

    def test_arguments_come_from_the_current_desktop_entry(self):
        appimage, desktop, _icon = self.install_fake_app()
        self._rewrite_exec(desktop, appimage, '--no-sandbox %U')

        listed = self.provider.list_installed()

        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].exec_arguments, '--no-sandbox %U')

    def test_field_codes_stay_verbatim(self):
        appimage, desktop, _icon = self.install_fake_app()
        self._rewrite_exec(desktop, appimage, '%U')

        listed = self.provider.list_installed()

        self.assertEqual(listed[0].exec_arguments, '%U')

    def test_no_arguments_means_empty_string(self):
        self.install_fake_app()

        listed = self.provider.list_installed()

        self.assertEqual(listed[0].exec_arguments, '')

    def test_recorded_arguments_are_the_fallback(self):
        appimage, desktop, icon = self.install_fake_app()
        el = _make_el(appimage, desktop_path=desktop)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': icon,
            'default_exec_arguments': '--no-sandbox %U',
        })

        listed = self.provider.list_installed()

        self.assertEqual(listed[0].exec_arguments, '--no-sandbox %U')


class StaleRecordTests(FakeXDGTestCase):
    """A record whose .desktop file is gone is invisible to the
    desktop-driven listing — it must be logged, never dropped silently,
    and never removed without an explicit request."""

    def setUp(self):
        super().setUp()
        self.provider = AppImageProvider()

    def _record_without_desktop_file(self) -> tuple:
        appimage, desktop, icon = self.install_fake_app()
        os.unlink(desktop)          # the desktop file is gone
        el = _make_el(appimage)    # no desktop entry to read any more
        el.desktop_file_path = desktop
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': icon,
            'default_exec_arguments': '--no-sandbox %U',
        })
        return appimage, desktop, icon

    def test_stale_record_is_logged_not_listed(self):
        appimage, desktop, _icon = self._record_without_desktop_file()

        with self.assertLogs(level='WARNING') as logs:
            listed = self.provider.list_installed()

        # not listed as an installed app…
        self.assertEqual(listed, [])
        # …but reported, with the missing desktop file and the orphaned
        # AppImage, so the user can reclaim the file
        message = '\n'.join(logs.output)
        self.assertIn(desktop, message)
        self.assertIn(appimage, message)

    def test_list_stale_records_finds_the_orphan(self):
        appimage, desktop, _icon = self._record_without_desktop_file()

        stale = self.provider.list_stale_records()

        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0].file_path, appimage)
        self.assertEqual(stale[0].desktop_file_path, desktop)
        self.assertEqual(stale[0].exec_arguments, '--no-sandbox %U')

    def test_live_record_is_not_stale(self):
        appimage, desktop, icon = self.install_fake_app()
        el = _make_el(appimage, desktop_path=desktop)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': icon,
        })

        self.assertEqual(self.provider.list_stale_records(), [])

    def test_listing_never_removes_the_orphan(self):
        appimage, _desktop, _icon = self._record_without_desktop_file()

        self.provider.list_installed()

        self.assertTrue(os.path.isfile(appimage))


class ResolveInstallNamesTests(unittest.TestCase):
    """Round-4 review: collision candidates are rebuilt from embedded,
    attacker-controlled desktop fields (Name, X-AppImage-Version). Every
    candidate must be a single flat filename — basename-safe, no
    separators — or a colliding package writes outside the managed
    directories (including ~/.config/autostart)."""

    HOSTILE_NAME = '../../.config/autostart/evil'
    HOSTILE_VERSION = '1.0/../../../.config/autostart/evil'

    def test_plain_name_unchanged(self):
        self.assertEqual(
            provider_module._resolve_install_names(
                'something.AppImage', 'Neovim', 'v0.11.3', []),
            ('neovim.appimage', 'neovim'))

    def test_gearlever_fallback_and_numeric_suffixes(self):
        self.assertEqual(
            provider_module._resolve_install_names(
                'tool.AppImage', None, '', []),
            ('gearlever_tool.appimage', 'gearlever_tool'))
        self.assertEqual(
            provider_module._resolve_install_names(
                'tool.AppImage', None, '', ['gearlever_tool.appimage']),
            ('gearlever_tool.appimage_0.appimage', 'gearlever_tool.appimage_0'))

    def test_version_suffix_shape_preserved(self):
        # benign data: the collision suffix shape is exactly upstream's
        self.assertEqual(
            provider_module._resolve_install_names(
                'x.AppImage', 'Neovim', 'v0.11.3', ['neovim.appimage']),
            ('neovim.appimage_v0_11_3.appimage', 'neovim.appimage_v0_11_3'))

    def test_collision_desktop_name_traversal_stays_inside(self):
        # the sanitized hostile name takes the first slot; the collision
        # suffix path must still produce a flat, separator-free filename
        first = provider_module.remove_special_chars(
            self.HOSTILE_NAME.lower().replace(' ', '_') + '.appimage').lower()
        name, prefix = provider_module._resolve_install_names(
            'x.AppImage', self.HOSTILE_NAME, '', [first])
        self.assertNotEqual(name, first)  # actually took the suffix path
        self.assertTrue(name.startswith(first[:-len('.appimage')]
                                        + '.appimage_'))
        self.assertTrue(name.endswith('_0.appimage'))
        self.assertEqual(os.path.basename(name), name)
        self.assertEqual(os.path.basename(prefix), prefix)
        self.assertNotIn('/', name)
        self.assertNotIn('/', prefix)

    def test_collision_version_traversal_stays_inside(self):
        # X-AppImage-Version is embedded too: its separators must never
        # reach the destination path on the collision path. (The sanitized
        # stem may still END in the payload's letters — flatness is the
        # invariant, not the spelling: a flat candidate lands inside the
        # managed directory whatever it reads.)
        name, prefix = provider_module._resolve_install_names(
            'x.AppImage', 'Evil App', self.HOSTILE_VERSION,
            ['evil_app.appimage'])
        self.assertEqual(os.path.basename(name), name)
        self.assertNotIn('/', name)
        self.assertNotIn(os.sep, name)
        self.assertEqual(os.path.basename(prefix), prefix)
        self.assertNotIn('/', prefix)


class AppVersionTests(unittest.TestCase):
    """_get_app_version: X-AppImage-Version only.

    GearLever fell back to the first six hex chars of the file's md5 when
    an image shipped no X-AppImage-Version, and install_file's KEEP branch
    wrote that "version" into the desktop entry's Name — so a freshly
    integrated AppImage showed up in the launcher and the panel as
    "Terax (77efdf)": a hash presented as the app's version."""

    class _Entry:
        def __init__(self, version):
            self.version = version

        def get(self, key, default=None):
            if key == 'X-AppImage-Version':
                return self.version
            return default

    class _Extracted:
        def __init__(self, version):
            self.desktop_entry = AppVersionTests._Entry(version)
            self.md5 = '77efdf43468678a2e98edeff02ccc193'

    def setUp(self):
        self.provider = AppImageProvider()

    def test_real_version_is_returned(self):
        self.assertEqual(
            self.provider._get_app_version(self._Extracted('3.3.1')), '3.3.1')

    def test_no_version_means_no_version_not_the_md5_hash(self):
        self.assertEqual(self.provider._get_app_version(self._Extracted('')), '')
        self.assertEqual(self.provider._get_app_version(self._Extracted(None)), '')

    def test_desktop_entry_only_call(self):
        self.assertEqual(
            self.provider._get_app_version(
                None, desktop_entry=self._Entry('1.4.237')), '1.4.237')
        self.assertEqual(
            self.provider._get_app_version(None, desktop_entry=self._Entry('')), '')


if __name__ == '__main__':
    unittest.main()

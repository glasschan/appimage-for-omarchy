# test_cli_listing.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import json
import os
from pathlib import Path
from unittest import mock

from cli_support import InProcessCliMixin
from helpers import FakeXDGTestCase
from test_procs import FakeProbe

from omarchy_appimage import cli, procs, utils
from omarchy_appimage.ini_config import Config
from omarchy_appimage.provider import AppImageListElement, InstalledStatus


class RuntimeBlockCliTests(InProcessCliMixin, FakeXDGTestCase):
    """The additive `runtime` envelope block on --list-installed --json
    (schema_version stays 1): best-effort fuse2 states for the panel's
    launch-failure banner. The probe runs once per call, never per app,
    and can never fail the listing."""

    def test_envelope_carries_runtime_fuse2(self):
        with mock.patch.object(utils, 'probe_fuse2',
                               return_value='missing'):
            out, _err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['runtime'], {'fuse2': 'missing'})

    def test_probe_runs_once_per_call_not_per_app(self):
        self.install_fake_app()
        self.install_fake_app('secondapp', 'Second App')
        with mock.patch.object(utils, 'probe_fuse2',
                               return_value='present') as probe:
            out, _err, code = self.run_cmd(cli.cmd_list_installed,
                                           ['--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(len(payload['installed']), 2)
        self.assertEqual(probe.call_count, 1)

    def test_probe_crash_keeps_call_successful(self):
        # belt and braces: probe_fuse2 never raises by contract, but a
        # crash must still not fail the listing — the block is always
        # present, worst case with 'unknown'
        with mock.patch.object(utils, 'probe_fuse2',
                               side_effect=RuntimeError('boom')):
            out, _err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload['runtime'], {'fuse2': 'unknown'})


class RunningPidsCliTests(InProcessCliMixin, FakeXDGTestCase):
    """`running_pids` (ticket #27): the app object's process count, the
    number the panel's force-quit card names before the user agrees to
    close the app. It is computed through procs.pids_for — the module that
    owns the identity rule (runtime parent + FUSE-mount children) — and
    ONLY for rows the listing reports as running; a failed scan degrades
    to 0 and never fails the listing."""

    def setUp(self):
        super().setUp()
        self.appimage, _desktop, _icon = self.install_fake_app()

    def _list_with(self, running, pids_for):
        with mock.patch.object(cli.appimage_provider, 'is_app_running',
                               return_value=running), \
                mock.patch.object(procs, 'pids_for', pids_for):
            out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        return json.loads(out), err, code

    def test_running_row_reports_the_count(self):
        scan = mock.Mock(
            return_value={'parent': [700], 'children': [701, 702]})
        payload, err, code = self._list_with(True, scan)
        self.assertEqual(code, 0, err)
        app = payload['installed'][0]
        self.assertTrue(app['running'])
        self.assertEqual(app['running_pids'], 3)
        # the listing hands the shared scan down, so a running row costs
        # one `ps` table for the whole document (finding 8)
        scan.assert_called_once_with(self.appimage, probe=mock.ANY)

    def test_non_running_row_reports_zero_without_a_scan(self):
        scan = mock.Mock(return_value={'parent': [700], 'children': [701]})
        payload, err, code = self._list_with(False, scan)
        self.assertEqual(code, 0, err)
        app = payload['installed'][0]
        self.assertFalse(app['running'])
        self.assertEqual(app['running_pids'], 0)
        # a non-running row must not pay for a second process scan
        scan.assert_not_called()

    def test_scan_failure_degrades_to_zero_and_keeps_the_listing(self):
        scan = mock.Mock(side_effect=OSError('ps exploded'))
        payload, err, code = self._list_with(True, scan)
        self.assertEqual(code, 0, err)
        app = payload['installed'][0]
        # the `running` verdict is untouched by the count's failure
        self.assertTrue(app['running'])
        self.assertEqual(app['running_pids'], 0)

    def test_every_running_row_shares_one_process_table(self):
        # finding 8: the panel polls this listing every 10 s, so two
        # running rows must not cost two `ps` spawns and two /proc/mounts
        # parses — the scan is built once per listing
        self.install_fake_app(desktop_name='secondapp', app_name='Second')
        first = os.path.join(self.managed_dir, 'fakeapp.appimage')
        second = os.path.join(self.managed_dir, 'secondapp.appimage')
        probe = FakeProbe(processes=[(700, first), (701, second)])
        with mock.patch.object(procs, '_PROBE', probe), \
                mock.patch.object(cli.appimage_provider, 'is_app_running',
                                  return_value=True):
            out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0, err)
        apps = json.loads(out)['installed']
        self.assertEqual(len(apps), 2)
        self.assertEqual([a['running_pids'] for a in apps], [1, 1])
        self.assertEqual(probe.scans, 1)


class ExecArgumentsCliTests(FakeXDGTestCase):
    """`exec_arguments` hands the desktop entry's Exec arguments to the UI:
    the current entry wins, the install-time record is the fallback."""

    def _install_with_exec(self, args: str) -> str:
        appimage, desktop, icon = self.install_fake_app()
        with open(desktop, 'w') as f:
            f.write(f'''[Desktop Entry]
Name=Fake App
Exec=env DESKTOPINTEGRATION=1 {appimage} {args}
TryExec={appimage}
Icon={icon}
Type=Application
Terminal=false
X-AppImage-Version=1.0
''')
        return appimage

    def _listed(self) -> dict:
        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(len(payload['installed']), 1)
        return payload['installed'][0]

    def _record(self, default_exec_arguments: str) -> None:
        appimage, desktop, icon = self.install_fake_app()
        el = AppImageListElement(name='Fake App', description='',
                                 provider='AppImage',
                                 installed_status=InstalledStatus.INSTALLED,
                                 file_path=appimage,
                                 desktop_file_path=desktop)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': desktop,
            'icon_path': icon,
            'default_exec_arguments': default_exec_arguments,
        })

    def test_exec_arguments_come_from_the_desktop_entry(self):
        self._install_with_exec('--no-sandbox %U')
        self.assertEqual(self._listed()['exec_arguments'], '--no-sandbox %U')

    def test_field_codes_stay_verbatim(self):
        self._install_with_exec('%U')
        self.assertEqual(self._listed()['exec_arguments'], '%U')

    def test_exec_arguments_empty_without_arguments(self):
        self.install_fake_app()
        self.assertEqual(self._listed()['exec_arguments'], '')

    def test_recorded_arguments_are_the_fallback(self):
        # the current desktop entry carries no arguments: the install-time
        # record (apps.ini) is used instead
        self._record('--no-sandbox %U')
        self.assertEqual(self._listed()['exec_arguments'], '--no-sandbox %U')

    def test_current_entry_wins_over_the_record(self):
        self._record('--old-flag %U')
        self._install_with_exec('--no-sandbox %U')
        self.assertEqual(self._listed()['exec_arguments'], '--no-sandbox %U')


class StaleRecordCliTests(FakeXDGTestCase):
    """A record whose .desktop file is gone is logged, never removed
    automatically, and can be cleaned up explicitly by its desktop id."""

    def _install_stale(self) -> tuple:
        os.makedirs(os.path.join(self.managed_dir, '.icons'), exist_ok=True)
        os.makedirs(self.applications_dir, exist_ok=True)

        appimage = os.path.join(self.managed_dir, 'stale.appimage')
        Path(appimage).write_bytes(
            b'\x7fELF\x02\x01\x01\x00\x41\x49\x02padding')
        os.chmod(appimage, 0o755)
        icon = os.path.join(self.managed_dir, '.icons', 'stale.png')
        Path(icon).write_bytes(b'\x89PNG\r\n\x1a\n')
        # the desktop file is gone (deleted by hand, or never written)
        missing_desktop = os.path.join(self.applications_dir, 'stale.desktop')

        el = AppImageListElement(name='Stale App', description='',
                                 provider='AppImage',
                                 installed_status=InstalledStatus.INSTALLED,
                                 file_path=appimage,
                                 desktop_file_path=missing_desktop)
        Config.set_app_config(el, {
            'appimage_path': appimage,
            'desktop_file_path': missing_desktop,
            'icon_path': icon,
            'default_exec_arguments': '--no-sandbox %U',
        })
        return appimage, icon, missing_desktop

    def _apps_ini(self) -> str:
        path = os.path.join(self.config_home, 'io.github.glasschan.appimage',
                            'apps.ini')
        if not os.path.isfile(path):
            return ''
        with open(path) as f:
            return f.read()

    def test_stale_record_is_logged_and_not_listed(self):
        appimage, _icon, missing_desktop = self._install_stale()

        result = self.run_cli('--list-installed', '--json')

        # the run does not crash
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        # the record is not listed as an installed app…
        self.assertEqual(payload['installed'], [])
        # …but it is not silently dropped either: the orphan is reported
        # with both the missing desktop file and the orphaned AppImage
        self.assertIn(missing_desktop, result.stderr)
        self.assertIn(appimage, result.stderr)
        # and nothing was removed
        self.assertTrue(os.path.isfile(appimage))
        self.assertIn('stale', self._apps_ini())

    def test_stale_record_can_be_removed_by_desktop_id(self):
        appimage, icon, _missing_desktop = self._install_stale()

        result = self.run_cli('--remove', 'stale.desktop', '--yes', '--json')

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['result'], 'removed')
        # the orphaned AppImage and its icon went to the trash
        self.assertFalse(os.path.exists(appimage))
        self.assertTrue(os.path.isfile(
            os.path.join(self.trash_files, 'stale.appimage')))
        self.assertTrue(os.path.isfile(
            os.path.join(self.trash_files, 'stale.png')))
        # the record is gone
        self.assertNotIn('stale', self._apps_ini())
        # and the listing is still fine
        payload = json.loads(self.run_cli('--list-installed', '--json').stdout)
        self.assertEqual(payload['installed'], [])

    def test_stale_record_is_not_removed_by_a_listing(self):
        appimage, _icon, _missing_desktop = self._install_stale()

        self.run_cli('--list-installed', '--json')
        self.run_cli('--list-installed')

        # a listing never removes anything
        self.assertTrue(os.path.isfile(appimage))
        self.assertIn('stale', self._apps_ini())

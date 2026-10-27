# test_cli_listing.py — end-to-end CLI tests via subprocess in a fake XDG sandbox.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import errno
import json
import os
import time
import zlib
from pathlib import Path
from unittest import mock

from cli_support import InProcessCliMixin
from helpers import FakeXDGTestCase
from test_icon import build_png, chunk_types
from test_procs import FakeProbe

from omarchy_appimage import cli, icon, janitor, procs, utils
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


class LaunchArgvCliTests(FakeXDGTestCase):
    """`launch_argv` (ticket #32): the listing hands the panel its launch
    command — the argv Quickshell.execDetached spawns — built by the ONE
    launch builder (omarchy_appimage.launch) instead of the QML. Additive
    key on the app object; schema_version stays 1."""

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

    def test_launch_argv_prefixes_env_desktopintegration(self):
        self._install_with_exec('--no-sandbox %U')
        app = self._listed()
        self.assertEqual(
            app['launch_argv'],
            ['env', 'DESKTOPINTEGRATION=1', app['path'], '--no-sandbox'])

    def test_launch_argv_drops_the_field_codes(self):
        self._install_with_exec('--flag %U')
        app = self._listed()
        self.assertNotIn('%U', app['launch_argv'])
        self.assertIn('--flag', app['launch_argv'])

    def test_launch_argv_without_arguments_is_just_the_prefix(self):
        self._install_with_exec('')
        app = self._listed()
        self.assertEqual(
            app['launch_argv'],
            ['env', 'DESKTOPINTEGRATION=1', app['path']])


class LaunchScaleArgvCliTests(LaunchArgvCliTests):
    """`launch_scale_argv` (ticket #35): the argv tail for the backend's
    `--exec-with-scale` entry point — the image plus its own arguments,
    field codes dropped. The panel spawns
    `[backendCommand..., '--exec-with-scale', *launch_scale_argv]` so the
    panel launch shares the launcher's scale mechanism; `launch_argv`
    stays for an older shell pairing and as the non-scale fallback.
    Additive key; schema_version stays 1."""

    def test_launch_scale_argv_leads_with_the_image(self):
        self._install_with_exec('--no-sandbox %U')
        app = self._listed()
        self.assertEqual(app['launch_scale_argv'],
                         [app['path'], '--no-sandbox'])

    def test_launch_scale_argv_drops_the_field_codes(self):
        self._install_with_exec('--flag %U')
        app = self._listed()
        self.assertEqual(app['launch_scale_argv'], [app['path'], '--flag'])

    def test_launch_scale_argv_without_arguments_is_just_the_image(self):
        self._install_with_exec('')
        app = self._listed()
        self.assertEqual(app['launch_scale_argv'], [app['path']])

    def test_launch_scale_argv_agrees_with_launch_argv_behind_the_prefix(self):
        self._install_with_exec('--no-sandbox %U')
        app = self._listed()
        self.assertEqual(app['launch_argv'][2:], app['launch_scale_argv'])


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


class IconSafeCliTests(InProcessCliMixin, FakeXDGTestCase):
    """`icon_safe` (ticket #62, per-candidate since #63): the listing's
    display gate. The panel must render its built-in glyph unless the
    CURRENT sanitiser proved each icon candidate — reported as a
    `{"png": bool, "svg": bool}` dict, one key per EXISTING candidate —
    proven here end-to-end through the throttled sweep, the persisted
    verdict registry and the throttle marker."""

    def setUp(self):
        super().setUp()
        self.appimage, _desktop, _old_icon = self.install_fake_app()
        self.icons_dir = os.path.join(self.managed_dir, '.icons')
        self.icon_path = os.path.join(self.icons_dir, 'fakeapp.png')
        self.svg_path = os.path.join(self.icons_dir, 'fakeapp.svg')
        self.marker = janitor.sweep_marker_path()

    def _write_icon(self, data, path=None):
        path = path or self.icon_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_bytes(data)

    def _hostile_png(self):
        bomb = zlib.compress(b'A' * (1024 * 1024), 9)
        return build_png(8, 8, extra=[(b'zTXt', b'C\x00\x00' + bomb)])

    def _unsafe_svg(self):
        return (b'<svg xmlns="http://www.w3.org/2000/svg" '
                b'width="8" height="8"><script/></svg>')

    def _valid_svg(self):
        return (b'<svg xmlns="http://www.w3.org/2000/svg" '
                b'width="8" height="8"/>')

    def _throttle_sweep(self):
        # a refused icon DELETED is invisible to the verdict (the key
        # disappears); to pin the verdict itself, the sweep is throttled
        # away with a current-version marker so the refused file survives
        # to be judged
        Path(self.marker).parent.mkdir(parents=True, exist_ok=True)
        Path(self.marker).write_text(json.dumps(
            {'v': icon.SANITIZER_VERSION, 'at': time.time()}))

    def _list(self):
        out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(len(payload['installed']), 1)
        return payload['installed'][0]

    def test_retained_hostile_icon_is_sanitised_and_reported_safe(self):
        self._write_icon(self._hostile_png())
        app = self._list()
        self.assertEqual(app['icon_safe'], {'png': True})
        with open(self.icon_path, 'rb') as handle:
            rewritten = handle.read()
        self.assertNotIn(b'zTXt', rewritten)
        self.assertEqual(chunk_types(rewritten),
                         [b'IHDR', b'IDAT', b'IEND'])

    def test_safe_png_with_refused_svg_reports_each_candidate(self):
        # ticket #63: the row's Image falls through to the next candidate
        # when one fails to DECODE, so each existing candidate carries
        # its own verdict — the safe png must not vouch for the svg
        self._write_icon(self._hostile_png())
        self._write_icon(self._unsafe_svg(), self.svg_path)
        self._throttle_sweep()
        self.assertEqual(self._list()['icon_safe'],
                         {'png': True, 'svg': False})
        # the refused file survives (deletion is the sweep's) and stays
        # refused on the next poll
        self.assertTrue(os.path.exists(self.svg_path))
        self.assertEqual(self._list()['icon_safe'],
                         {'png': True, 'svg': False})

    def test_both_safe_candidates_report_both_true(self):
        self._write_icon(self._hostile_png())
        self._write_icon(self._valid_svg(), self.svg_path)
        self.assertEqual(self._list()['icon_safe'],
                         {'png': True, 'svg': True})

    def test_svg_only_unsafe_reports_only_the_svg_key(self):
        # install_fake_app leaves an unsanitisable stub png: the
        # svg-only scenario removes it first
        os.unlink(self.icon_path)
        self._write_icon(self._unsafe_svg(), self.svg_path)
        self._throttle_sweep()
        self.assertEqual(self._list()['icon_safe'], {'svg': False})

    def test_fresh_marker_skips_the_sweep_and_reports_unsafe(self):
        # the sweep is throttled away, so nothing removed the refused
        # icon — the verdict must still refuse it (a glyph, never the
        # file); a metadata-laden but sanitisable icon is instead
        # REWRITTEN by the verdict itself (see the safe-path test)
        self._write_icon(build_png(60000, 60000, idat=zlib.compress(b'')))
        self._throttle_sweep()
        self.assertEqual(self._list()['icon_safe'], {'png': False})
        self.assertTrue(os.path.exists(self.icon_path))

    def test_failed_rewrite_reports_unsafe_and_keeps_the_file(self):
        self._write_icon(self._hostile_png())
        with mock.patch.object(icon, 'replace_icon_file',
                               side_effect=OSError(errno.ENOSPC,
                                                   'no space')):
            self.assertEqual(self._list()['icon_safe'], {'png': False})
        self.assertIn(b'zTXt', Path(self.icon_path).read_bytes())

    def test_no_icon_file_reports_no_verdict(self):
        # the stub icon install_fake_app leaves is unsanitisable, so the
        # no-icon scenario (empty dict → null) removes it first
        os.unlink(self.icon_path)
        self.assertIsNone(self._list()['icon_safe'])

    def test_proven_icons_stay_safe_through_the_registry_fast_path(self):
        # first listing proves (and rewrites) the icons and persists the
        # verdicts; the second listing must answer from the registry
        # WITHOUT a read or a decode — proven by the poisoned sanitiser
        self._write_icon(self._hostile_png())
        self._write_icon(self._valid_svg(), self.svg_path)
        self.assertEqual(self._list()['icon_safe'],
                         {'png': True, 'svg': True})
        with mock.patch.object(icon, 'sanitize_icon',
                               side_effect=AssertionError('fast path')):
            self.assertEqual(self._list()['icon_safe'],
                             {'png': True, 'svg': True})

    def test_a_clean_poll_writes_no_registry_save(self):
        # the registry write must not run on every 10 s poll: once every
        # candidate carries a current verdict, the listing persists
        # nothing
        self._write_icon(self._hostile_png())
        self.assertEqual(self._list()['icon_safe'], {'png': True})
        with mock.patch.object(icon, 'save_sanitized_registry') as save:
            self.assertEqual(self._list()['icon_safe'], {'png': True})
        save.assert_not_called()

    def test_registry_save_failure_never_fails_the_listing(self):
        # a registry write failure is best-effort by contract: the
        # listing still succeeds and still reports its verdict
        self._write_icon(self._hostile_png())
        with mock.patch.object(icon.utils, 'atomic_write',
                               side_effect=OSError(errno.ENOSPC,
                                                   'no space')):
            app = self._list()
        self.assertEqual(app['icon_safe'], {'png': True})

# test_check_state.py — issue #53: the last check's verdict is ONE durable
# record owned by the backend. Both check verbs write it, the installation
# listing reports it (so the panel's row, its badge and its Update button
# all read the same value on every poll instead of losing it ten seconds
# later), a successful update retires it, and the background announcement
# follows the write instead of preceding it.
#
# Test suite written for this plugin. Released under the GNU GPL-3.0 — see
# the repo's LICENSE for terms.

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from helpers import FakeXDGTestCase

from omarchy_appimage import cli, net, settings


class CliTestCase(FakeXDGTestCase):
    """Sandbox, one installed fake app, and the check seams the tests use.

    Every manager here is a Mock because these tests are about the RECORD,
    not about any publisher's HTTP shape — the source-level checks live in
    test_availability.py and test_sources.py. The manager therefore names
    the element it belongs to and returns real values for the two
    attributes the record stores (`available_version`, `download_size`),
    so the state file stays JSON-serialisable."""

    def setUp(self):
        super().setUp()
        _appimage, _desktop, _icon = self.install_fake_app()
        self.el = cli.appimage_provider.list_installed()[0]

    def _manager(self, version='v2.0', size=5000, installable=True,
                 available=True):
        manager = mock.Mock(download_size=size, available_version=version,
                            embedded=False)
        manager.name = 'GithubUpdater'
        manager.el = self.el
        manager.verification_method.return_value = (
            'sha256' if installable else 'size')
        manager.installable.return_value = installable
        manager.is_update_available.return_value = available
        return manager

    def _with_manager(self, manager):
        patcher = mock.patch.object(cli.UpdateManagerChecker,
                                    'check_url_for_app',
                                    return_value=manager)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _check(self, cmd=cli.cmd_fetch_updates):
        with mock.patch.object(net, 'check_internet', return_value=True):
            return self.run_cmd(cmd, ['--json'])

    def _listing(self):
        out, err, code = self.run_cmd(cli.cmd_list_installed, ['--json'])
        self.assertEqual(code, 0, err)
        return json.loads(out)['installed'][0]

    def run_cmd(self, func, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = func(args)
            except cli.UsageError as e:
                err.write(f'Error: {e}\n')
                code = 2
            except (cli.OperationError, cli.InternalError) as e:
                err.write(f'Error: {e}\n')
                code = 1
        return out.getvalue(), err.getvalue(), code


class VerdictRecordTests(CliTestCase):
    """What the record says, and what the listing reports from it."""

    def test_a_fresh_listing_reports_no_pending_verdict(self):
        app = self._listing()
        self.assertFalse(app['pending_update'])
        self.assertIsNone(app['pending_version'])
        self.assertIsNone(app['pending_installable'])

    def test_the_manual_check_writes_the_verdict_the_listing_reports(self):
        self._with_manager(self._manager())
        _out, err, code = self._check(cli.cmd_list_updates)
        self.assertEqual(code, 0, err)

        app = self._listing()
        self.assertTrue(app['pending_update'])
        self.assertEqual(app['pending_version'], 'v2.0')
        self.assertIs(app['pending_installable'], True)

    def test_the_background_check_writes_the_verdict_too(self):
        self._with_manager(self._manager())
        with mock.patch.object(cli, 'run_command',
                               mock.Mock(return_value=mock.Mock(returncode=0))):
            _out, err, code = self._check()
        self.assertEqual(code, 0, err)

        # the record is a file, so a later process reads the same verdict
        # without asking the network again
        stored = settings.load_updates_state()['fakeapp.desktop']
        self.assertEqual(stored['version'], 'v2.0')
        self.assertIs(stored['installable'], True)
        self.assertEqual(stored['path'], self.el.file_path)

        app = self._listing()
        self.assertTrue(app['pending_update'])
        self.assertEqual(app['pending_version'], 'v2.0')

    def test_a_listing_reports_the_verdict_without_running_a_check(self):
        # the regression this record exists for: the panel polls this
        # listing every 10 s and must not lose what the last check found
        settings.save_updates_state({'fakeapp.desktop': {
            'signature': 'v2.0|5000', 'installable': True,
            'version': 'v2.0', 'path': self.el.file_path}})
        for _round in range(2):
            app = self._listing()
            self.assertTrue(app['pending_update'])
            self.assertEqual(app['pending_version'], 'v2.0')
            self.assertIs(app['pending_installable'], True)

    def test_a_source_that_cannot_install_reports_its_verdict(self):
        self._with_manager(self._manager(installable=False))
        _out, err, code = self._check(cli.cmd_list_updates)
        self.assertEqual(code, 0, err)
        app = self._listing()
        self.assertTrue(app['pending_update'])
        self.assertIs(app['pending_installable'], False)

    def test_a_legacy_entry_reads_as_pending_without_a_version(self):
        # upgrading must not lose a release the previous version recorded;
        # the old shape named no version and carried no verdict
        settings.save_updates_state({'fakeapp.desktop': 'v9.0|9000'})
        app = self._listing()
        self.assertTrue(app['pending_update'])
        self.assertIsNone(app['pending_version'])
        self.assertIsNone(app['pending_installable'])

    def test_a_corrupt_installability_reads_as_unknown(self):
        # only a real boolean is a verdict. A hand-edited or damaged
        # record saying "false" (a string) must not reach the panel as
        # "this can be installed" — the panel offers an Update button
        # for anything that is not exactly false.
        settings.save_updates_state({'fakeapp.desktop': {
            'signature': 'v2.0|5000', 'installable': 'false',
            'version': 'v2.0', 'path': self.el.file_path}})
        app = self._listing()
        self.assertTrue(app['pending_update'])
        self.assertIsNone(app['pending_installable'])


class VerdictRetirementTests(CliTestCase):
    """A verdict is retired by the truth, never by a silence."""

    def test_an_up_to_date_app_loses_its_verdict(self):
        settings.save_updates_state({'fakeapp.desktop': {
            'signature': 'v1.0|1', 'installable': True,
            'version': 'v1.0', 'path': self.el.file_path}})
        # the source named a release and answered "no newer one": that is
        # a definite verdict, so nothing is pending any more
        self._with_manager(self._manager(version='v1.0', available=False))

        _out, err, code = self._check(cli.cmd_list_updates)
        self.assertEqual(code, 0, err)
        self.assertNotIn('fakeapp.desktop', settings.load_updates_state())
        self.assertFalse(self._listing()['pending_update'])

    def test_a_failed_check_keeps_the_verdict(self):
        record = {'signature': 'v1.0|1', 'installable': True,
                  'version': 'v1.0', 'path': self.el.file_path}
        settings.save_updates_state({'fakeapp.desktop': record})
        manager = self._manager()
        manager.is_update_available.side_effect = OSError('source unreachable')
        self._with_manager(manager)

        _out, err, code = self._check()
        self.assertEqual(code, 0, err)
        self.assertEqual(settings.load_updates_state()['fakeapp.desktop'],
                         record)
        app = self._listing()
        self.assertTrue(app['pending_update'])
        self.assertEqual(app['pending_version'], 'v1.0')

    def test_an_undeterminable_check_keeps_the_verdict(self):
        # a source that answers None has not decided anything, so the last
        # known result stands (issue #43's rule, now applied to the record)
        record = {'signature': 'v1.0|1', 'installable': True,
                  'version': 'v1.0', 'path': self.el.file_path}
        settings.save_updates_state({'fakeapp.desktop': record})
        manager = self._manager()
        manager.is_update_available.return_value = None
        self._with_manager(manager)

        _out, err, code = self._check()
        self.assertEqual(code, 0, err)
        self.assertEqual(settings.load_updates_state()['fakeapp.desktop'],
                         record)

    def test_an_update_retires_the_verdict(self):
        settings.save_updates_state({'fakeapp.desktop': {
            'signature': 'v2.0|5000', 'installable': True,
            'version': 'v2.0', 'path': self.el.file_path}})
        self._with_manager(self._manager())
        new_el = mock.Mock(version='v2.0', size=11,
                           file_path=self.el.file_path,
                           desktop_file_path=self.el.desktop_file_path)
        with mock.patch.object(cli.appimage_provider, 'update_from_url',
                               return_value=new_el):
            _out, err, code = self.run_cmd(cli.cmd_update, ['fakeapp', '--yes'])
        self.assertEqual(code, 0, err)

        self.assertNotIn('fakeapp.desktop', settings.load_updates_state())
        self.assertFalse(self._listing()['pending_update'])

    def test_an_up_to_date_update_retires_the_verdict(self):
        # the pipeline's own availability check is a truth too: it proved
        # there is nothing newer to install, so the pinned verdict is
        # stale and goes - the row must not keep offering a release the
        # backend just said does not exist
        settings.save_updates_state({'fakeapp.desktop': {
            'signature': 'v9.0|9000', 'installable': True,
            'version': 'v9.0', 'path': self.el.file_path}})
        self._with_manager(self._manager(version='v1.0', available=False))

        out, err, code = self.run_cmd(cli.cmd_update,
                                      ['fakeapp', '--yes', '--json'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['result'], 'up-to-date')

        self.assertNotIn('fakeapp.desktop', settings.load_updates_state())
        self.assertFalse(self._listing()['pending_update'])


class AnnouncementOrderTests(CliTestCase):
    """The record is written first, and the announcement is its consequence."""

    def test_the_record_is_written_before_the_announcement(self):
        self._with_manager(self._manager())
        seen = {}

        def fake_run_command(command, check=False):
            # what the state file says AT THE MOMENT of the announcement
            seen['state'] = settings.load_updates_state()
            return mock.Mock(returncode=0)

        with mock.patch.object(cli, 'run_command', fake_run_command):
            out, err, code = self._check()
        self.assertEqual(code, 0, err)

        self.assertTrue(json.loads(out)['notified'])
        self.assertIn('fakeapp.desktop', seen['state'])
        self.assertEqual(seen['state']['fakeapp.desktop']['version'], 'v2.0')

    def test_a_failed_write_announces_nothing(self):
        self._with_manager(self._manager())
        sent = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(cli, 'run_command', sent), \
                mock.patch.object(settings, 'save_updates_state',
                                  side_effect=OSError('disk full')):
            out, err, code = self._check()
        self.assertEqual(code, 0, err)

        # a sweep that learned nothing durable has nothing new to announce:
        # announcing anyway is what made the same release re-announce on
        # every cycle while the write kept failing
        self.assertFalse(json.loads(out)['notified'])
        self.assertEqual(sent.call_count, 0)

    def test_an_unchanged_verdict_is_not_announced(self):
        settings.save_updates_state({'fakeapp.desktop': {
            'signature': 'v2.0|5000', 'installable': True,
            'version': 'v2.0', 'path': self.el.file_path}})
        self._with_manager(self._manager())
        sent = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(cli, 'run_command', sent):
            out, err, code = self._check()
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out)['notified'])
        self.assertEqual(sent.call_count, 0)


if __name__ == '__main__':
    unittest.main()

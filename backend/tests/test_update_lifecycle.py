# test_update_lifecycle.py — issue #46: one update lifecycle, one claim.
#
# The claim lives inside run_update, so a second update of the same app is
# refused the same way whether it was started by the panel's background job
# or by a terminal --update. Removal during that app's job is refused by the
# same module that owns the job.
#
# Test suite written for this plugin. Released under the GNU GPL-3.0 — see
# the repo's LICENSE for terms.

import io
import os
import signal
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from helpers import FakeXDGTestCase

from omarchy_appimage import cli, jobs


class LifecycleTestCase(FakeXDGTestCase):
    def setUp(self):
        super().setUp()
        self.install_fake_app()
        self.app = os.path.join(self.managed_dir, 'fakeapp.appimage')

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
        return code, out.getvalue(), err.getvalue()


class SingleFlightTests(LifecycleTestCase):
    def test_a_held_claim_refuses_the_synchronous_update(self):
        held = jobs.open_job_log_fd('fakeapp.desktop')
        self.addCleanup(os.close, held)
        with self.assertRaises(cli.OperationError) as caught:
            cli.run_update(self.app)
        self.assertIn('already running', str(caught.exception))
        self.assertIn('refusing to start a second one',
                      str(caught.exception))

    def test_the_module_releases_its_own_claim(self):
        # no update source is configured, so the body fails — but with its
        # own reason, never with a claim it forgot to release
        for _ in range(2):
            with self.assertRaises(cli.OperationError) as caught:
                cli.run_update(self.app)
            self.assertNotIn('already running', str(caught.exception))

    def test_the_background_path_refuses_the_same_way(self):
        held = jobs.open_job_log_fd('fakeapp.desktop')
        self.addCleanup(os.close, held)
        code, out, _err = self.run_cmd(
            cli.cmd_update, ['--async', self.app, '--json', '--yes'])
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn('refusing to start a second one', out)

    def test_an_unnameable_desktop_id_still_updates_synchronously(self):
        # a .desktop name with a space cannot name a job document; the
        # sync path therefore claims nothing and must fail for its own
        # reason instead of blaming a claim
        self.cleanup_and_install('my app', 'My App')
        path = os.path.join(self.managed_dir, 'myapp.appimage')
        with self.assertRaises(cli.OperationError) as caught:
            cli.run_update(path)
        self.assertNotIn('already running', str(caught.exception))

    def cleanup_and_install(self, desktop_name, app_name):
        self.install_fake_app(desktop_name=desktop_name, app_name=app_name)


class CrossProcessRaceTests(LifecycleTestCase):
    def test_a_job_in_another_process_blocks_the_terminal_update(self):
        # the real shape of the race: the grandchild holds the flock on
        # its inherited log description; a terminal --update in a
        # different process must be the one that is refused
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:                                    # pragma: no cover
            try:
                os.close(read_fd)
                _held = jobs.open_job_log_fd('fakeapp.desktop')
                os.write(write_fd, b'held')
                time.sleep(30)

            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            self.assertEqual(os.read(read_fd, 4), b'held')
            with self.assertRaises(cli.OperationError) as caught:
                cli.run_update(self.app)
            self.assertIn('already running', str(caught.exception))
        finally:
            os.close(read_fd)
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


class RemovalGuardTests(LifecycleTestCase):
    def test_removal_is_refused_while_that_app_is_being_updated(self):
        writer = jobs.JobWriter('fakeapp.desktop')
        self.addCleanup(writer.stop_heartbeat)
        writer.stage('downloading')
        code, _out, err = self.run_cmd(
            cli.cmd_remove, [self.app, '--yes', '--json'])
        self.assertEqual(code, 1)
        self.assertIn('refusing to remove it', err)
        self.assertTrue(os.path.exists(self.app))

    def test_removal_works_once_the_job_is_terminal(self):
        writer = jobs.JobWriter('fakeapp.desktop')
        writer.finish('updated', message='done')
        self.addCleanup(writer.stop_heartbeat)
        code, _out, _err = self.run_cmd(
            cli.cmd_remove, [self.app, '--yes', '--json'])
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.app))


if __name__ == '__main__':
    unittest.main()
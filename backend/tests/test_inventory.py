# test_inventory.py — issue #41: one listing pass answers every running
# question from ONE process-table and ONE mounts read, so the cost of a
# listing does not grow with the number of installed apps.

import json
import os
import unittest
from unittest import mock

from helpers import FakeXDGTestCase
from test_procs import FakeProbe

from omarchy_appimage import cli, procs


class CountingProbe(FakeProbe):
    """A FakeProbe that also counts mounts reads."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.mount_reads = 0

    def mounts(self):
        self.mount_reads += 1
        return super().mounts()


def _install_many(case, count):
    names = []
    for index in range(count):
        name = 'app%02d' % index
        case.install_fake_app(desktop_name=name, app_name='App %02d' % index)
        names.append(name)
    return names


class SingleScanTests(FakeXDGTestCase):
    def run_listing(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.cmd_list_installed(['--json'])
        self.assertEqual(code, 0, err.getvalue())
        return json.loads(out.getvalue())

    def test_twenty_apps_cost_one_process_table_and_one_mounts_read(self):
        _install_many(self, 20)
        probe = CountingProbe()
        with mock.patch.object(procs, '_PROBE', probe):
            document = self.run_listing()
        self.assertEqual(len(document['installed']), 20)
        self.assertEqual(probe.scans, 1)
        self.assertEqual(probe.mount_reads, 1)

    def test_every_row_answers_from_that_one_scan(self):
        names = _install_many(self, 3)
        running_path = os.path.join(self.managed_dir, names[1] + '.appimage')
        probe = CountingProbe(processes=[(4242, running_path)])
        with mock.patch.object(procs, '_PROBE', probe):
            document = self.run_listing()
        apps = {app['desktop_id']: app for app in document['installed']}
        self.assertIs(apps[names[1] + '.desktop']['running'], True)
        self.assertEqual(apps[names[1] + '.desktop']['running_pids'], 1)
        for other in (names[0], names[2]):
            self.assertIs(apps[other + '.desktop']['running'], False)
            self.assertEqual(apps[other + '.desktop']['running_pids'], 0)
        self.assertEqual(probe.scans, 1)
        self.assertEqual(probe.mount_reads, 1)

    def test_a_fuse_mount_still_counts_as_running_from_the_same_scan(self):
        names = _install_many(self, 1)
        appimage = os.path.join(self.managed_dir, names[0] + '.appimage')
        probe = CountingProbe(
            processes=[(77, '/tmp/.mount_app00abc/AppRun')],
            mounts=[(os.path.basename(appimage), '/tmp/.mount_app00abc',
                     'fuse.app00.appimage')])
        with mock.patch.object(procs, '_PROBE', probe):
            document = self.run_listing()
        app = document['installed'][0]
        self.assertIs(app['running'], True)
        # display-only count: a mount can outlive its processes
        self.assertEqual(app['running_pids'], 1)
        self.assertEqual(probe.scans, 1)
        self.assertEqual(probe.mount_reads, 1)


if __name__ == '__main__':
    unittest.main()
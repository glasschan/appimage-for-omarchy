# test_utils.py — unit tests for the shared helpers (utils.py), focusing
# on the running detection (ps exe + /proc/mounts FUSE mounts).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import sys
import tempfile
import unittest

from omarchy_appimage import utils

# Synthetic /proc/mounts content: two FUSE AppImage mounts (as produced
# by running type-2 AppImages, one with octal-escaped spaces in the
# source) and unrelated mounts that must never match.
SYNTHETIC_MOUNTS = '''\
/dev/sda2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
/home/user/AppImages/foo.appimage /tmp/.mount_fooXq8zW fuse.AppImage rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0
/home/user/AppImages/my\\040spaced\\040app.appimage /tmp/.mount_spacedAB12 fuse.AppImage rw,nosuid,nodev,relatime 0 0
/home/user/AppImages/plain.iso /mnt/iso iso9660 ro,relatime 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev 0 0
'''

FUSE_APP = '/home/user/AppImages/foo.appimage'
SPACED_APP = '/home/user/AppImages/my spaced app.appimage'


class MountsTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.mounts = tempfile.mkstemp(prefix='mounts-')
        with os.fdopen(fd, 'w') as f:
            f.write(SYNTHETIC_MOUNTS)
        self.addCleanup(os.unlink, self.mounts)


class ParseProcMountsTests(MountsTestCase):
    def test_parses_source_target_fstype(self):
        entries = utils.parse_proc_mounts(self.mounts)
        self.assertEqual(entries[0], ('/dev/sda2', '/', 'ext4'))
        self.assertIn((FUSE_APP, '/tmp/.mount_fooXq8zW', 'fuse.AppImage'),
                      entries)

    def test_octal_escapes_are_decoded(self):
        sources = [e[0] for e in utils.parse_proc_mounts(self.mounts)]
        self.assertIn(SPACED_APP, sources)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(utils.parse_proc_mounts('/no/such/mounts'), [])


class IsFuseMountedTests(MountsTestCase):
    def test_fuse_mounted_appimage_is_detected(self):
        self.assertTrue(utils.is_fuse_mounted(FUSE_APP, self.mounts))

    def test_escaped_source_is_detected(self):
        self.assertTrue(utils.is_fuse_mounted(SPACED_APP, self.mounts))

    def test_unmounted_appimage_is_not_running(self):
        self.assertFalse(
            utils.is_fuse_mounted('/home/user/AppImages/bar.appimage',
                                  self.mounts))

    def test_non_fuse_source_does_not_match(self):
        # same file as source, but not a fuse.* mount
        self.assertFalse(
            utils.is_fuse_mounted('/home/user/AppImages/plain.iso',
                                  self.mounts))

    def test_missing_mounts_file_is_false(self):
        self.assertFalse(
            utils.is_fuse_mounted(FUSE_APP, '/no/such/mounts'))

    def test_empty_path_is_false(self):
        self.assertFalse(utils.is_fuse_mounted('', self.mounts))


class IsAppRunningTests(MountsTestCase):
    """The two signals are OR-ed: ps exe match (original GearLever
    detection) + FUSE mount source match (new for type-2 AppImages)."""

    def test_fuse_mount_only_still_reports_running(self):
        # nothing matches in `ps -eo exe`, but the mount source does
        self.assertTrue(utils.is_app_running(FUSE_APP, self.mounts))

    def test_ps_match_only_still_reports_running(self):
        # the interpreter of this very test runner is found via ps,
        # with a mounts file that contains nothing relevant
        self.assertTrue(utils.is_app_running(
            os.path.realpath(sys.executable), '/no/such/mounts'))

    def test_no_signal_reports_not_running(self):
        self.assertFalse(utils.is_app_running(
            '/home/user/AppImages/bar.appimage', '/no/such/mounts'))

    def test_empty_path_is_false(self):
        self.assertFalse(utils.is_app_running('', self.mounts))


if __name__ == '__main__':
    unittest.main()

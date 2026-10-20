# test_utils.py — unit tests for the shared helpers (utils.py), focusing
# on the running detection (ps exe + /proc/mounts FUSE mounts) and the
# fuse2 runtime probe.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import subprocess
import sys
import tempfile
import time
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

# Synthetic `ldconfig -p` listings (the dynamic-linker cache probe's
# primary signal). Real shape: a summary line, then one line per cache
# entry as "<soname> (<abi>) => <path>".
FAKE_LDCONFIG_WITH_FUSE2 = '''\
1612 libs found in cache '/etc/ld.so.cache'
\tlibz.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libz.so.1
\tlibfuse.so.2 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libfuse.so.2
\tlibc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6
'''
FAKE_LDCONFIG_CLEAN = '''\
1611 libs found in cache '/etc/ld.so.cache'
\tlibz.so.1 (libc6,x86-64) => /lib/x86_64-linux-gnu/libz.so.1
\tlibc.so.6 (libc6,x86-64) => /lib/x86_64-linux-gnu/libc.so.6
'''
# near-miss sonames that must never count as libfuse.so.2
FAKE_LDCONFIG_DECOYS_ONLY = '''\
2 libs found in cache '/etc/ld.so.cache'
\tlibfuse3.so.3 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libfuse3.so.3
\tlibfuse.so.20 (libc6,x86-64) => /usr/lib/x86_64-linux-gnu/libfuse.so.20
'''


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


class Fuse2ProbeTests(unittest.TestCase):
    """The fuse2 runtime probe (utils.probe_fuse2): dynamic-linker cache
    first (faked via cache_output / a failing cache_command), then
    standard library directory globs (faked filesystem roots). Every
    outcome is one of present / missing / unknown — the probe NEVER
    raises and NEVER depends on the host actually having (or lacking)
    fuse2."""

    def setUp(self):
        # two faked library roots (one empty, one unused) plus a missing
        # directory, which the scan must skip like any absent system dir
        self.empty_dir = tempfile.mkdtemp(prefix='fuse2-empty-')
        self.fuse_dir = tempfile.mkdtemp(prefix='fuse2-libs-')
        self.lib_dirs = [self.fuse_dir, self.empty_dir, '/no/such/dir']
        self.addCleanup(os.rmdir, self.empty_dir)
        self.addCleanup(os.rmdir, self.fuse_dir)

    def _put_lib(self, name: str) -> str:
        path = os.path.join(self.fuse_dir, name)
        with open(path, 'w') as f:
            f.write('\x7fELF-fake-soname-body')
        self.addCleanup(os.unlink, path)
        return path

    # ------------------------------------------------- linker-cache phase

    def test_cache_listing_fuse2_is_present(self):
        self.assertEqual(
            utils.probe_fuse2(cache_output=FAKE_LDCONFIG_WITH_FUSE2,
                              lib_dirs=self.lib_dirs),
            'present')

    def test_cache_short_circuits_the_glob_scan(self):
        # the cache says present: no library directory is even scanned
        # (here every lib dir is missing entirely and it still reports
        # present)
        self.assertEqual(
            utils.probe_fuse2(cache_output=FAKE_LDCONFIG_WITH_FUSE2,
                              lib_dirs=['/no/such/dir']),
            'present')

    def test_decoy_sonames_do_not_match(self):
        # token equality: libfuse3.so.3 and libfuse.so.20 are NOT
        # libfuse.so.2; a clean verdict with no glob hit is 'missing'
        self.assertEqual(
            utils.probe_fuse2(cache_output=FAKE_LDCONFIG_DECOYS_ONLY,
                              lib_dirs=self.lib_dirs),
            'missing')

    # ---------------------------------------------- directory-glob phase

    def test_stale_cache_real_file_in_lib_dir_is_present(self):
        # cache ran clean but the glob finds the real file
        # (libfuse.so.2.9.9, the usual real-file name behind the
        # libfuse.so.2 symlink)
        self._put_lib('libfuse.so.2.9.9')
        self.assertEqual(
            utils.probe_fuse2(cache_output=FAKE_LDCONFIG_CLEAN,
                              lib_dirs=self.lib_dirs),
            'present')

    def test_unrelated_files_do_not_match_the_glob(self):
        self._put_lib('libfuse3.so.3')
        self.assertEqual(
            utils.probe_fuse2(cache_output=FAKE_LDCONFIG_CLEAN,
                              lib_dirs=self.lib_dirs),
            'missing')

    # ------------------------------------------------- failure -> unknown

    def test_missing_cache_binary_unknown_without_glob_hit(self):
        # the linker-cache phase fails (ldconfig not installed) and the
        # globs find nothing: the probe cannot decide -> unknown
        self.assertEqual(
            utils.probe_fuse2(
                cache_command=['omarchy-appimage-no-such-ldconfig', '-p'],
                lib_dirs=self.lib_dirs),
            'unknown')

    def test_failing_cache_command_unknown_without_glob_hit(self):
        # ldconfig runs but exits non-zero: same unknown verdict
        self.assertEqual(
            utils.probe_fuse2(
                cache_command=[sys.executable, '-c', 'import sys; sys.exit(1)'],
                lib_dirs=self.lib_dirs),
            'unknown')

    def test_failed_cache_still_reports_present_on_glob_hit(self):
        # a glob hit is decisive evidence on its own, even when the
        # cache phase failed entirely
        self._put_lib('libfuse.so.2')
        self.assertEqual(
            utils.probe_fuse2(
                cache_command=['omarchy-appimage-no-such-ldconfig', '-p'],
                lib_dirs=self.lib_dirs),
            'present')

    def test_missing_lib_dirs_are_skipped_not_unknown(self):
        # absent directories are the normal case (not every arch dir
        # exists): a clean cache over all-missing dirs is 'missing'
        self.assertEqual(
            utils.probe_fuse2(cache_output=FAKE_LDCONFIG_CLEAN,
                              lib_dirs=['/no/such/dir', '/no/either']),
            'missing')

    # ------------------------------------------------------- never raises

    def test_real_defaults_return_a_contract_state(self):
        # against the real host the probe must still answer with exactly
        # one of the three contract states (whatever the host has)
        self.assertIn(utils.probe_fuse2(),
                      ('present', 'missing', 'unknown'))

    def test_standard_dirs_cover_the_usual_roots(self):
        # spec: standard library directory globs — the multiarch dir,
        # lib64 and lib under the usual roots
        for expected in ('/usr/lib/x86_64-linux-gnu', '/lib64', '/lib',
                         '/usr/lib64', '/usr/lib'):
            self.assertIn(expected, utils.FUSE2_LIB_DIRS, expected)


class RunCommandOutputBoundTests(unittest.TestCase):
    """Producer-side output bounds: a chatty child can never make the
    backend buffer more than max_output_bytes per stream."""

    def test_small_output_unchanged(self):
        result = utils.run_command([sys.executable, '-c', 'print(1)'])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'1\n')

    def test_chatty_child_output_is_truncated_at_cap(self):
        # the child writes past the cap (30 KB > 1 KiB); the pipe keeps
        # being drained past the cap, the child exits cleanly and the
        # captured output is truncated to the cap, no hang
        code = 'import sys; sys.stdout.write("a" * 30000)'
        result = utils.run_command([sys.executable, '-c', code],
                                   check=False, max_output_bytes=1024)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), 1024)
        self.assertEqual(result.stdout, b'a' * 1024)

    def test_stderr_is_capped_too(self):
        code = 'import sys; sys.stderr.write("b" * 30000)'
        result = utils.run_command([sys.executable, '-c', code],
                                   check=False, max_output_bytes=1024)
        self.assertEqual(len(result.stderr), 1024)

    def test_endless_writer_is_reaped_by_timeout(self):
        # an unbounded producer never blocks on the pipe (it is always
        # drained past the cap), so the timeout is what reaps it, and
        # the retained output stays capped the whole time
        code = ('import sys\n'
                'while True:\n'
                '    sys.stdout.write("a" * 65536)')
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            utils.run_command([sys.executable, '-c', code],
                              timeout=2, max_output_bytes=1024)
        self.assertLess(time.monotonic() - start, 30)

    def test_called_process_error_contract_kept(self):
        code = 'import sys; print("boom", file=sys.stderr); sys.exit(3)'
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            utils.run_command([sys.executable, '-c', code])
        self.assertEqual(ctx.exception.returncode, 3)
        self.assertIn(b'boom', ctx.exception.stderr)

    def test_stdin_passthrough(self):
        fd, path = tempfile.mkstemp(prefix='stdin-')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(b'via-stdin')
            with open(path, 'rb') as f:
                result = utils.run_command(['cat'], stdin=f)
        finally:
            os.unlink(path)
        self.assertEqual(result.stdout, b'via-stdin')

    def test_timeout_killpg_kills_whole_group_and_raises(self):
        # a sleeping child plus a tiny timeout: TimeoutExpired with the
        # same shape callers see today — and the WHOLE process group dies.
        # The child (its own group leader via start_new_session) spawns a
        # grandchild on DEVNULL stdio (so it holds no pipe fds and cannot
        # stall the stream teardown) and records the pgid; run_command
        # kills via os.killpg, so after the timeout no group member may
        # survive. A regression to a plain proc.kill() would orphan the
        # grandchild and make the killpg(0) liveness probe below succeed
        # instead of raising.
        fd, pgid_file = tempfile.mkstemp(prefix='killpg-probe-')
        os.close(fd)
        self.addCleanup(os.unlink, pgid_file)
        code = (
            'import os, subprocess, sys, time\n'
            'gc = subprocess.Popen(\n'
            '    [sys.executable, "-c", "import time; time.sleep(10)"],\n'
            '    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n'
            '    stderr=subprocess.DEVNULL)\n'
            f'__import__("pathlib").Path({pgid_file!r}).write_text(str(os.getpgrp()))\n'
            'time.sleep(30)\n'
        )
        start = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            utils.run_command([sys.executable, '-c', code], timeout=2)
        self.assertLess(time.monotonic() - start, 30)

        # no orphan in the group: allow the kernel a moment to finish the
        # SIGKILL reap, then the group must be gone entirely
        time.sleep(0.3)
        with open(pgid_file) as f:
            pgid = int(f.read())
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)


if __name__ == '__main__':
    unittest.main()

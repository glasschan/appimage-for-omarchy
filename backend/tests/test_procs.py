# test_procs.py — process identity and the TERM->grace->KILL primitive
# (procs.py).
#
# Test suite written for this plugin. Every branch of the identity scan
# and of the quit walk is driven through the probe seam with a fake; the
# ONE exception is RealProcessTests, which copies /bin/sleep to a temp
# "AppImage" path, spawns it as its own child and is the only test in the
# suite allowed to signal anything — and only that child.
#
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from omarchy_appimage import procs, utils

APP = '/home/user/AppImages/foo.appimage'
MOUNT = '/tmp/.mount_fooXq8zW'
APP_MOUNTS = [('foo.appimage', MOUNT, 'fuse.foo.appimage')]

# Synthetic /proc/mounts as the real kernel writes it for a running
# type-2 AppImage: the source field is the AppImage's BASENAME (the
# absolute path never appears), the target is a /tmp/.mount_* directory
# and the fstype is fuse.<basename>.
BASENAME_MOUNTS = '''\
/dev/sda2 / ext4 rw,relatime 0 0
proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0
foo.appimage /tmp/.mount_fooXq8zW fuse.foo.appimage rw,nosuid,nodev,relatime,user_id=1000,group_id=1000 0 0
my\\040spaced\\040app.appimage /tmp/.mount_spacedAB12 fuse.AppImage rw,nosuid,nodev,relatime 0 0
plain.iso /mnt/iso iso9660 ro,relatime 0 0
'''

# Synthetic `ps -eo pid=,exe=` output: the header is suppressed but the
# pid column keeps its padding, the exe column is EMPTY where ps could
# not resolve it (kernel thread, another user, zombie) and `-` where ps
# prints its placeholder.
PS_OUTPUT = '''\
    1 /usr/lib/systemd/systemd
  441 /home/user/AppImages/foo.appimage
  442 /tmp/.mount_fooXq8zW/foo --some-arg
  443
  444 -
  xyz /not/a/pid
'''


class FakeProbe:
    """The test side of procs' probe seam.

    `processes` is the raw `ps` view (exe hint may be ''), `mounts` the
    /proc/mounts entries, `exes` the /proc/<pid>/exe readlink results,
    `states` /proc/<pid>/stat state chars and `reactions` how a pid
    answers a signal. Nothing here touches a real process.
    """

    def __init__(self, processes=(), mounts=(), exes=None, states=None,
                 reactions=None, unpermitted=(), gone=()):
        self._processes = list(processes)
        self._mounts = list(mounts)
        self.exes = dict(exes or {})
        self.states = dict(states or {})
        self.reactions = dict(reactions or {})   # 'survive-term' | ...
        self.unpermitted = set(unpermitted)
        self.dead = set(gone)
        self.sent = []            # [(pid, signo)] in call order
        self.exe_reads = []       # pids whose /proc exe was consulted
        self.scans = 0            # processes() calls
        self.now = 0.0
        self.slept = []

    # ----------------------------------------------------- probe protocol

    def processes(self):
        self.scans += 1
        return list(self._processes)

    def mounts(self):
        return list(self._mounts)

    def exe_for(self, pid):
        self.exe_reads.append(pid)
        if pid in self.exes:
            return self.exes[pid]
        # No explicit readlink result: /proc/<pid>/exe answers what `ps`
        # showed for the same pid (that is what makes the identity
        # re-check a no-op on an unchanged process).
        for proc_pid, exe in self._processes:
            if proc_pid == pid:
                return exe or None
        return None

    def state_for(self, pid):
        if pid in self.dead:
            return None
        return self.states.get(pid, 'S')

    def signal(self, pid, sig):
        self.sent.append((pid, sig))
        if pid in self.unpermitted:
            raise PermissionError(pid)
        if pid in self.dead:
            raise ProcessLookupError(pid)
        reaction = self.reactions.get(pid)
        if sig == signal.SIGTERM and reaction != 'survive-term' \
                and reaction != 'gone-on-kill':
            self.dead.add(pid)
        elif sig == signal.SIGKILL:
            self.dead.add(pid)
            if reaction == 'gone-on-kill':
                # exited on its own between the last poll and the KILL
                raise ProcessLookupError(pid)

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class MountsFileTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.mounts = tempfile.mkstemp(prefix='mounts-')
        with os.fdopen(fd, 'w') as f:
            f.write(BASENAME_MOUNTS)
        self.addCleanup(os.unlink, self.mounts)


class MountPointsForTests(MountsFileTestCase):
    """`mount_points_for` matches on the /proc/mounts SOURCE BASENAME —
    the field only ever holds the file name, so the old absolute-path
    comparison could never match on a real host."""

    def test_basename_source_matches_absolute_appimage_path(self):
        self.assertEqual(
            procs.mount_points_for(APP, self.mounts),
            ['/tmp/.mount_fooXq8zW'])

    def test_octal_escaped_basename_is_decoded_and_matches(self):
        self.assertEqual(
            procs.mount_points_for(
                '/home/user/AppImages/my spaced app.appimage', self.mounts),
            ['/tmp/.mount_spacedAB12'])

    def test_unmounted_appimage_has_no_mount_points(self):
        self.assertEqual(
            procs.mount_points_for('/home/user/AppImages/bar.appimage',
                                   self.mounts),
            [])

    def test_non_fuse_source_with_the_same_basename_never_matches(self):
        self.assertEqual(
            procs.mount_points_for('/home/user/AppImages/plain.iso',
                                   self.mounts),
            [])

    def test_missing_mounts_file_yields_nothing(self):
        self.assertEqual(procs.mount_points_for(APP, '/no/such/mounts'), [])

    def test_empty_path_yields_nothing(self):
        self.assertEqual(procs.mount_points_for('', self.mounts), [])


class SystemProbeMountsTests(MountsFileTestCase):
    """Finding 16: the real probe resolves /proc/mounts through
    utils.PROC_MOUNTS_PATH at CALL time, so patching that one name drives
    the whole path end to end — the way every CLI test does it."""

    def test_mounts_follow_the_patched_name(self):
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH', self.mounts):
            self.assertEqual(procs._SystemProbe().mounts(),
                             procs.parse_proc_mounts(self.mounts))

    def test_an_explicit_mounts_path_still_wins(self):
        with mock.patch.object(utils, 'PROC_MOUNTS_PATH',
                               '/no/such/mounts'):
            probe = procs._SystemProbe(mounts_path=self.mounts)
            self.assertEqual(probe.mounts(),
                             procs.parse_proc_mounts(self.mounts))


class ParsePsOutputTests(unittest.TestCase):
    """The `ps -eo pid=,exe=` parser: padded pids are kept, an empty or
    `-` exe column becomes '' (iter_processes then falls back to
    /proc/<pid>/exe), anything without an integer pid is skipped."""

    def test_pairs_are_parsed_with_padding_stripped(self):
        pairs = procs._parse_ps_exe_lines(PS_OUTPUT)
        self.assertEqual(pairs[:3], [
            (1, '/usr/lib/systemd/systemd'),
            (441, '/home/user/AppImages/foo.appimage'),
            (442, '/tmp/.mount_fooXq8zW/foo --some-arg'),
        ])

    def test_blank_and_placeholder_exe_are_normalised_to_empty(self):
        pairs = dict(procs._parse_ps_exe_lines(PS_OUTPUT))
        self.assertEqual(pairs[443], '')
        self.assertEqual(pairs[444], '')

    def test_malformed_line_is_skipped(self):
        pids = [pid for pid, _exe in procs._parse_ps_exe_lines(PS_OUTPUT)]
        self.assertNotIn('xyz', pids)
        self.assertEqual(len(pids), 5)


class IterProcessesTests(unittest.TestCase):
    """`iter_processes` yields (pid, exe) for entries it can attribute an
    exe to; an unattributable entry is skipped, never guessed at."""

    def test_ps_exe_is_yielded_without_reading_proc(self):
        probe = FakeProbe(processes=[(441, APP)])
        self.assertEqual(list(procs.iter_processes(probe=probe)),
                         [(441, APP)])
        self.assertEqual(probe.exe_reads, [])

    def test_blank_ps_exe_falls_back_to_proc_exe(self):
        probe = FakeProbe(processes=[(443, '')], exes={443: APP})
        self.assertEqual(list(procs.iter_processes(probe=probe)),
                         [(443, APP)])
        self.assertEqual(probe.exe_reads, [443])

    def test_unreadable_proc_exe_is_skipped(self):
        probe = FakeProbe(processes=[(443, ''), (441, APP)])
        self.assertEqual(list(procs.iter_processes(probe=probe)),
                         [(441, APP)])

    def test_missing_ps_binary_yields_nothing(self):
        probe = procs._SystemProbe(
            ps_command=['omarchy-appimage-no-such-ps', '-eo', 'pid=,exe='])
        self.assertEqual(list(procs.iter_processes(probe=probe)), [])


class PidsForTests(unittest.TestCase):
    """Strict exe identity: the AppImage path itself is the parent, an
    exe under one of ITS mount points is a child — nothing else."""

    def test_exe_equal_to_the_appimage_path_is_the_parent(self):
        probe = FakeProbe(processes=[(441, APP)])
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [441], 'children': []})

    def test_exe_under_the_mount_point_is_a_child(self):
        probe = FakeProbe(
            processes=[(442, MOUNT + '/foo'),
                       (445, MOUNT + '/usr/lib/helper')],
            mounts=APP_MOUNTS)
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [], 'children': [442, 445]})

    def test_sibling_directory_sharing_the_prefix_is_not_a_child(self):
        # /tmp/.mount_fooXq8zWextra is a DIFFERENT directory: the
        # separator is part of the match
        probe = FakeProbe(processes=[(446, MOUNT + 'extra/bin')],
                          mounts=APP_MOUNTS)
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [], 'children': []})

    def test_unrelated_process_with_the_app_name_is_never_matched(self):
        probe = FakeProbe(processes=[(447, '/usr/bin/foo'),
                                     (448, '/home/user/AppImages/foo')],
                          mounts=APP_MOUNTS)
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [], 'children': []})

    def test_process_under_another_appimages_mount_is_never_matched(self):
        probe = FakeProbe(
            processes=[(449, '/tmp/.mount_barZ9pQ/bar')],
            mounts=APP_MOUNTS)
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [], 'children': []})

    def test_stale_mount_entry_attributes_nothing(self):
        # the entry is still in /proc/mounts, the mount point is gone:
        # no exe can resolve under it, and the scan must not crash
        probe = FakeProbe(
            processes=[(441, APP), (450, '/usr/bin/unrelated')],
            mounts=[('foo.appimage', '/tmp/.mount_goneXYZ',
                     'fuse.foo.appimage')])
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [441], 'children': []})

    def test_unreadable_proc_exe_is_skipped(self):
        probe = FakeProbe(processes=[(451, '')], mounts=APP_MOUNTS)
        self.assertEqual(procs.pids_for(APP, probe=probe),
                         {'parent': [], 'children': []})

    def test_pids_are_sorted(self):
        probe = FakeProbe(processes=[(99, APP), (7, APP)])
        self.assertEqual(procs.pids_for(APP, probe=probe)['parent'], [7, 99])

    def test_empty_path_scans_nothing(self):
        probe = FakeProbe(processes=[(441, APP)])
        self.assertEqual(procs.pids_for('', probe=probe),
                         {'parent': [], 'children': []})
        self.assertEqual(probe.scans, 0)

    def test_symlinked_install_directory_matches_both_ways(self):
        # finding 7: ~/Applications -> ~/AppImages. `ps` and /proc can
        # render the exe either way round, so both sides of the parent
        # comparison are realpath-normalised
        root = tempfile.mkdtemp(prefix='omarchy-appimage-link-')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        real_dir = os.path.join(root, 'AppImages')
        os.makedirs(real_dir)
        real = os.path.join(real_dir, 'foo.appimage')
        with open(real, 'wb') as f:
            f.write(b'ELF')
        link = os.path.join(root, 'Applications')
        os.symlink(real_dir, link)
        via_link = os.path.join(link, 'foo.appimage')

        probe = FakeProbe(processes=[(460, via_link)])
        self.assertEqual(procs.pids_for(real, probe=probe)['parent'], [460])
        probe = FakeProbe(processes=[(461, real)])
        self.assertEqual(procs.pids_for(via_link, probe=probe)['parent'],
                         [461])


class CachedProbeTests(unittest.TestCase):
    """Finding 8: a listing wraps the probe once, so every row of the
    document shares ONE `ps` table and ONE /proc/mounts read."""

    def test_process_table_is_read_once_for_many_lookups(self):
        probe = FakeProbe(processes=[(441, APP), (442, MOUNT + '/foo')],
                          mounts=APP_MOUNTS)
        scan = procs._cached_probe(probe)
        self.assertEqual(procs.pids_for(APP, probe=scan)['parent'], [441])
        self.assertEqual(procs.pids_for(APP, probe=scan)['children'], [442])
        self.assertEqual(probe.scans, 1)

    def test_the_wrapper_answers_the_rest_of_the_protocol(self):
        probe = FakeProbe(processes=[(441, APP)], mounts=APP_MOUNTS)
        scan = procs._cached_probe(probe)
        self.assertEqual(scan.exe_for(441), APP)
        self.assertEqual(scan.state_for(441), 'S')
        scan.signal(441, signal.SIGTERM)
        self.assertIn((441, signal.SIGTERM), probe.sent)
        self.assertIsNone(scan.state_for(441))      # now dead to the fake


class QuitAppTests(unittest.TestCase):
    """TERM the whole set in one round, grace-poll the survivors, KILL
    only those, and report which outcome each pid had."""

    def _probe(self, **kwargs):
        kwargs.setdefault('mounts', APP_MOUNTS)
        kwargs.setdefault('processes', [(441, APP), (442, MOUNT + '/foo')])
        return FakeProbe(**kwargs)

    def test_terms_parent_and_children_in_the_same_round(self):
        probe = self._probe()
        report = procs.quit_app(APP, term_grace_s=2.0, probe=probe)

        self.assertEqual(set(report),
                         {'signalled', 'killed_with_sigkill', 'outcomes',
                          'survived'})
        self.assertEqual(report['signalled'], [441, 442])
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['outcomes'],
                         {441: 'terminated', 442: 'terminated'})
        self.assertEqual(report['survived'], [])
        self.assertNotIn(signal.SIGKILL,
                         [sig for _pid, sig in probe.sent])
        # everything died on TERM: the grace was never consumed
        self.assertEqual(probe.slept, [])

    def test_survivor_is_killed_after_the_grace(self):
        probe = self._probe(reactions={442: 'survive-term'})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)

        self.assertEqual(report['signalled'], [441, 442])
        self.assertEqual(report['killed_with_sigkill'], [442])
        self.assertEqual(report['outcomes'],
                         {441: 'terminated', 442: 'sigkilled'})
        self.assertIn((442, signal.SIGKILL), probe.sent)
        # the whole grace was spent polling, and no more (a float-sum
        # tolerance: the per-slice sleeps accumulate tiny error)
        self.assertAlmostEqual(sum(probe.slept), 1.0, places=6)
        self.assertLessEqual(sum(probe.slept), 1.05)

    def test_zero_grace_kills_survivors_without_waiting(self):
        probe = self._probe(reactions={442: 'survive-term'})
        report = procs.quit_app(APP, term_grace_s=0, probe=probe)
        self.assertEqual(report['killed_with_sigkill'], [442])
        self.assertEqual(probe.slept, [])

    def test_zombie_counts_as_gone_not_as_a_kill(self):
        # a zombie still answers signal 0 but has already exited; it must
        # never be reported as needing SIGKILL
        probe = self._probe(reactions={442: 'survive-term'},
                            states={442: 'Z'})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['outcomes'][442], 'terminated')

    def test_pid_gone_before_the_term_is_reported_not_signalled(self):
        probe = self._probe(gone={442})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['signalled'], [441])
        self.assertNotIn(442, report['outcomes'])
        self.assertEqual(probe.slept, [])

    def test_unpermitted_pid_is_reported_and_never_escalated(self):
        # another user's process (same file name, different owner) is
        # not ours to kill
        probe = self._probe(unpermitted={442})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['signalled'], [441])
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['outcomes'][442], 'not-permitted')
        self.assertNotIn((442, signal.SIGKILL), probe.sent)

    def test_pid_exiting_between_term_and_kill_stays_terminated(self):
        probe = self._probe(reactions={442: 'gone-on-kill'})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['outcomes'][442], 'terminated')

    def test_nothing_running_is_a_noop(self):
        probe = FakeProbe(processes=[(450, '/usr/bin/unrelated')],
                          mounts=APP_MOUNTS)
        report = procs.quit_app(APP, probe=probe)
        self.assertEqual(report['signalled'], [])
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['outcomes'], {})
        self.assertEqual(probe.sent, [])

    def test_empty_path_is_a_noop(self):
        probe = FakeProbe(processes=[(441, APP)])
        report = procs.quit_app('', probe=probe)
        self.assertEqual(report['signalled'], [])
        self.assertEqual(probe.sent, [])

    # ------------------------------------- finding E / SHOULD-FIX 2 and 3

    def test_recycled_pid_is_dropped_before_the_term(self):
        # the scan captured 441, but by the time the round runs 441 is
        # somebody else's process: never signal it, and say so
        probe = self._probe(exes={441: '/usr/bin/impostor'})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['outcomes'][441], 'identity-changed')
        self.assertNotIn((441, signal.SIGTERM), probe.sent)
        self.assertEqual(report['signalled'], [442])
        # an identity change is not a survivor: nothing of ours is left
        self.assertEqual(report['survived'], [])

    def test_child_whose_exe_changed_between_rounds_is_not_killed(self):
        class _FlippingProbe(FakeProbe):
            """442's /proc exe reads fine for the TERM round, then
            resolves to an unrelated program before the KILL round."""

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.flipped = False

            def exe_for(self, pid):
                self.exe_reads.append(pid)
                if pid == 442 and self.flipped:
                    return '/usr/bin/impostor'
                if pid == 442:
                    self.flipped = True
                return super().exe_for(pid)

        probe = _FlippingProbe(processes=[(441, APP), (442, MOUNT + '/foo')],
                               mounts=APP_MOUNTS,
                               reactions={442: 'survive-term'})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertIn((442, signal.SIGTERM), probe.sent)
        self.assertNotIn((442, signal.SIGKILL), probe.sent)
        self.assertEqual(report['outcomes'][442], 'identity-changed')
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['survived'], [])

    def test_unreadable_exe_during_recheck_is_never_signalled(self):
        # the pid vanished between the scan and the round: nothing to
        # prove membership with, so nothing is signalled
        probe = self._probe(exes={441: None, 442: None})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['signalled'], [])
        self.assertEqual(probe.sent, [])
        self.assertEqual(report['outcomes'],
                         {441: 'identity-changed', 442: 'identity-changed'})

    def test_survivors_are_listed_for_the_caller(self):
        # finding F / 12: the caller has to refuse the replace, so the
        # report has to name what is still alive
        probe = self._probe(unpermitted={442})
        report = procs.quit_app(APP, term_grace_s=1.0, probe=probe)
        self.assertEqual(report['survived'], [442])

    # ------------------------------------------- finding 10 / J(NITPICK)

    def test_generic_oserror_counts_as_alive(self):
        # only ProcessLookupError proves the pid is gone; a generic OSError
        # (EIO from /proc, a wedged kernel call) must not be read as death,
        # or the KILL round silently drops a live process
        class _Odd(FakeProbe):
            def signal(self, pid, sig):
                super().signal(pid, sig)
                if sig == 0:
                    raise OSError('EIO')

        probe = _Odd(processes=[(441, APP)], mounts=APP_MOUNTS)
        self.assertTrue(procs._is_alive(probe, 441))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RealProcessTests(unittest.TestCase):
    """The one test allowed to signal anything: a child of this test
    process (a copy of /bin/sleep at a temp path, so its exe IS that
    path) is detected and then quit. Nothing else is ever signalled."""

    def setUp(self):
        self.tmpdir = os.path.realpath(
            tempfile.mkdtemp(prefix='omarchy-appimage-proc-'))
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.appimage = os.path.join(self.tmpdir, 'probe-test.appimage')
        shutil.copy2('/bin/sleep', self.appimage)
        os.chmod(self.appimage, 0o755)

        try:
            # argv[0] is the applet name, never the temp path: the CI
            # runner ships uutils coreutils, whose /bin/sleep is a
            # multicall binary that picks its applet from the basename of
            # argv[0] (busybox and GNU coreutils' single binaries do the
            # same), so a copy launched as `probe-test.appimage` printed
            # an applet error and exited 1 within 20 ms — after the kernel
            # had already recorded the temp path in /proc/<pid>/exe, which
            # is the only thing the identity scan reads. Naming the applet
            # keeps the fixture alive on every coreutils flavour.
            self.child = subprocess.Popen(['sleep', '60'],
                                          executable=self.appimage,
                                          stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
        except PermissionError:
            # a noexec /tmp: this test needs an executable temp path
            self.skipTest('temp dir is mounted noexec')
        self.addCleanup(self._reap_child)

        # exec is asynchronous: wait until /proc/<pid>/exe is the temp
        # path before asserting anything about identity
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if os.readlink(f'/proc/{self.child.pid}/exe') \
                        == self.appimage:
                    break
            except OSError:
                pass
            if self.child.poll() is not None:
                self.fail('child exited before detection')
            time.sleep(0.01)
        else:
            self.fail('child never exec\'d the temp path')

        # the scan below must observe a live process: a fixture that dies
        # just after exec is a broken test environment, not a detection
        # bug, and must say so instead of surfacing as an empty scan
        time.sleep(0.05)
        if self.child.poll() is not None:
            self.fail('fixture exited right after exec '
                      f'(rc={self.child.returncode})')

    def _reap_child(self):
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(timeout=10)

    def test_detects_and_quits_its_own_child(self):
        found = procs.pids_for(self.appimage)
        self.assertIn(self.child.pid, found['parent'])
        self.assertEqual(found['children'], [])

        report = procs.quit_app(self.appimage, term_grace_s=10)

        self.assertIn(self.child.pid, report['signalled'])
        self.assertEqual(report['killed_with_sigkill'], [])
        self.assertEqual(report['outcomes'][self.child.pid], 'terminated')

        self.child.wait(timeout=10)      # reaped: it is really gone
        self.assertFalse(_pid_alive(self.child.pid))


if __name__ == '__main__':
    unittest.main()
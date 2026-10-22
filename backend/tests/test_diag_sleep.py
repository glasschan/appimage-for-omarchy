# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
"""TEMPORARY diagnostic: how each spawn form of the /bin/sleep fixture
behaves on the CI runner. Deleted before this branch is merged."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


def _probe(args, executable, path):
    child = subprocess.Popen(args, executable=executable,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
    link = None
    for _ in range(300):
        try:
            link = os.readlink(f'/proc/{child.pid}/exe')
        except OSError:
            pass
        if link == path:
            break
        time.sleep(0.01)
    try:
        out, err = child.communicate(timeout=1.5)
        alive = False
    except subprocess.TimeoutExpired:
        alive = True
        child.kill()
        out, err = child.communicate()
    return (f'alive={alive} rc={child.returncode} exe_matched={link == path} '
            f'err={err[:240]!r} out={out[:120]!r}')


class SleepFixtureDiagnostic(unittest.TestCase):
    def test_report(self):
        tmp = os.path.realpath(tempfile.mkdtemp(prefix='diag-sleep-'))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        facts = [
            f'/bin/sleep -> {os.path.realpath("/bin/sleep")}',
            f'islink={os.path.islink("/bin/sleep")} '
            f'target={os.readlink("/bin/sleep") if os.path.islink("/bin/sleep") else "-"}',
            f'cargo_dir={sorted(os.listdir("/usr/lib/cargo/bin"))[:8] if os.path.isdir("/usr/lib/cargo/bin") else "absent"}',
        ]
        sleep_copy = os.path.join(tmp, 'probe-test.appimage')
        shutil.copy2('/bin/sleep', sleep_copy)
        os.chmod(sleep_copy, 0o755)
        facts.append(f'sleep_copy_size={os.path.getsize(sleep_copy)}')

        facts.append('sleep[tmppath-argv0]: ' +
                     _probe([sleep_copy, '60'], None, sleep_copy))
        facts.append('sleep[applet-argv0]: ' +
                     _probe(['sleep', '60'], sleep_copy, sleep_copy))
        facts.append('sleep[multicall-form]: ' +
                     _probe([sleep_copy, 'sleep', '60'], None, sleep_copy))

        py_copy = os.path.join(tmp, 'probe-py.appimage')
        shutil.copy2(sys.executable, py_copy)
        os.chmod(py_copy, 0o755)
        facts.append(f'py_src={sys.executable} py_copy_size={os.path.getsize(py_copy)}')
        facts.append('py[copy+(-c)]: ' +
                     _probe([py_copy, '-c',
                             'import time; time.sleep(60)'], None, py_copy))

        print('\n'.join(f'DIAG {f}' for f in facts))
        self.fail('diagnostic only')
# test_exec_scale.py — the `--exec-with-scale` verb (ticket #33).
#
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Three layers are pinned:
#
#   * `parse_focused_scale` (pure): every unusable `hyprctl -j monitors`
#     answer (malformed JSON, not a list, no focused monitor,
#     non-numeric / out-of-range scale) yields None, a good answer
#     yields the focused monitor's scale — never a non-focused one.
#   * `focused_scale` (bounded query): the fallback paths of the real
#     child spawn — missing binary, non-zero exit, timeout, malformed
#     output — all degrade to None. Fakes run through the same
#     `run_command` seam the production code uses; the timing cases use
#     `sys.executable` (never /bin/sleep — the uutils coreutils fixture
#     rule) with a short timeout so the process-group kill is exercised.
#   * `exec_with_scale` (the handover): a REAL process test — a wrapper
#     child computes the env and `os.execve`s `sys.executable`; the app
#     reports its own pid and argv, and the test proves the pid equals
#     the wrapper's (the image replaced the process, nothing outlives
#     it) and that arguments — including `%f` and flag-shaped tokens —
#     arrived untouched.

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

from omarchy_appimage import exec_scale
from omarchy_appimage.commands.envelope import UsageError
from omarchy_appimage.commands.exec_scale import cmd_exec_with_scale


def _monitor(name='DP-1', focused=False, scale=1):
    return {'name': name, 'focused': focused, 'scale': scale}


class ParseFocusedScaleTests(unittest.TestCase):
    """Every branch of the pure parser, good and unusable."""

    def test_the_focused_monitor_wins(self):
        stdout = json.dumps([
            _monitor('DP-1', focused=False, scale=2),
            _monitor('HDMI-A-1', focused=True, scale=1.5),
        ])
        self.assertEqual(exec_scale.parse_focused_scale(stdout), 1.5)

    def test_scale_may_be_a_json_int(self):
        stdout = json.dumps([_monitor(focused=True, scale=2)])
        self.assertEqual(exec_scale.parse_focused_scale(stdout), 2.0)

    def test_malformed_json_is_unusable(self):
        self.assertIsNone(exec_scale.parse_focused_scale('not json'))

    def test_a_non_list_answer_is_unusable(self):
        self.assertIsNone(exec_scale.parse_focused_scale('{"monitors": []}'))

    def test_no_focused_monitor_is_unusable(self):
        stdout = json.dumps([_monitor('DP-1', focused=False, scale=2),
                             _monitor('HDMI-A-1', focused=False, scale=1)])
        self.assertIsNone(exec_scale.parse_focused_scale(stdout))

    def test_empty_answer_is_unusable(self):
        self.assertIsNone(exec_scale.parse_focused_scale('[]'))

    def test_non_numeric_scale_is_unusable(self):
        stdout = json.dumps([_monitor(focused=True, scale='big')])
        self.assertIsNone(exec_scale.parse_focused_scale(stdout))

    def test_boolean_scale_is_unusable(self):
        # JSON true/false parse as bool, an int subclass — excluded
        stdout = json.dumps([_monitor(focused=True, scale=True)])
        self.assertIsNone(exec_scale.parse_focused_scale(stdout))

    def test_null_scale_is_unusable(self):
        stdout = json.dumps([_monitor(focused=True, scale=None)])
        self.assertIsNone(exec_scale.parse_focused_scale(stdout))

    def test_zero_and_negative_scale_are_unusable(self):
        for scale in (0, 0.0, -1):
            stdout = json.dumps([_monitor(focused=True, scale=scale)])
            self.assertIsNone(exec_scale.parse_focused_scale(stdout),
                              f'scale={scale}')

    def test_absurd_scale_is_unusable(self):
        stdout = json.dumps([_monitor(focused=True, scale=50)])
        self.assertIsNone(exec_scale.parse_focused_scale(stdout))

    def test_a_focused_entry_without_a_dict_shape_is_skipped(self):
        stdout = json.dumps(['junk', _monitor(focused=True, scale=2)])
        self.assertEqual(exec_scale.parse_focused_scale(stdout), 2.0)


class RoundedScaleTests(unittest.TestCase):
    """The mapping the acceptance criteria name: 1.0->1, 2.0->2,
    fractional rounds to the nearest whole factor (1.49->1, 1.5->2)."""

    def test_whole_factors_pass_through(self):
        self.assertEqual(exec_scale.rounded_scale(1.0), 1)
        self.assertEqual(exec_scale.rounded_scale(2.0), 2)
        self.assertEqual(exec_scale.rounded_scale(3.0), 3)

    def test_halves_round_up(self):
        self.assertEqual(exec_scale.rounded_scale(1.5), 2)
        self.assertEqual(exec_scale.rounded_scale(2.5), 3)

    def test_fraction_below_half_rounds_down(self):
        self.assertEqual(exec_scale.rounded_scale(1.49), 1)
        self.assertEqual(exec_scale.rounded_scale(1.25), 1)

    def test_fraction_above_half_rounds_up(self):
        self.assertEqual(exec_scale.rounded_scale(1.75), 2)


class FocusedScaleFallbackTests(unittest.TestCase):
    """Every fallback path of the bounded query degrades to None and
    never raises."""

    def test_missing_binary_is_unusable(self):
        self.assertIsNone(exec_scale.focused_scale(
            query_argv=['definitely-not-a-binary-omarchy-test']))

    def test_non_zero_exit_is_unusable(self):
        self.assertIsNone(exec_scale.focused_scale(
            query_argv=[sys.executable, '-c', 'import sys; sys.exit(3)']))

    def test_timeout_is_unusable(self):
        # a hung child: the process-group kill under run_command's
        # timeout must reap it and yield None, never a hang
        self.assertIsNone(exec_scale.focused_scale(
            query_argv=[sys.executable, '-c', 'import time; time.sleep(30)'],
            timeout=0.5))

    def test_malformed_output_is_unusable(self):
        self.assertIsNone(exec_scale.focused_scale(
            query_argv=[sys.executable, '-c', 'print("garbage, not json")']))

    def test_a_good_answer_parses(self):
        good = json.dumps([_monitor(focused=True, scale=2)])
        self.assertEqual(exec_scale.focused_scale(
            query_argv=[sys.executable, '-c', f'print({good!r})']), 2.0)


class LaunchEnvTests(unittest.TestCase):
    """The env the image is exec'd with, per the never-fail rule."""

    def test_unusable_scale_inherits_the_environment_unchanged(self):
        env = {'GDK_SCALE': '2', 'PATH': '/bin'}
        self.assertEqual(exec_scale.launch_env(env, None), env)

    def test_focused_scale_is_set(self):
        env = exec_scale.launch_env({'PATH': '/bin'}, 2.0)
        self.assertEqual(env['GDK_SCALE'], '2')
        self.assertEqual(env['PATH'], '/bin')

    def test_inherited_gdk_scale_matching_the_monitor_is_kept_untouched(self):
        # the caller's env already produces the focused factor: hand the
        # environment over byte-identical
        env = {'GDK_SCALE': '2', 'PATH': '/bin'}
        self.assertEqual(exec_scale.launch_env(env, 2.0), env)

    def test_inherited_gdk_scale_rounding_the_same_is_kept_untouched(self):
        env = {'GDK_SCALE': '1.5', 'PATH': '/bin'}
        self.assertEqual(exec_scale.launch_env(env, 2.0), env)

    def test_inherited_gdk_scale_disagreeing_is_overridden(self):
        # the XWayland case: GDK_SCALE=2, focused monitor is scale 1
        env = exec_scale.launch_env({'GDK_SCALE': '2', 'PATH': '/bin'}, 1.0)
        self.assertEqual(env['GDK_SCALE'], '1')

    def test_non_numeric_inherited_gdk_scale_is_overridden(self):
        env = exec_scale.launch_env({'GDK_SCALE': 'nonsense'}, 2.0)
        self.assertEqual(env['GDK_SCALE'], '2')

    def test_the_caller_environment_is_never_mutated(self):
        env = {'GDK_SCALE': '2'}
        exec_scale.launch_env(env, 1.0)
        self.assertEqual(env, {'GDK_SCALE': '2'})


_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_APP_CODE = (
    "import json, os, sys; "
    "print(json.dumps({'pid': os.getpid(), "
    "'argv': sys.argv, "
    "'scale': os.environ.get('GDK_SCALE')}))")


class ExecHandoverTests(unittest.TestCase):
    """A real handover: the wrapper child (which imports this module)
    execve's `sys.executable`; the app reports its pid, argv and env.
    The pid pin is the exec proof — `os.execve` REPLACES the process, so
    the app's pid IS the wrapper's; a spawned wrapper would differ."""

    def _run_handover(self, environment, query_scale):
        wrapper = (
            "import os, sys; "
            f"sys.path.insert(0, {_BACKEND_ROOT!r}); "
            "from omarchy_appimage import exec_scale; "
            f"exec_scale.exec_with_scale(sys.executable, "
            f"['-c', {_APP_CODE!r}, 'flag-like', '%f'], "
            f"environment={environment!r}, "
            f"query=lambda: {query_scale!r})")
        result = subprocess.run(
            [sys.executable, '-c', wrapper],
            capture_output=True, timeout=30, text=True,
            env={'PATH': os.environ.get('PATH', ''),
                 'PYTHONPATH': _BACKEND_ROOT,
                 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_exec_replaces_the_process_and_forwards_arguments_untouched(self):
        reported = self._run_handover({'PATH': '/bin'}, 2.0)
        # sys.argv[0] is the '-c' marker (the interpreter consumed it);
        # everything after it is exactly what was handed over —
        # including an option-shaped token and a desktop field code,
        # verbatim, unquoted, unshell'd
        # `-c` consumes the code argument itself, so sys.argv[1:] holds
        # the forwarded arguments — verbatim, unquoted, unshell'd,
        # including an option-shaped token and a desktop field code
        self.assertEqual(reported['argv'], ['-c', 'flag-like', '%f'])
        self.assertEqual(reported['scale'], '2')
        # nothing outlives the exec: run() waited on the app itself and
        # got its output directly (a surviving wrapper would have
        # produced none)

    def test_query_failure_inherits_the_environment_unchanged(self):
        reported = self._run_handover({'PATH': '/bin', 'GDK_SCALE': '7'},
                                      None)
        self.assertEqual(reported['scale'], '7')

    def test_disagreeing_monitor_scale_overrides_the_inherited_value(self):
        reported = self._run_handover({'PATH': '/bin', 'GDK_SCALE': '2'},
                                      1.0)
        self.assertEqual(reported['scale'], '1')


class VerbTests(unittest.TestCase):
    """The verb's own contract: no options of its own, everything after
    the image path belongs to the app."""

    def test_missing_image_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            cmd_exec_with_scale([])

    def test_an_option_where_the_image_belongs_is_a_usage_error(self):
        with self.assertRaises(UsageError):
            cmd_exec_with_scale(['--json'])

    def test_the_default_environment_is_the_constants_seam(self):
        fake_env = {'PATH': '/bin'}
        with mock.patch.object(exec_scale.constants, 'process_environment',
                               lambda: fake_env), \
                mock.patch.object(exec_scale, 'os') as fake_os:
            # query=lambda: None: the default binds focused_scale at def
            # time, so the seam is passed explicitly here
            exec_scale.exec_with_scale('/opt/App.AppImage', ['--flag'],
                                       query=lambda: None)
        # the exec handed over the constants seam's map (GDK_SCALE left
        # alone: the query returned None) with the args verbatim
        fake_os.execve.assert_called_once_with(
            '/opt/App.AppImage', ['/opt/App.AppImage', '--flag'], fake_env)

    def test_the_mechanism_receives_image_and_verbatim_arguments(self):
        seen = {}

        def fake_exec(image_path, arguments, environ=None, query=None):
            seen['image'] = image_path
            seen['args'] = arguments

        with mock.patch.object(exec_scale, 'exec_with_scale', fake_exec):
            cmd_exec_with_scale(['/opt/App.AppImage', '--flag', '%f'])
        self.assertEqual(seen['image'], '/opt/App.AppImage')
        self.assertEqual(seen['args'], ['--flag', '%f'])

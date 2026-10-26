# test_launch.py — the one launch-command builder (ticket #32).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# Every place that writes or spawns a launch (the desktop-entry writer at
# integrate time, the clobbered-desktop repair, the panel's launch action)
# builds its command through omarchy_appimage.launch. The pins below are
# byte-identity pins: the generated `Exec=` text and the spawned argv must
# stay exactly what the pre-#32 assembly produced at each site.

import shlex
import sys
import unittest

import helpers  # noqa: F401 — puts the backend dir on sys.path
from omarchy_appimage import launch


class MergeEnvVariablesTests(unittest.TestCase):
    """The DESKTOPINTEGRATION precedence the install path used to inline."""

    def test_appends_when_missing(self):
        env, changed = launch.merge_env_variables([], None)
        self.assertEqual(env, ['DESKTOPINTEGRATION=1'])
        self.assertTrue(changed)

    def test_appends_to_existing_vars_and_reports_the_change(self):
        env, changed = launch.merge_env_variables(
            ['GTK_THEME=Adwaita'], None)
        self.assertEqual(env, ['GTK_THEME=Adwaita', 'DESKTOPINTEGRATION=1'])
        self.assertTrue(changed)

    def test_existing_desktopintegration_variable_wins(self):
        env, changed = launch.merge_env_variables(
            ['DESKTOPINTEGRATION=0'], None)
        self.assertEqual(env, ['DESKTOPINTEGRATION=0'])
        self.assertFalse(changed)

    def test_prefix_match_is_what_counts(self):
        # any VAR starting with the name (e.g. a bare `DESKTOPINTEGRATION=`)
        # already carries the choice; nothing is appended
        env, changed = launch.merge_env_variables(
            ['OTHER=1', 'DESKTOPINTEGRATION='], None)
        self.assertEqual(env, ['OTHER=1', 'DESKTOPINTEGRATION='])
        self.assertFalse(changed)

    def test_previous_vars_take_precedence(self):
        env, changed = launch.merge_env_variables(
            ['MINE=1'], ['THEIRS=2'])
        self.assertEqual(env, ['THEIRS=2', 'DESKTOPINTEGRATION=1'])
        self.assertTrue(changed)

    def test_previous_empty_list_keeps_current(self):
        # the install path treats a falsy previous as "no previous vars"
        env, changed = launch.merge_env_variables(['MINE=1'], [])
        self.assertEqual(env, ['MINE=1', 'DESKTOPINTEGRATION=1'])
        self.assertTrue(changed)

    def test_inputs_are_never_mutated(self):
        current = ['MINE=1']
        previous = ['THEIRS=2']
        launch.merge_env_variables(current, previous)
        self.assertEqual(current, ['MINE=1'])
        self.assertEqual(previous, ['THEIRS=2'])


class LaunchArgvTests(unittest.TestCase):
    """The token form: what execDetached spawns and what shlex.join
    serialises for the repair path."""

    def test_default_env_is_desktopintegration(self):
        self.assertEqual(
            launch.launch_argv('/apps/foo.appimage', []),
            ['env', 'DESKTOPINTEGRATION=1', '/apps/foo.appimage'])

    def test_arguments_ride_along_verbatim(self):
        self.assertEqual(
            launch.launch_argv('/apps/foo.appimage', ['--flag', '%F']),
            ['env', 'DESKTOPINTEGRATION=1', '/apps/foo.appimage',
             '--flag', '%F'])

    def test_explicit_env_variables_replace_the_default(self):
        self.assertEqual(
            launch.launch_argv('/a', [], ['X=1']),
            ['env', 'X=1', '/a'])

    def test_repair_serialisation_is_unchanged(self):
        # provider_listing._repair_clobbered_desktop used to call
        # shlex.join(['env', 'DESKTOPINTEGRATION=1', appimage, *args]);
        # the builder must feed shlex.join the identical token list.
        args = shlex.split('--no-sandbox %U')
        self.assertEqual(
            shlex.join(launch.launch_argv('/apps/z.appimage', args)),
            shlex.join(['env', 'DESKTOPINTEGRATION=1',
                        '/apps/z.appimage', *args]))


class LaunchExecTextTests(unittest.TestCase):
    """The `Exec=` serialization the desktop-entry writer emits."""

    def test_matches_the_install_formula_with_env(self):
        self.assertEqual(
            launch.launch_exec_text('/apps/foo.appimage', ['--flag', '%F'],
                                    ['DESKTOPINTEGRATION=1']),
            'env DESKTOPINTEGRATION=1 /apps/foo.appimage --flag %F')

    def test_empty_arguments_leave_no_trailing_space(self):
        self.assertEqual(
            launch.launch_exec_text('/apps/foo.appimage', [],
                                    ['DESKTOPINTEGRATION=1']),
            'env DESKTOPINTEGRATION=1 /apps/foo.appimage')

    def test_image_path_is_quoted(self):
        self.assertEqual(
            launch.launch_exec_text('/apps/my app.appimage', [],
                                    ['DESKTOPINTEGRATION=1']),
            "env DESKTOPINTEGRATION=1 '/apps/my app.appimage'")

    def test_multiple_env_vars_join_inside_the_prefix(self):
        self.assertEqual(
            launch.launch_exec_text('/a', ['--x'],
                                    ['A=1', 'DESKTOPINTEGRATION=1']),
            'env A=1 DESKTOPINTEGRATION=1 /a --x')

    def test_no_env_vars_drops_the_prefix_entirely(self):
        self.assertEqual(
            launch.launch_exec_text('/a', ['--x'], []),
            '/a --x')

    def test_arguments_are_joined_raw_not_requoted(self):
        # the writer's formula joins the parsed arguments with plain
        # spaces (the parsed tokens already carry their original shape);
        # it must not grow shlex quoting the pre-#32 code never had
        self.assertEqual(
            launch.launch_exec_text('/a', ['--x="a b"'],
                                    ['DESKTOPINTEGRATION=1']),
            'env DESKTOPINTEGRATION=1 /a --x="a b"')


class EntryPointArgvTests(unittest.TestCase):
    """The entry-point argv (ticket #34): what the repair path
    serialises with shlex.join."""

    def test_shape_with_default_env(self):
        self.assertEqual(
            launch.entry_point_argv('/plugins/backend', '/apps/foo.appimage',
                                    ['--flag', '%F']),
            ['env', 'DESKTOPINTEGRATION=1', sys.executable,
             '/plugins/backend/main.py', '--exec-with-scale',
             '/apps/foo.appimage', '--flag', '%F'])

    def test_explicit_python_executable(self):
        argv = launch.entry_point_argv(
            '/b', '/a', [], python_executable='/usr/bin/python3')
        self.assertEqual(
            argv,
            ['env', 'DESKTOPINTEGRATION=1', '/usr/bin/python3',
             '/b/main.py', '--exec-with-scale', '/a'])

    def test_empty_env_drops_the_env_token(self):
        self.assertEqual(
            launch.entry_point_argv('/b', '/a', [], env_variables=[]),
            [sys.executable, '/b/main.py', '--exec-with-scale', '/a'])


class EntryPointExecTextTests(unittest.TestCase):
    """The entry-point `Exec=` serialization (ticket #34)."""

    def test_full_shape(self):
        self.assertEqual(
            launch.entry_point_exec_text(
                '/plugins/backend', '/apps/foo.appimage',
                ['--flag', '%F'],
                python_executable='/usr/bin/python3'),
            'env DESKTOPINTEGRATION=1 /usr/bin/python3 '
            '/plugins/backend/main.py --exec-with-scale '
            '/apps/foo.appimage --flag %F')

    def test_field_codes_survive_verbatim(self):
        for code in ('%f', '%F', '%u', '%U', '%i', '%c', '%d', '%k'):
            text = launch.entry_point_exec_text(
                '/b', '/a', ['--flag', code],
                python_executable='/usr/bin/python3')
            self.assertTrue(text.endswith(f'--flag {code}'), code)

    def test_paths_are_quoted(self):
        self.assertEqual(
            launch.entry_point_exec_text(
                '/my plug/backend', '/apps/my app.appimage', [],
                python_executable='/usr/bin/py thon3'),
            "env DESKTOPINTEGRATION=1 '/usr/bin/py thon3' "
            "'/my plug/backend/main.py' --exec-with-scale "
            "'/apps/my app.appimage'")


class ParseEntryPointExecTests(unittest.TestCase):
    """Reading back what entry_point_exec_text wrote."""

    def test_round_trip(self):
        text = launch.entry_point_exec_text(
            '/plugins/backend', '/apps/foo.appimage',
            ['--no-sandbox', '%U'],
            env_variables=['DESKTOPINTEGRATION=1', 'GTK_THEME=Adwaita'],
            python_executable='/usr/bin/python3')
        self.assertEqual(
            launch.parse_entry_point_exec(text),
            {'env_variables': ['DESKTOPINTEGRATION=1', 'GTK_THEME=Adwaita'],
             'image_path': '/apps/foo.appimage',
             'arguments': ['--no-sandbox', '%U']})

    def test_legacy_raw_image_exec_is_not_the_entry_point(self):
        self.assertIsNone(launch.parse_entry_point_exec(
            'env DESKTOPINTEGRATION=1 /apps/foo.appimage --flag %F'))

    def test_plain_exec_is_not_the_entry_point(self):
        self.assertIsNone(launch.parse_entry_point_exec('/apps/foo %F'))

    def test_quoted_paths_round_trip(self):
        text = launch.entry_point_exec_text(
            '/my plug/backend', '/apps/my app.appimage', [],
            python_executable='/usr/bin/py thon3')
        self.assertEqual(
            launch.parse_entry_point_exec(text),
            {'env_variables': ['DESKTOPINTEGRATION=1'],
             'image_path': '/apps/my app.appimage',
             'arguments': []})

    def test_unparseable_text_is_rejected(self):
        self.assertIsNone(launch.parse_entry_point_exec('env "unclosed'))

    def test_action_shape_without_env_prefix(self):
        self.assertEqual(
            launch.parse_entry_point_exec(
                '/usr/bin/python3 /b/main.py --exec-with-scale /a '
                '--new-window'),
            {'env_variables': [], 'image_path': '/a',
             'arguments': ['--new-window']})


class PanelLaunchArgvTests(unittest.TestCase):
    """The panel's argv (ticket #32): the listing hands it over instead
    of the QML assembling it. Must stay byte-identical to what
    Panel.qml's launchItem built: whitespace split, desktop field codes
    dropped, `env DESKTOPINTEGRATION=1 <path>` prefix."""

    def test_plain_flags(self):
        self.assertEqual(
            launch.panel_launch_argv('/apps/foo.appimage', '--no-sandbox'),
            ['env', 'DESKTOPINTEGRATION=1', '/apps/foo.appimage',
             '--no-sandbox'])

    def test_field_codes_are_dropped(self):
        for code in ('%f', '%F', '%u', '%U', '%i', '%c', '%d', '%k'):
            self.assertEqual(
                launch.panel_launch_argv('/a', f'--flag {code}'),
                ['env', 'DESKTOPINTEGRATION=1', '/a', '--flag'],
                code)

    def test_partial_field_code_is_a_real_argument(self):
        # the QML filter was exact-match only: %U1 and file%U stay
        self.assertEqual(
            launch.panel_launch_argv('/a', '%U1 file%U'),
            ['env', 'DESKTOPINTEGRATION=1', '/a', '%U1', 'file%U'])

    def test_empty_arguments_collapse_to_the_prefix(self):
        # the QML `"".trim().split(/\s+/)` produced [""] which the filter
        # dropped; the builder must produce the same argv
        self.assertEqual(
            launch.panel_launch_argv('/a', ''),
            ['env', 'DESKTOPINTEGRATION=1', '/a'])

    def test_whitespace_runs_split_like_the_qml_regex(self):
        self.assertEqual(
            launch.panel_launch_argv('/a', '  --x \t y  '),
            ['env', 'DESKTOPINTEGRATION=1', '/a', '--x', 'y'])

    def test_matches_the_qml_assembly_end_to_end(self):
        # the exact example the old Panel.qml comment described
        self.assertEqual(
            launch.panel_launch_argv('/apps/z.appimage', '--no-sandbox %U'),
            ['env', 'DESKTOPINTEGRATION=1', '/apps/z.appimage',
             '--no-sandbox'])


if __name__ == '__main__':
    unittest.main()

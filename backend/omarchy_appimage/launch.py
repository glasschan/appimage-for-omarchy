# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
"""The one launch-command builder (ticket #32).

Every place that writes or spawns a launch — the desktop-entry writer at
integrate time (``provider_install``), the clobbered-desktop repair
(``provider_listing._repair_clobbered_desktop``) and the panel's launch
action (the additive ``launch_argv`` key the listing reports) — builds its
command through this module. The point is the follow-up tickets: when the
launch environment changes (GDK_SCALE matching), it changes HERE and only
here.

Two serialisations of the same command exist, and both are pinned by
tests/test_launch.py to stay byte-identical to the pre-#32 assemblies:

* ``launch_exec_text`` — the desktop file's ``Exec=`` line: ``env``-style
  prefix, the image path shlex-quoted, arguments joined raw (the parsed
  tokens keep their original shape; the writer never re-quoted them).
* ``shlex.join(launch_argv(...))`` — what the repair path writes: every
  token quoted (this is what that site always did).

``panel_launch_argv`` is the token list Quickshell.execDetached spawns:
whitespace-split exec arguments with the desktop field codes (%f %F %u %U
%i %c %d %k) dropped, because the panel has no file or URL to hand over.
"""

import os
import re
import shlex
import sys

DESKTOPINTEGRATION_ENV = 'DESKTOPINTEGRATION=1'

# The scale-aware entry point (tickets #33/#34): the desktop entry's
# `Exec=` invokes `<python> <backend>/main.py --exec-with-scale <image>
# [args]` instead of the raw image, so a launcher click gets the focused
# monitor's scale. The plugin's backend directory is recorded per app at
# integrate time (`launch_backend_dir` in apps.ini) and rebuilt from the
# CURRENT directory by the install and repair paths, so a plugin update
# that moves the directory re-points existing entries instead of
# breaking them.
EXEC_SCALE_FLAG = '--exec-with-scale'
ENTRY_POINT_SCRIPT = 'main.py'

# Exact-match desktop field codes (Exec spec): dropped on launch because
# nothing can fill them. A partial match (%U1, file%U) is a real argument.
_FIELD_CODE = re.compile(r'%[fFuUicdk]')

# An env assignment as `env` writes it: NAME=value with a bare variable
# name (never a path — a path can carry '=' and is not an assignment).
_ENV_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')


def merge_env_variables(current, previous):
    """The install path's env-variable precedence: the updating app's own
    variables are adopted wholesale when it has any, and
    DESKTOPINTEGRATION=1 is appended when no variable carries the name.

    Returns ``(variables, changed)`` — ``changed`` is what told the writer
    to rewrite the ``Exec`` line, so the caller can keep writing the
    entry untouched when neither adoption nor the append happened.
    """
    if previous:
        env_variables = list(previous)
        changed = True
    else:
        env_variables = list(current)
        changed = False
    if not any(v.startswith('DESKTOPINTEGRATION=') for v in env_variables):
        env_variables.append(DESKTOPINTEGRATION_ENV)
        changed = True
    return env_variables, changed


def launch_argv(image_path, exec_arguments, env_variables=(
        DESKTOPINTEGRATION_ENV,)):
    """The launch command as argv tokens: ``env`` prefix, then the image,
    then the (already parsed) arguments verbatim."""
    return ['env', *env_variables, image_path, *exec_arguments]


def launch_exec_text(image_path, exec_arguments, env_variables):
    """The desktop file's ``Exec=`` text: ``env VAR=...`` prefix, the
    quoted image path, arguments joined raw. Empty arguments leave no
    trailing space; an empty environment drops the prefix entirely."""
    env_vars_prefix = f'env {" ".join(env_variables)} ' if env_variables \
        else ''
    exec_arguments_text = ' '.join(exec_arguments)
    return ''.join([
        env_vars_prefix,
        shlex.quote(image_path),
        f' {exec_arguments_text}' if exec_arguments_text else '',
    ])


def entry_point_script(backend_dir) -> str:
    """The absolute path of the scale-aware entry point inside
    `backend_dir` (the plugin's backend directory)."""
    return os.path.join(backend_dir, ENTRY_POINT_SCRIPT)


def entry_point_argv(backend_dir, image_path, exec_arguments,
                     env_variables=(DESKTOPINTEGRATION_ENV,),
                     python_executable=None):
    """The entry-point launch command as argv tokens: optional `env`
    prefix, `<python> <backend>/main.py`, the `--exec-with-scale` flag,
    the image, then the (already parsed) arguments verbatim."""
    python = python_executable or sys.executable
    tokens = [python, entry_point_script(backend_dir), EXEC_SCALE_FLAG,
              image_path, *exec_arguments]
    if env_variables:
        tokens = ['env', *env_variables, *tokens]
    return tokens


def entry_point_exec_text(backend_dir, image_path, exec_arguments,
                          env_variables=(DESKTOPINTEGRATION_ENV,),
                          python_executable=None):
    """The desktop file's ``Exec=`` text for the entry-point launch: the
    same serialization rules as ``launch_exec_text`` (env prefix joined
    with spaces, every path shlex-quoted, arguments joined raw so the
    desktop field codes survive verbatim). An empty environment drops
    the ``env`` prefix entirely."""
    python = python_executable or sys.executable
    env_vars_prefix = f'env {" ".join(env_variables)} ' if env_variables \
        else ''
    exec_arguments_text = ' '.join(exec_arguments)
    return ''.join([
        env_vars_prefix,
        shlex.quote(python), ' ',
        shlex.quote(entry_point_script(backend_dir)), ' ',
        EXEC_SCALE_FLAG, ' ',
        shlex.quote(image_path),
        f' {exec_arguments_text}' if exec_arguments_text else '',
    ])


def parse_entry_point_exec(exec_text):
    """Read back an ``Exec=`` written by ``entry_point_exec_text``.

    Returns ``{'env_variables', 'image_path', 'arguments'}`` or None when
    the text is not the entry-point shape (a legacy raw-image Exec, or an
    app whose own first argument merely looks like one). The env
    assignments are recognised between ``env`` and the interpreter only —
    a bare NAME=value form, never a path."""
    try:
        tokens = shlex.split(exec_text)
    except ValueError:
        return None
    index = 1 if tokens and tokens[0] == 'env' else 0
    env_variables = []
    while index < len(tokens) and _ENV_ASSIGNMENT.match(tokens[index]):
        env_variables.append(tokens[index])
        index += 1
    remaining = tokens[index:]
    if len(remaining) < 4:
        return None
    _python, script, flag, image_path = remaining[:4]
    if flag != EXEC_SCALE_FLAG or not script.endswith(ENTRY_POINT_SCRIPT):
        return None
    return {'env_variables': env_variables, 'image_path': image_path,
            'arguments': list(remaining[4:])}


def panel_launch_argv(image_path, exec_arguments_text):
    """The argv the panel spawns: ``launch_argv`` over the reported
    exec-argument string, whitespace-split with the desktop field codes
    dropped — byte-identical to what Panel.qml's launchItem assembled
    before ticket #32 moved the assembly here."""
    args = exec_arguments_text.split()
    args = [a for a in args if not _FIELD_CODE.fullmatch(a)]
    return launch_argv(image_path, args)

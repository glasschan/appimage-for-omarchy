# exec_scale.py — launch an AppImage at the focused monitor's scale
# factor (`--exec-with-scale`, ticket #33).
#
# Original code written for this plugin (GDK_SCALE wave, spec #31 /
# ticket #33), GPL-3.0 (same license as the plugin; see LICENSE).
#
# The problem: an XWayland client launched into a scale-1 focused
# monitor renders at 2x when the session exports GDK_SCALE=2 (the env
# var is global, the monitor scale is per-output). The fix at launch
# time: query the compositor for the FOCUSED monitor's scale, round it
# to the whole factor GDK_SCALE takes, and exec the image with that
# value — `os.execve` REPLACES the process image, so no wrapper outlives
# the app and the app is PID one of its own lifecycle from the caller's
# point of view.
#
# Never-fail rule: a launch must never fail because the scale could not
# be determined. EVERY failure of the compositor query (binary missing,
# non-zero exit, timeout, malformed output, no focused monitor,
# non-numeric scale) degrades to exec'ing with the CALLER'S environment
# unchanged — the app still starts.
#
# Security posture (ticket #33 is on the process-spawning trigger list):
# the only child ever spawned is the bounded compositor query — constant
# argv through utils.run_command (argument lists, no shell, 10 MiB
# output cap, process-group kill on timeout). The image and its
# arguments are handed to `os.execve` as argv, never spliced into a
# shell command line. Nothing here reads unbounded output.

import json
import logging
import math
import os

from . import constants
from .utils import run_command

# The focused-monitor scale lives in the Wayland compositor. Omarchy is
# Hyprland: `hyprctl -j monitors` prints one JSON array, one object per
# monitor, each carrying `focused` (bool) and `scale` (number). The argv
# is constant — nothing user-controlled is ever interpolated.
HYPRCTL_QUERY_ARGV = ['hyprctl', '-j', 'monitors']

# Wall-clock bound on the query. A compositor that cannot answer in 5 s
# is not going to answer usefully; the launch falls back to the
# inherited environment instead of waiting.
QUERY_TIMEOUT_SECONDS = 5.0

# Sanity window for a usable scale: GDK_SCALE takes whole factors
# starting at 1; anything outside (0, MAX_SCALE] is compositor noise and
# treated as "could not determine".
MAX_SCALE = 8.0


def _usable_scale(value) -> bool:
    """True when `value` is a real number (never bool — JSON true/false
    are int subclasses) inside the sanity window."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return 0 < float(value) <= MAX_SCALE


def parse_focused_scale(stdout_text: str):
    """The focused monitor's scale from `hyprctl -j monitors` stdout, or
    None when the answer is unusable (malformed JSON, not a list, no
    focused monitor, non-numeric or out-of-range scale). Pure — the
    tests drive every branch through this."""
    try:
        monitors = json.loads(stdout_text)
    except (ValueError, TypeError):
        return None
    if not isinstance(monitors, list):
        return None
    for monitor in monitors:
        if not isinstance(monitor, dict):
            continue
        if monitor.get('focused') is not True:
            continue
        scale = monitor.get('scale')
        return float(scale) if _usable_scale(scale) else None
    return None  # no focused monitor in the answer


def focused_scale(query_argv=None, timeout=QUERY_TIMEOUT_SECONDS):
    """One bounded compositor query; the focused monitor's scale as a
    float, or None on ANY failure. Never raises.

    `query_argv` overrides the command (tests point it at fakes); a
    missing binary, a non-zero exit, a timeout and a malformed answer
    are all the same "could not determine" verdict."""
    argv = HYPRCTL_QUERY_ARGV if query_argv is None else query_argv
    try:
        result = run_command(argv, check=False, timeout=timeout)
    except Exception as e:  # FileNotFoundError, TimeoutExpired, OSError
        logging.debug('compositor query failed: %s', e)
        return None
    if result.returncode != 0:
        logging.debug('compositor query exited %d', result.returncode)
        return None
    return parse_focused_scale(result.stdout.decode(errors='replace'))


def rounded_scale(scale: float) -> int:
    """The whole factor GDK_SCALE takes: nearest whole number, halves
    away from .5 downward (1.49 -> 1, 1.5 -> 2, 2.0 -> 2). floor(x+0.5)
    rather than round(), whose ties-to-even would send 2.5 to 2."""
    return int(math.floor(scale + 0.5))


def _inherited_matches(inherited, rounded: int) -> bool:
    """True when the caller's own GDK_SCALE already produces the rounded
    factor — the env is then handed over UNCHANGED."""
    try:
        value = float(inherited)
    except (TypeError, ValueError):
        return False
    return math.isfinite(value) and rounded_scale(value) == rounded


def launch_env(environment, scale):
    """The environment the image is exec'd with.

    `scale` None (query unusable) -> the caller's environment unchanged.
    Otherwise GDK_SCALE is set to the rounded focused-monitor factor —
    except when the caller's own GDK_SCALE already rounds to the same
    factor, in which case the environment is also handed over unchanged
    (the value the caller would produce IS the focused monitor's)."""
    env = dict(environment)
    if scale is None:
        return env
    rounded = rounded_scale(scale)
    if _inherited_matches(env.get('GDK_SCALE'), rounded):
        return env
    env['GDK_SCALE'] = str(rounded)
    return env


def exec_with_scale(image_path: str, arguments, environment=None,
                    query=focused_scale):
    """Compute the launch environment, then REPLACE this process with
    the image. Never returns on success; a failed exec (missing image,
    not executable) raises and the CLI's error path reports it.

    `query` is the compositor-query seam (tests mock it). On any
    unusable answer the child inherits the caller's environment
    unchanged — a launch never fails because the scale could not be
    determined."""
    if environment is None:
        environment = constants.process_environment()
    env = launch_env(environment, query())
    os.execve(image_path, [image_path, *arguments], env)

# commands/exec_scale.py — the `--exec-with-scale` verb (ticket #33).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# A thin verb over omarchy_appimage.exec_scale (the mechanism module):
# everything after the image path is the image's own argument list and
# is forwarded UNTOUCHED to os.execve — including tokens that look like
# options, because they belong to the app. The verb therefore accepts no
# options of its own and never runs `_reject_unknown_options`: only the
# first token is ours, and it must be a plain image path, never an
# option. The process ends in `os.execve` on success, so this verb never
# prints a document and never returns an exit code — it either becomes
# the app or raises into cli.main's error path.

from .. import exec_scale
from .envelope import UsageError


def cmd_exec_with_scale(args: list) -> int:
    """Launch <image> [args...] with GDK_SCALE matching the focused
    monitor's scale (compositor query bounded and best-effort: an
    unusable answer execs with the caller's environment unchanged).

    The verb is the plugin's launch handover, so it stamps the
    managed-launch marker DESKTOPINTEGRATION=1 onto the handover env
    (append-if-absent — the caller's own value wins): the panel's
    detached spawn cannot carry env vars, and every launch the plugin
    performs carries the same marker the launch builder writes into the
    desktop entry (ticket #35)."""
    if not args:
        raise UsageError('exec-with-scale requires an AppImage path')
    if args[0].startswith('-'):
        # the caller wrote an option where the image belongs; there is
        # no context in which an option before the image is ours
        raise UsageError(
            f'exec-with-scale takes an AppImage path, not {args[0]!r} '
            '(arguments to the app go after the path)')
    exec_scale.exec_with_scale(args[0], args[1:],
                               extra_env=exec_scale.PLUGIN_LAUNCH_ENV)
    return 0  # unreachable: execve replaced the process

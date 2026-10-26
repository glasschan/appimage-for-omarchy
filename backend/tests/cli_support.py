# cli_support.py — shared in-process CLI helper for the end-to-end CLI tests.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.
#
# Update commands that need to fake connectivity or a running app run the
# cmd_* functions in-process (with unittest.mock seams) instead of via the
# main.py subprocess; everything else goes through the real CLI.

import io
from contextlib import redirect_stderr, redirect_stdout

from omarchy_appimage import cli


class InProcessCliMixin:
    """Runs cmd_* functions directly so net/internals can be patched."""

    def run_cmd(self, func, args: list):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = func(args)
            except cli.UsageError as e:
                err.write(f'Error: {e}\n')
                code = 2
            except (cli.OperationError, cli.InternalError) as e:
                err.write(f'Error: {e}\n')
                code = 1
        return out.getvalue(), err.getvalue(), code

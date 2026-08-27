#!/usr/bin/env python3
# main.py — CLI entry point for the AppImage for Omarchy backend.
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Replaces GearLever's src/Cli.py + flatpak launcher: a plain Python 3
# stdlib-only script meant to be spawned once per action by the QML
# frontend (Quickshell Process) and exit immediately afterwards
# (PRD §2: zero resident memory).

import logging
import os
import sys

# Keep the installed plugin directory clean: never drop __pycache__/
# next to the sources when the shell spawns us.
sys.dont_write_bytecode = True

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from omarchy_appimage import extractor          # noqa: E402
from omarchy_appimage.cli import main           # noqa: E402


def configure_logging():
    level = logging.DEBUG if os.environ.get('OMARCHY_APPIMAGE_DEBUG') \
        else logging.WARNING
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format='%(levelname)s: %(message)s',
    )


if __name__ == '__main__':
    configure_logging()
    try:
        exit_code = main(sys.argv)
    finally:
        extractor.cleanup_temp_dirs()
    sys.exit(exit_code)

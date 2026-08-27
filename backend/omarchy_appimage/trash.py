# trash.py — FreeDesktop Trash implementation (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# GearLever used Gio.File.trash(); this is a self-contained
# implementation of the FreeDesktop Trash Specification 1.0
# (https://specifications.freedesktop.org/trash-spec/latest/) using
# $XDG_DATA_HOME/Trash (honoured via constants.xdg_data_home()).

import logging
import os
import shutil
import urllib.parse
from datetime import datetime

from . import constants


def trash_root() -> str:
    return os.path.join(constants.xdg_data_home(), 'Trash')


def _ensure_layout():
    for sub in ('files', 'info'):
        os.makedirs(os.path.join(trash_root(), sub), exist_ok=True)


def _unique_name(directory: str, name: str) -> str:
    """Resolve name collisions like GLib does: name, name.2, name.3 ...
    (the counter is appended to the complete file name)."""
    candidate = name
    i = 2
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f'{name}.{i}'
        i += 1
    return candidate


def send_to_trash(path: str) -> str:
    """Move `path` into the user trash dir; returns the path inside
    Trash/files. Raises OSError on failure (caller decides to fall back
    to deletion, like GearLever's uninstall does)."""
    path = os.path.abspath(path)
    if not os.path.lexists(path):
        raise FileNotFoundError(path)

    _ensure_layout()
    basename = os.path.basename(path.rstrip('/'))
    dest_name = _unique_name(os.path.join(trash_root(), 'files'), basename)
    dest_path = os.path.join(trash_root(), 'files', dest_name)

    # .trashinfo first: per spec, an info file without its file is
    # recoverable, a trashed file without info is not.
    deletion_date = datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    quoted_path = urllib.parse.quote(path, safe='/')
    info = (f'[Trash Info]\n'
            f'Path={quoted_path}\n'
            f'DeletionDate={deletion_date}\n')

    info_path = os.path.join(trash_root(), 'info', dest_name + '.trashinfo')
    tmp_info = info_path + '.tmp'
    with open(tmp_info, 'w', encoding='utf-8') as f:
        f.write(info)
    os.replace(tmp_info, info_path)

    try:
        shutil.move(path, dest_path)
    except BaseException:
        try:
            os.unlink(info_path)
        except OSError:
            pass
        raise

    logging.debug('Trashed %s -> %s', path, dest_path)
    return dest_path

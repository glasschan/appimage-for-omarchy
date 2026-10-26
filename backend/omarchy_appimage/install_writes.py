# install_writes.py — install name resolution + atomic file writes.
#
# Split out of provider.py (ticket 57): the security-relevant write
# primitives (temp-file + fsync + os.replace contract, the O_NOFOLLOW
# source open) and the one naming rule share a home away from the
# behaviour mixins. Behaviour is unchanged — the functions moved
# verbatim; provider.py re-exports them.

import os
import stat
import tempfile

from .provider_types import InternalError
from .utils import remove_special_chars


def _resolve_install_names(source_basename: str, desktop_name, version: str,
                           existing_names) -> tuple:
    """Install filename + desktop/icon prefix for a new app, with
    GearLever's conflict suffixing (never overwrite an existing app).

    `desktop_name` (the embedded desktop entry's Name) and `version`
    (X-AppImage-Version) are attacker-controlled embedded fields. Both are
    passed through remove_special_chars() BEFORE any collision reuse, so
    every candidate that reaches os.path.join() is a single flat filename:
    no separator and no '..' component can ever survive, and a colliding
    package cannot write outside the managed directories (round-4 review).

    Non-collision shape is unchanged from upstream: sanitized
    `name.appimage`; collision candidates are `name.appimage_v0_11_3` /
    `name.appimage_N` plus a trailing `.appimage`."""
    appimage_filename = 'gearlever_' + os.path.splitext(source_basename)[0]

    if desktop_name is not None:
        appimage_filename = desktop_name.lower().replace(' ', '_')

    # (GearLever's 'exec-as-name-for-terminal-apps' setting defaults
    # to false, so the extension is always appended here)
    appimage_filename = f'{appimage_filename}.appimage'

    # NOTE: like upstream, app_name_without_ext still contains the
    # extension at this point — but it is sanitized and lowercased ONCE
    # here, before the collision loop can ever rebuild from it.
    app_name_without_ext = remove_special_chars(appimage_filename).lower()
    appimage_filename = app_name_without_ext

    i = 0

    # if there is already an app with the same name,
    # we try not to overwrite
    while appimage_filename in existing_names:
        if i == 0 and version:
            appimage_filename = (app_name_without_ext + '_'
                                 + remove_special_chars(
                                     version.replace('.', '_')))
        else:
            appimage_filename = app_name_without_ext + f'_{i}'

        appimage_filename = appimage_filename + '.appimage'
        i += 1

    return appimage_filename, os.path.splitext(appimage_filename)[0]


# Public alias for cross-module callers: the preview's destination block
# (cli.py) derives names through this helper, and cli.py must not import a
# provider-internal name. The underscore-private spelling stays the
# implementation — install_file() and the naming unit tests keep using it.
resolve_install_names = _resolve_install_names


def _replace_destination(dest: str, mode: int, write_body, before_replace=None):
    """Descriptor-safe replace of `dest` with whatever `write_body` writes.

    Shared by `_atomic_install` (a file on disk) and `_atomic_write_bytes`
    (in-memory bytes): the payload lands in a fresh temp file next to
    `dest` (created exclusively by mkstemp and written straight through
    its descriptor, so no window ever exists where a planted name could
    swap the inode), is chmod'd + fsync'd, then os.replace()'d over the
    final name — rename never follows a symlink at `dest`, so a planted
    symlink gets replaced instead of written through, and a crash can
    never leave a truncated file behind.

    `before_replace` is the narrow seam for the running-app grant (ticket
    26): it is called once, right before os.replace(), with the complete
    but not yet visible temp file on disk. It defaults to None; an
    exception from it aborts the install and the temp file is removed
    like any other failure."""
    try:
        if stat.S_ISDIR(os.lstat(dest).st_mode):
            raise InternalError(f'refusing to install over a directory: '
                                f'{dest}')
    except FileNotFoundError:
        pass

    dest_dir = os.path.dirname(dest) or '.'
    fd, tmp_path = tempfile.mkstemp(prefix='.gearlever-tmp-', dir=dest_dir)

    try:
        with os.fdopen(fd, 'wb') as out:
            fd = None  # the fdopen context owns it now
            write_body(out)
            os.fchmod(out.fileno(), mode)
            out.flush()
            os.fsync(out.fileno())
        if before_replace is not None:
            before_replace()
        os.replace(tmp_path, dest)
    except BaseException:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _atomic_install(src: str, dest: str, mode: int, before_replace=None):
    """Descriptor-safe install of `src` at `dest` (marketplace review).

    The source is the user's own chosen file: its final component is
    resolved with realpath first (following a symlink there is correct),
    then opened O_NOFOLLOW; the bytes themselves are written by
    `_replace_destination`, which owns the temp-file/replace dance and
    the `before_replace` hook (see there)."""
    def _copy_source(out):
        # O_NONBLOCK keeps a planted FIFO from blocking the open — the
        # regular-file check below rejects it right after (regular
        # files ignore the flag)
        src_fd = os.open(os.path.realpath(src),
                         os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(src_fd).st_mode):
                raise InternalError(f'source is not a regular file: {src}')
            with os.fdopen(src_fd, 'rb') as f:
                src_fd = None  # the fdopen context owns it now
                for chunk in iter(lambda: f.read(1 << 20), b''):
                    out.write(chunk)
        finally:
            if src_fd is not None:
                try:
                    os.close(src_fd)
                except OSError:
                    pass

    _replace_destination(dest, mode, _copy_source, before_replace)


def _atomic_write_bytes(data: bytes, dest: str, mode: int):
    """Descriptor-safe install of in-memory bytes (the sanitised icon).

    Same temp-file/fsync/replace contract as `_atomic_install`, without a
    source file that could change between planning and commit."""
    _replace_destination(dest, mode, lambda out: out.write(data))


def _user_applications_dir() -> str:
    from . import constants
    return constants.user_applications_dir()

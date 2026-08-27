# helpers.py — shared test utilities (not a test module itself).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import urllib.request

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN_PY = os.path.join(BACKEND_DIR, 'main.py')

if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

FIXTURES_DIR = '/tmp/appimage-testfiles'
NVIM_FIXTURE = os.path.join(FIXTURES_DIR, 'nvim.appimage')
NVIM_FIXTURE_URL = ('https://github.com/neovim/neovim/releases/download/'
                    'v0.11.3/nvim-linux-x86_64.appimage')
NVIM_SQUASHFS_OFFSET = 944632  # ground truth (build-aux/get_appimage_offset.sh)


def download_fixture() -> str:
    """Ensure the neovim type-2 AppImage fixture exists; None on failure."""
    if os.path.exists(NVIM_FIXTURE):
        return NVIM_FIXTURE
    try:
        os.makedirs(FIXTURES_DIR, exist_ok=True)
        tmp = NVIM_FIXTURE + '.part'
        urllib.request.urlretrieve(NVIM_FIXTURE_URL, tmp)
        os.replace(tmp, NVIM_FIXTURE)
        os.chmod(NVIM_FIXTURE, 0o755)
    except Exception:
        return None
    return NVIM_FIXTURE


class FakeXDGTestCase(unittest.TestCase):
    """Base class: every test runs against an isolated HOME/XDG sandbox.

    Tests must never touch the real user home.
    """

    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix='omarchy-appimage-test-')
        self.data_home = os.path.join(self.sandbox, 'data')
        self.config_home = os.path.join(self.sandbox, 'config')
        self.home = os.path.join(self.sandbox, 'home')
        for d in (self.data_home, self.config_home, self.home):
            os.makedirs(d, exist_ok=True)

        self._old_env = {k: os.environ.get(k)
                         for k in ('HOME', 'XDG_DATA_HOME', 'XDG_CONFIG_HOME')}

        os.environ['HOME'] = self.home
        os.environ['XDG_DATA_HOME'] = self.data_home
        os.environ['XDG_CONFIG_HOME'] = self.config_home

        self.addCleanup(self._restore)
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)

    def _restore(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ------------------------------------------------------------ sandbox

    @property
    def managed_dir(self) -> str:
        return os.path.join(self.home, 'AppImages')

    @property
    def applications_dir(self) -> str:
        return os.path.join(self.data_home, 'applications')

    @property
    def trash_files(self) -> str:
        return os.path.join(self.data_home, 'Trash', 'files')

    @property
    def trash_info(self) -> str:
        return os.path.join(self.data_home, 'Trash', 'info')

    def put_fake_appimage(self, name: str, content: bytes = None) -> str:
        """A minimal file that passes AppImage magic detection (type 2):
        ELF magic + \\x41\\x49\\x02 at bytes 8..10. Enough for list/remove
        flows that never need to extract anything."""
        path = os.path.join(self.sandbox, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(content if content is not None
                    else b'\x7fELF\x02\x01\x01' + b'\x00' + b'\x41\x49\x02')
        return path

    # ---------------------------------------------------------- subprocess

    def run_cli(self, *args, check=False) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env['HOME'] = self.home
        env['XDG_DATA_HOME'] = self.data_home
        env['XDG_CONFIG_HOME'] = self.config_home
        return subprocess.run(
            [sys.executable, MAIN_PY, *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=env, timeout=300, check=check)


def make_minimal_elf(is64: bool = True, big_endian: bool = False,
                     e_shoff: int = 0x1000, e_shentsize: int = 64,
                     e_shnum: int = 3, e_machine: int = 0x3E) -> bytes:
    """Hand-built ELF header for offset/arch unit tests (no fixture
    needed)."""
    endian = '>' if big_endian else '<'
    header = bytearray(64)

    header[0:4] = b'\x7fELF'
    header[4] = 2 if is64 else 1
    header[5] = 2 if big_endian else 1
    struct.pack_into(endian + 'H', header, 0x12, e_machine)

    if is64:
        struct.pack_into(endian + 'Q', header, 0x28, e_shoff)
        struct.pack_into(endian + 'HH', header, 0x3A, e_shentsize, e_shnum)
    else:
        struct.pack_into(endian + 'I', header, 0x20, e_shoff)
        struct.pack_into(endian + 'HH', header, 0x2E, e_shentsize, e_shnum)

    # AppImage type-2 magic at bytes 8..10
    header[8:11] = b'\x41\x49\x02'
    return bytes(header)

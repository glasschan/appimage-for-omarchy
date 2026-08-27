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
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

    def install_fake_app(self, desktop_name='fakeapp',
                         app_name='Fake App') -> tuple:
        """Simulate an already-integrated app: managed AppImage + .desktop
        + icon, exactly the layout install_file produces. Enough for
        list/remove/update-source flows that never need to extract."""
        os.makedirs(os.path.join(self.managed_dir, '.icons'), exist_ok=True)
        os.makedirs(self.applications_dir, exist_ok=True)

        appimage = os.path.join(self.managed_dir,
                                desktop_name + '.appimage')
        with open(appimage, 'wb') as f:
            f.write(b'\x7fELF\x02\x01\x01\x00\x41\x49\x02padding')
        os.chmod(appimage, 0o755)

        icon = os.path.join(self.managed_dir, '.icons', desktop_name + '.png')
        with open(icon, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n')

        desktop = os.path.join(self.applications_dir,
                               desktop_name + '.desktop')
        with open(desktop, 'w') as f:
            f.write(f'''[Desktop Entry]
Name={app_name}
Exec=env DESKTOPINTEGRATION=1 {appimage}
TryExec={appimage}
Icon={icon}
Type=Application
Terminal=false
X-AppImage-Version=1.0
''')
        return appimage, desktop, icon

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


def make_elf_with_sections(sections: dict, is64: bool = True,
                           big_endian: bool = False,
                           e_machine: int = 0x3E) -> bytes:
    """Hand-built ELF with a real section table for the section-reading
    code paths (elf._read_sections / read_upd_info).

    `sections` maps section name -> raw content bytes; a .shstrtab is
    generated automatically. Layout: ELF header | section data | .shstrtab
    | section header table (with the leading NULL entry)."""
    endian = '>' if big_endian else '<'
    ehsize = 64 if is64 else 52
    shentsize = 64 if is64 else 40

    # section-name string table ('\0' + name + '\0' for each section)
    strtab = bytearray(b'\x00')
    name_offsets = {}
    for name in list(sections) + ['.shstrtab']:
        name_offsets[name] = len(strtab)
        strtab += name.encode('ascii') + b'\x00'
    strtab = bytes(strtab)

    offset = ehsize
    entries = []                       # (sh_name, sh_offset, sh_size)
    for name, blob in sections.items():
        entries.append((name_offsets[name], offset, len(blob)))
        offset += len(blob)
    strtab_offset = offset
    offset += len(strtab)
    shoff = offset

    # section 0 is the NULL entry, the last one is .shstrtab
    shnum = len(sections) + 2
    shstrndx = len(sections) + 1

    header = bytearray(ehsize)
    header[0:4] = b'\x7fELF'
    header[4] = 2 if is64 else 1
    header[5] = 2 if big_endian else 1
    header[6] = 1                      # EI_VERSION
    header[8:11] = b'\x41\x49\x02'     # AppImage type-2 magic
    struct.pack_into(endian + 'H', header, 0x12, e_machine)

    if is64:
        struct.pack_into(endian + 'Q', header, 0x28, shoff)
        struct.pack_into(endian + 'HHH', header, 0x3A,
                         shentsize, shnum, shstrndx)
    else:
        struct.pack_into(endian + 'I', header, 0x20, shoff)
        struct.pack_into(endian + 'HHH', header, 0x2E,
                         shentsize, shnum, shstrndx)

    def shdr(name_off: int, data_off: int, size: int) -> bytes:
        # sh_name, sh_type=PROGBITS, sh_flags, sh_addr, sh_offset, sh_size,
        # sh_link, sh_info, sh_addralign, sh_entsize
        if is64:
            return struct.pack(endian + 'IIQQQQIIQQ',
                               name_off, 1, 0, 0, data_off, size, 0, 0, 1, 0)
        return struct.pack(endian + 'IIIIIIIIII',
                           name_off, 1, 0, 0, data_off, size, 0, 0, 1, 0)

    table = bytearray(shentsize)       # NULL section
    for name_off, data_off, size in entries:
        table += shdr(name_off, data_off, size)
    table += shdr(name_offsets['.shstrtab'], strtab_offset, len(strtab))

    blob = bytes(header)
    for name, content in sections.items():
        blob += content
    blob += strtab + bytes(table)
    return blob


# ------------------------------------------------------------ local http

class _FixtureHandler(BaseHTTPRequestHandler):
    routes = {}  # {path: (status, headers, body)} set per server instance

    def _reply(self):
        route = self.routes.get(self.path)
        if route is None:
            body = b'not found'
            self.send_response(404)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
            return
        status, headers, body = route
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        if not any(k.lower() == 'content-length' for k in headers):
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    do_GET = do_HEAD = _reply

    def log_message(self, format, *args):  # silence the test output
        pass


class FixtureHTTPServer:
    """Static-table HTTP server on 127.0.0.1:0 for hermetic network tests
    (update-source checks, --update downloads)."""

    def __init__(self, routes: dict):
        handler = type('RoutedHandler', (_FixtureHandler,), {'routes': routes})
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)
        self._thread.start()

    @property
    def routes(self) -> dict:
        """The live route table (mutable: add routes after start)."""
        return self.server.RequestHandlerClass.routes

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f'http://{host}:{port}'

    def url(self, path: str) -> str:
        return self.base_url + path

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)

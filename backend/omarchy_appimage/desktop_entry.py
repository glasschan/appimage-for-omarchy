# desktop_entry.py — minimal .desktop file reader/writer (stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Replaces GearLever's pyxdg DesktopEntry + desktop-entry-lib (JdDesktopEntry)
# pair: one order-preserving implementation that keeps locale keys
# (Name[xx]), field codes (%f %F %u %U %i %c %k) and [Desktop Action *]
# sections intact when rewriting the file.

import os


MAIN_SECTION = 'Desktop Entry'


class DesktopEntryAction:
    """A [Desktop Action <id>] group."""

    def __init__(self, name: str, keys: dict):
        self.name = name           # action id (the part after 'Desktop Action')
        self.keys = keys           # ordered dict of key -> value

    def get(self, key: str, default=''):
        return self.keys.get(key, default)

    @property
    def Exec(self):
        return self.keys.get('Exec', '')

    @Exec.setter
    def Exec(self, value):
        self.keys['Exec'] = _sanitize(value)

    @property
    def Icon(self):
        return self.keys.get('Icon', '')

    @Icon.setter
    def Icon(self, value):
        self.keys['Icon'] = _sanitize(value)


def _sanitize(value) -> str:
    """Desktop files are line-based; values must never contain newlines."""
    return str(value).replace('\n', ' ').strip()


class DesktopEntry:
    def __init__(self, path: str = None, content: str = None):
        self.path = path
        self.sections = {}       # section name -> ordered dict
        self.section_order = []  # keep the original section order
        self.main = self._section(MAIN_SECTION)

        if content is None and path is not None:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
        if content is not None:
            self._parse(content)

    # ---------------------------------------------------------------- parse

    def _section(self, name: str) -> dict:
        if name not in self.sections:
            self.sections[name] = {}
            self.section_order.append(name)
        return self.sections[name]

    def _parse(self, content: str):
        current = self.main
        for raw_line in content.splitlines():
            line = raw_line.strip('\ufeff').rstrip('\r').strip()
            if not line or line.startswith('#') or line.startswith(';'):
                continue

            if line.startswith('[') and line.endswith(']'):
                current = self._section(line[1:-1].strip())
                continue

            if '=' not in line:
                continue

            key, _, value = line.partition('=')
            current[key.strip()] = value.strip()

    # ------------------------------------------------------------------ get

    def get(self, key: str, default=None):
        """Raw value lookup in [Desktop Entry] (case-sensitive, like the
        spec requires)."""
        value = self.main.get(key)
        return default if value is None else value

    def getName(self) -> str:
        return self.get('Name', '')

    def getGenericName(self) -> str:
        return self.get('GenericName', '')

    def getComment(self) -> str:
        return self.get('Comment', '')

    def getExec(self) -> str:
        return self.get('Exec', '')

    def getTryExec(self) -> str:
        return self.get('TryExec', '')

    def getIcon(self) -> str:
        return self.get('Icon', '')

    def getVersion(self) -> str:
        return self.get('Version', '')

    def getTerminal(self) -> bool:
        return str(self.get('Terminal', 'false')).lower() in ('true', '1')

    def getFileName(self) -> str:
        return self.path or ''

    # ------------------------------------------------------------------ set

    def set(self, key: str, value):
        self.main[key] = _sanitize(value)

    @property
    def TryExec(self):
        return self.getTryExec()

    @TryExec.setter
    def TryExec(self, value):
        self.set('TryExec', value)

    @property
    def Exec(self):
        return self.getExec()

    @Exec.setter
    def Exec(self, value):
        self.set('Exec', value)

    @property
    def Icon(self):
        return self.getIcon()

    @Icon.setter
    def Icon(self, value):
        self.set('Icon', value)

    @property
    def Name(self) -> '_NameKey':
        return _NameKey(self)

    def set_custom(self, key: str, value):
        self.set(key, value)

    # -------------------------------------------------------------- actions

    @property
    def Actions(self) -> dict:
        """All [Desktop Action <id>] groups, keyed by action id."""
        actions = {}
        for section in self.section_order:
            if section.startswith('Desktop Action '):
                action_id = section[len('Desktop Action '):].strip()
                actions[action_id] = DesktopEntryAction(action_id,
                                                        self.sections[section])
        return actions

    # ---------------------------------------------------------------- write

    def get_text(self) -> str:
        lines = []
        for section in self.section_order:
            lines.append(f'[{section}]')
            for key, value in self.sections[section].items():
                lines.append(f'{key}={_sanitize(value)}')
            lines.append('')
        return '\n'.join(lines).rstrip('\n') + '\n'

    def write_file(self, path: str = None):
        from .utils import atomic_write
        path = path or self.path
        self.path = path
        atomic_write(path, self.get_text().encode('utf-8'))


class _NameKey:
    """Helper so callers can do `entry.Name.default_text = 'x'` like
    desktop-entry-lib does in GearLever's install_file()."""

    def __init__(self, entry: DesktopEntry):
        self._entry = entry

    @property
    def default_text(self) -> str:
        return self._entry.getName()

    @default_text.setter
    def default_text(self, value):
        self._entry.set('Name', value)

# test_desktop_entry.py — .desktop read/write helper tests.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import unittest

from helpers import FakeXDGTestCase

from omarchy_appimage.desktop_entry import DesktopEntry

SAMPLE = '''[Desktop Entry]
Type=Application
Name=Neovim
Name[zh_TW]=Neovim 編輯器
GenericName=Text Editor
Comment=Edit text files
TryExec=nvim
Exec=nvim %F
Icon=nvim
Terminal=true
Actions=new-window;preferences;
X-AppImage-Version=1.2.3

[Desktop Action new-window]
Name=New Window
Exec=nvim --new-window
Icon=nvim-new

[Desktop Action preferences]
Name=Preferences
Exec=nvim --prefs
'''


class DesktopEntryTests(FakeXDGTestCase):
    def _write_sample(self) -> str:
        path = os.path.join(self.sandbox, 'sample.desktop')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(SAMPLE)
        return path

    def test_parse_basics(self):
        entry = DesktopEntry(content=SAMPLE)
        self.assertEqual(entry.getName(), 'Neovim')
        self.assertEqual(entry.getComment(), 'Edit text files')
        self.assertEqual(entry.getExec(), 'nvim %F')
        self.assertEqual(entry.getTryExec(), 'nvim')
        self.assertEqual(entry.getIcon(), 'nvim')
        self.assertTrue(entry.getTerminal())
        self.assertEqual(entry.get('X-AppImage-Version'), '1.2.3')

    def test_field_codes_preserved(self):
        entry = DesktopEntry(content=SAMPLE)
        self.assertIn('%F', entry.getExec())
        entry.set('Exec', 'app %f %u')
        self.assertEqual(entry.getExec(), 'app %f %u')

    def test_locale_keys_roundtrip(self):
        entry = DesktopEntry(content=SAMPLE)
        text = entry.get_text()
        self.assertIn('Name[zh_TW]=Neovim 編輯器', text)

    def test_actions_parsed_and_editable(self):
        entry = DesktopEntry(content=SAMPLE)
        actions = entry.Actions
        self.assertEqual(sorted(actions), ['new-window', 'preferences'])
        self.assertEqual(actions['new-window'].Exec, 'nvim --new-window')

        actions['new-window'].Exec = '/opt/app.appimage --new-window'
        actions['new-window'].Icon = '/opt/.icons/app.png'
        text = entry.get_text()
        self.assertIn('Exec=/opt/app.appimage --new-window', text)
        self.assertIn('Icon=/opt/.icons/app.png', text)

    def test_write_read_roundtrip(self):
        path = self._write_sample()
        entry = DesktopEntry(path=path)
        entry.TryExec = '/managed/app.appimage'
        entry.Exec = '/managed/app.appimage --flag'
        entry.Icon = '/managed/.icons/app.png'
        entry.Name.default_text = 'App (1.2.3)'
        entry.set_custom('X-AppImage-Name', 'App')
        entry.write_file(path)

        reloaded = DesktopEntry(path=path)
        self.assertEqual(reloaded.getTryExec(), '/managed/app.appimage')
        self.assertEqual(reloaded.getExec(),
                         '/managed/app.appimage --flag')
        self.assertEqual(reloaded.getIcon(), '/managed/.icons/app.png')
        self.assertEqual(reloaded.getName(), 'App (1.2.3)')
        self.assertEqual(reloaded.get('X-AppImage-Name'), 'App')
        # untouched keys survive
        self.assertEqual(reloaded.getComment(), 'Edit text files')
        self.assertEqual(reloaded.get('X-AppImage-Version'), '1.2.3')
        # mode must be world readable (desktop files)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)

    def test_newlines_never_leak_into_values(self):
        entry = DesktopEntry(content=SAMPLE)
        entry.set('Name', 'evil\n[Desktop Action bad]')
        text = entry.get_text()
        self.assertNotIn('\n[Desktop Action bad]', text)

    def test_terminal_default_false(self):
        entry = DesktopEntry(content='[Desktop Entry]\nName=X\n')
        self.assertFalse(entry.getTerminal())


if __name__ == '__main__':
    unittest.main()

# test_trash.py — FreeDesktop trash implementation tests.
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import unittest
import urllib.parse

from helpers import FakeXDGTestCase

from omarchy_appimage import trash


class TrashTests(FakeXDGTestCase):
    def _make_file(self, name: str, content: bytes = b'x') -> str:
        path = os.path.join(self.sandbox, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    def test_trash_creates_layout_and_info(self):
        path = self._make_file('foo.appimage')
        dest = trash.send_to_trash(path)

        self.assertTrue(os.path.isfile(dest))
        self.assertTrue(dest.startswith(self.trash_files))
        self.assertFalse(os.path.exists(path))

        info_path = os.path.join(self.trash_info, 'foo.appimage.trashinfo')
        self.assertTrue(os.path.isfile(info_path))
        with open(info_path) as f:
            content = f.read()
        self.assertIn('[Trash Info]', content)
        self.assertIn('Path=' + path, content)
        # DeletionDate must be ISO YYYY-MM-DDThh:mm:ss
        import re
        match = re.search(r'DeletionDate=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})',
                          content)
        self.assertIsNotNone(match)

    def test_path_percent_encoded(self):
        path = self._make_file('sp ace+&file.appimage')
        trash.send_to_trash(path)
        info_path = os.path.join(self.trash_info,
                                 'sp ace+&file.appimage.trashinfo')
        with open(info_path) as f:
            content = f.read()
        expected = urllib.parse.quote(path, safe='/')
        self.assertIn(f'Path={expected}', content)

    def test_name_collision_gets_suffix(self):
        first = self._make_file('foo.appimage', b'one')
        second = self._make_file('sub/foo.appimage', b'two')
        trash.send_to_trash(first)
        dest2 = trash.send_to_trash(second)

        self.assertEqual(os.path.basename(dest2), 'foo.appimage.2')
        self.assertTrue(os.path.isfile(os.path.join(
            self.trash_info, 'foo.appimage.2.trashinfo')))

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            trash.send_to_trash(os.path.join(self.sandbox, 'nope.appimage'))

    def test_trash_root_respects_xdg(self):
        self.assertEqual(trash.trash_root(),
                         os.path.join(self.data_home, 'Trash'))


if __name__ == '__main__':
    unittest.main()

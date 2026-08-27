# ini_config.py — per-app integration metadata (Python stdlib only).
#
# Derived from GearLever (c) mijorus, GPL-3.0.
# Ported from src/lib/ini_config.py; GLib.get_user_config_dir() is
# replaced by the XDG_CONFIG_HOME-aware helpers in constants.py and the
# config file lives in this plugin's own config directory (GearLever
# uses ~/.config/gearlever.conf). The on-disk section format
# ([app.<md5-of-file-path>] with name/file_path/default_exec_arguments
# keys) is kept identical to upstream.

import configparser
import hashlib
import logging
import os

from . import constants
from .utils import atomic_write


class Config:
    parser = configparser.ConfigParser(interpolation=None)

    @classmethod
    def path(cls) -> str:
        return constants.app_config_path()

    @classmethod
    def refresh(cls):
        cls.parser = configparser.ConfigParser(interpolation=None)
        if os.path.exists(cls.path()):
            try:
                cls.parser.read(cls.path(), encoding='utf-8')
            except Exception as e:
                logging.warning('Could not parse %s: %s', cls.path(), e)

    @classmethod
    def write(cls):
        import io
        buf = io.StringIO()
        cls.parser.write(buf)
        logging.debug('Writing config to %s', cls.path())
        atomic_write(cls.path(), buf.getvalue().encode('utf-8'))

    @staticmethod
    def get_app_hash(el) -> str:
        return hashlib.md5(el.file_path.encode()).hexdigest()

    @classmethod
    def get_app_config(cls, el) -> dict:
        cls.refresh()
        k = f'app.{cls.get_app_hash(el)}'
        if cls.parser.has_section(k):
            return dict(cls.parser[k])
        return {}

    @classmethod
    def delete_app_config(cls, el):
        cls.refresh()
        h = cls.get_app_hash(el)
        for section in (f'app.{h}', f'app.{h}.update_manager'):
            if cls.parser.has_section(section):
                logging.debug('Deleting config section %s', section)
                cls.parser.remove_section(section)
        cls.write()

    @classmethod
    def set_app_config(cls, el, data: dict):
        cls.refresh()
        h = cls.get_app_hash(el)
        data = dict(data)
        data['name'] = el.name
        data['file_path'] = el.file_path
        cls.parser[f'app.{h}'] = data
        cls.write()

    # update-manager sections are unused in the MVP (no update support),
    # but the structure is kept for the P1 --update work, mirroring upstream.

    @classmethod
    def delete_app_update_config(cls, el):
        cls.refresh()
        k = f'app.{cls.get_app_hash(el)}.update_manager'
        if cls.parser.has_section(k):
            cls.parser.remove_section(k)
        cls.write()

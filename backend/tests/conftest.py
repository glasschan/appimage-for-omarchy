# conftest.py — pytest path bootstrap (the suite itself is stdlib
# unittest; `python -m unittest discover` adds this directory to sys.path
# automatically, pytest needs this file to do the same so the tests can
# import the top-level `helpers` module).
#
# Derived from GearLever (c) mijorus, GPL-3.0. Test suite written for
# this plugin; integration behaviour verified against GearLever upstream.

import os
import sys

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

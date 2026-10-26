# commands — per-verb CLI command modules (ticket #56).
# Released under the GNU GPL-3.0 — see the repo's LICENSE for terms.
#
# One concern per module; the shared envelope helpers (JSON printing,
# option validation, target resolution, the app-object normaliser and the
# singletons every verb reads) live in envelope.py. cli.py stays the
# public import surface: it re-exports every name the tests and
# CONTRACT.md reference, so `omarchy_appimage.cli` keeps working.

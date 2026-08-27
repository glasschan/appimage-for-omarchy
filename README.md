# AppImage for Omarchy

[![CI](https://github.com/glasschan/appimage-for-omarchy/actions/workflows/ci.yml/badge.svg)](https://github.com/glasschan/appimage-for-omarchy/actions/workflows/ci.yml)

Manage AppImages from the Omarchy bar: integrate new ones into your app
menu, see what is installed (and running), launch them, and remove them —
all in a native Quickshell panel. No daemon, no extra runtime: the panel
is plain QML and the backend is a Python-stdlib CLI that runs only when
an action needs it.

Backend logic is derived from
[GearLever](https://github.com/mijorus/gearlever) (GPL-3.0, mijorus), so
this plugin is licensed under **GPL-3.0** as well — see [LICENSE](LICENSE).
UI icons are from [tabler-icons](https://github.com/tabler/tabler-icons)
(MIT, Paweł Kuna) — see [icons/LICENSE-TABLER.md](icons/LICENSE-TABLER.md).

## Status (MVP, F1–F5)

- **List (F1)** — panel shows integrated AppImages with name, version,
  extracted icon and a running dot. The list is cached in the shared
  store, so opening the panel renders instantly and rescans in the
  background (only when the cache is stale or empty — never blocking).
- **Integrate (F2)** — `Integrate` opens an inline picker: type a path
  or click an AppImage found in `~/Downloads` / `~/downloads`. The
  backend extracts the `.desktop` entry and icon, moves the file into
  `~/AppImages` and reports success, "already integrated", or the exact
  error. Quickshell ships no file dialog, hence the inline picker.
- **Remove (F3)** — two-click inline confirmation on the row (click once
  to arm "Remove?", again to trash). Targets the stable `desktop_id`, so
  two versions of the same app coexist cleanly.
- **Launch (F4)** — click the row (or Launch) to start the AppImage
  detached, with `DESKTOPINTEGRATION=1` like the generated desktop entry.
  The running dot refreshes shortly after launch.
- **Bar widget (F5)** — tabler package icon + installed count; updates live as
  the store changes (one backend probe at login seeds the count).

Errors are never silent: a missing `python3`, a hung backend, or invalid
JSON all surface as a dismissible banner in the panel (with an
`omarchy pkg add python` hint when the backend could not start).

## Requirements

- `python3` (the backend is standard-library only) — present on a normal
  Omarchy install; otherwise `omarchy pkg add python`.
- Nothing else. The AppImage is never executed to read its metadata.

## Install

From this repository (once published):

```sh
omarchy plugin add <repo-url> --enable
```

From a local checkout (runtime files only — no tests/docs/bytecode), use the
bundled scripts:

```sh
./install.sh      # sync into ~/.config/omarchy/plugins/ and rescan
./uninstall.sh    # disable, remove the plugin dir, hint a shell restart
```

For development, validate and load straight from a checkout:

```sh
omarchy plugin validate "$PWD"
omarchy plugin enable io.github.glasschan.appimage
omarchy-shell shell rescanPlugins
```

Then click the bar button, or:

```sh
omarchy-shell shell summon io.github.glasschan.appimage '{}'
```

## Backend CLI

The QML layer talks to `backend/main.py` over a stable JSON contract
(see [backend/CONTRACT.md](backend/CONTRACT.md)):

```sh
python3 backend/main.py --list-installed --json
python3 backend/main.py --integrate /path/to/App.AppImage --yes --json
python3 backend/main.py --remove <desktop-id> --yes --json
```

With `--json`, stdout is exactly one JSON document (logging goes to
stderr); `--json` requires `--yes` for integrate/remove.

## Plugin layout

```
manifest.json     Omarchy plugin manifest (bar-widget + panel)
BarWidget.qml     Bar entry: icon + count badge, summons the panel
Panel.qml         Main panel (list, picker, actions, states, Escape via PanelKeyCatcher)
ThemeIcon.qml     Theme-colored tabler icon (runtime currentColor tint)
lib/Model.js      Shared state store (items cache, counts, busy flags, JSON mapping)
lib/Backend.js    Process wrapper for the backend CLI (watchdog, tolerant JSON parse)
icons/            Bundled tabler SVGs (MIT) + LICENSE-TABLER.md
backend/          Python stdlib CLI (derived from GearLever)
```

## Releases / versioning

The version lives in `manifest.json`. To cut a release: bump that
version, commit, tag `vX.Y.Z`, then `gh release create vX.Y.Z`.

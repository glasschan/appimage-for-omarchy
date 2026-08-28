# AppImage for Omarchy

[![CI](https://github.com/glasschan/appimage-for-omarchy/actions/workflows/ci.yml/badge.svg)](https://github.com/glasschan/appimage-for-omarchy/actions/workflows/ci.yml)

Manage AppImages from the Omarchy bar: integrate new ones into your app
menu, see what is installed (and running), launch, update, and remove
them — all in a native Quickshell panel. No daemon, no extra runtime: the
panel is plain QML and the backend is a Python-stdlib CLI that runs only
when an action needs it.

Backend logic is derived from
[GearLever](https://github.com/mijorus/gearlever) (GPL-3.0, mijorus), so
this plugin is licensed under **GPL-3.0** as well — see [LICENSE](LICENSE).
UI icons are from [tabler-icons](https://github.com/tabler/tabler-icons)
(MIT, Paweł Kuna) — see [icons/LICENSE-TABLER.md](icons/LICENSE-TABLER.md).

## Status (F1–F9)

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
- **Bar widget (F5)** — tabler cube-unfolded icon + installed count; updates live as
  the store changes (one backend probe at login seeds the count). When
  updates are pending the badge turns urgent and counts them, and the
  tooltip switches to the pending-updates count.
- **Update checks (F6)** — `Check updates` in the panel header sweeps every
  app that has an update source: the AppImage's embedded `.upd_info` string
  (`gh-releases-zsync|…` → GitHub releases, `zsync|<url>` → static file) or
  a custom source (F9). Rows with a new release grow an
  "⬆ version available" marker.
- **One-click update (F7)** — the row's `Update` button downloads the
  release through the app's source and replaces the AppImage in place
  (same filename and desktop id, so the app-menu entry stays valid). A
  running app is skipped with a status message instead; `--keep-both` on
  the CLI keeps the old file next to the new one.
- **Background checks (F8)** — the plugin ships a service kind that sweeps
  ~5 minutes after login and then every 6 hours (defaults; both
  configurable, or off, in the panel's settings card). Genuinely new
  updates get one desktop notification per release — signatures are
  deduped in `updates-state.json`, and updating an app re-arms the
  notification for the next release.
- **Custom update sources (F9)** — per-app source editor on each app row
  or `--set-update-source` on the CLI: Static URL, GitHub, GitLab,
  Codeberg, or Forgejo. A custom source wins over the embedded
  `.upd_info`.

Errors are never silent: a missing `python3`, a hung backend, or invalid
JSON all surface as a dismissible banner in the panel (with an
`omarchy pkg add python` hint when the backend could not start).

## Requirements

- `python3` (the backend is standard-library only) — present on a normal
  Omarchy install; otherwise `omarchy pkg add python`.
- `notify-send` (libnotify) for the background-check desktop notification —
  present on Omarchy; a missing binary just means no notification, never
  a failure.
- Nothing else is required: `unsquashfs`/`bsdtar` (optional fallbacks for
  exotic squashfs compressions), `zstd` (zstd AppImages on Python < 3.14),
  `ps` and `update-desktop-database` are best-effort and their absence is
  never an error (see [backend/CONTRACT.md](backend/CONTRACT.md)). The
  AppImage is never executed to read its metadata.

## Install

From this repository:

```sh
omarchy plugin add https://github.com/glasschan/appimage-for-omarchy --enable
```

From a local checkout (runtime files only — no tests/docs/bytecode), use the
bundled scripts:

```sh
./install.sh      # sync into ~/.config/omarchy/plugins/ and rescan
./uninstall.sh    # disable, remove the plugin dir, hint a shell restart
```

One note when updating: if a release **adds a new entry point** (a new root
QML file — v0.2.0 added the background-check `Service.qml`), an
already-running shell will not pick it up through rescan alone; run
`omarchy restart shell` once after installing.

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
python3 backend/main.py --list-updates --json
python3 backend/main.py --update <desktop-id> --yes --json
python3 backend/main.py --set-update-source <desktop-id> --manager StaticFileUpdater url=https://example.com/app.AppImage --json
python3 backend/main.py --list-update-managers --json
python3 backend/main.py --fetch-updates --json
python3 backend/main.py --settings --json
python3 backend/main.py --set-setting update_check_interval_minutes=120 --json
```

With `--json`, stdout is exactly one JSON document (logging goes to
stderr); `--json` requires `--yes` for integrate/remove/update.

### Update sources

Every app's update source is resolved locally, before any network access:
a custom source from `apps.ini` wins, otherwise the AppImage's embedded
`.upd_info` ELF section is routed (`gh-releases-zsync|…` → GitHub,
`zsync|<url>` → static file; anything else → no source). The five managers
and the `key=value` config keys each accepts:

| Manager | Source | Config keys |
|---|---|---|
| `StaticFileUpdater` | Static URL | `url` |
| `GithubUpdater` | GitHub releases | `repo`, `repo_filename`, `allow_prereleases` |
| `GitlabUpdater` | GitLab releases | `repo_url`, `repo_filename` |
| `CodebergUpdater` | Codeberg releases | `repo`, `repo_filename`, `allow_prereleases` |
| `ForgejoUpdater` | Forgejo releases | `repo_url`, `repo_filename`, `allow_prereleases` |

Per-app sources live in
`$XDG_CONFIG_HOME/io.github.glasschan.appimage/apps.ini` (GearLever-
compatible INI); global settings in `…/settings.json` —
`update_check_enabled`, `update_check_interval_minutes` (minimum 15) and
`update_check_delay_minutes`, all writable via `--set-setting`.

## Plugin layout

```
manifest.json     Omarchy plugin manifest (bar-widget + panel + service)
BarWidget.qml     Bar entry: icon + count badge, summons the panel
Panel.qml         Main panel (list, picker, update rows, settings card, states, Escape via PanelKeyCatcher)
Service.qml       Background update checker (scheduled --fetch-updates sweeps)
ThemeIcon.qml     Theme-colored tabler icon (runtime currentColor tint)
lib/Model.js      Shared state store (items cache, counts, busy flags, JSON mapping)
lib/Backend.js    Process wrapper for the backend CLI (watchdog, tolerant JSON parse)
icons/            Bundled tabler SVGs (cube-unfolded, plus, refresh, trash, player-play, settings, arrow-up, cloud-download, pin; MIT) + LICENSE-TABLER.md
backend/          Python stdlib CLI (derived from GearLever)
```

## Releases / versioning

The version lives in `manifest.json`. To cut a release: bump that
version, commit, tag `vX.Y.Z`, then `gh release create vX.Y.Z`.

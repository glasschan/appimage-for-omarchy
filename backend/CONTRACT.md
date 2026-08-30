# CONTRACT.md — Backend CLI contract for `backend/main.py`

> Stable interface between the QML frontend (Quickshell `Process`) and the
> Python backend. The QML layer must only rely on what is documented here.
>
> Backend derived from GearLever (c) mijorus, GPL-3.0.

## Invocation

```sh
python3 <plugin_dir>/backend/main.py [OPTION]...
```

- Runtime: Python 3, **standard library only** (no third-party packages).
- External tools: none required for AppImage handling. `unsquashfs` /
  `bsdtar` are used as optional fallbacks for exotic squashfs compressions;
  `zstd` CLI is used only on Python < 3.14 for zstd-compressed AppImages;
  `update-desktop-database` and `ps` are called best-effort and failures are
  ignored. `notify-send` is used best-effort by `--fetch-updates` only
  (a missing binary just means no notification, never a failure).
- The AppImage is **never executed** (metadata is read with a built-in
  squashfs reader; updates download and re-integrate the new file).
- `OMARCHY_APPIMAGE_DEBUG=1` enables debug logging (stderr), including
  `--update` download progress.

## Output purity

- With `--json`, **stdout contains exactly one JSON document** and nothing
  else (single trailing newline). All logging and human-readable text goes
  to stderr.
- Without `--json`, human-readable output goes to stdout, errors to stderr.

## Commands

### `--list-installed [--json]`

Lists integrated apps. Plain text (name, version, path) or JSON.

For every app an update source is resolved **locally** (the per-app config
in `apps.ini`, else the AppImage's embedded `.upd_info` ELF section — no
network access), so `manager` / `embedded_source` are filled. An app whose
source comes from the embedded string reports `embedded_source: true` and
the routed manager name (e.g. `GithubUpdater` for a `gh-releases-zsync|...`
string). `available_version` / `download_size` stay `null` here; only
`--list-updates` populates them.

### `--integrate <path> [--yes] [--replace] [--json]`

Integrates an AppImage: extracts the `.desktop` entry + icon from the
embedded squashfs, copies the AppImage to the managed folder (chmod 755),
writes the desktop entry and icon, then **moves** (deletes) the original
file (GearLever's `move-appimage-on-integration` default).

- `--yes` is **required** with `--json` (no interactive prompts), otherwise
  exit code 2.
- Integrating a file whose content already matches an installed AppImage
  prints `already-integrated` and exits 0 (nothing is changed).
- Name conflict (same app, different content): default keeps both — the
  second copy gets a version-suffixed filename and a `Name (version)`
  desktop name (GearLever behaviour). `--replace` overwrites the existing
  installation in place instead.

### `--remove <target> [--yes] [--delete] [--json]`

Removes an integrated app. `<target>` is resolved in this order:

1. exact AppImage file path (e.g. `~/AppImages/neovim.appimage`)
2. desktop id, with or without `.desktop` (e.g. `neovim` or `neovim.desktop`)
3. desktop `Name`, case-insensitive (e.g. `Neovim (v0.11.3)`)

The AppImage, its `.desktop` file and its icon are moved to the FreeDesktop
trash (`$XDG_DATA_HOME/Trash`); `--delete` removes them permanently instead.
Per-app config entries (including the update source) are deleted either way.
**All commands taking a `<target>` use this same resolution order.**

### `--list-updates [--json]`

Checks every installed app that has an update source (custom config or
embedded `.upd_info`) for a new release. Apps without a source are skipped;
per-app errors are logged (stderr) and skipped.

- Offline (no connectivity): exit 1 — plain text prints
  `Internet connection not available` to stderr; `--json` prints the error
  document (see below).
- Plain text (real output):

  ```
  Neovim [Update available, GithubUpdater]   /home/user/AppImages/neovim.appimage
  ```

  or `No updates available` (exit 0).

- JSON (real example output):

```json
{"schema_version": 1, "updates": [{"name": "Neovim (v0.11.3)", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": 10996237, "manager": "StaticFileUpdater", "embedded_source": false, "running": false}]}
```

### `--update <target> [--yes] [--keep-both] [--force] [--json]`

Downloads the new release through the app's update source and installs it.

- `--json` requires `--yes` (exit 2 otherwise).
- No update source configured: exit 1, error
  `No update method was found for this AppImage (set one with --set-update-source)`.
- App currently running (detected via `ps -eo exe` or a `fuse.*` mount in
  `/proc/mounts`) and no `--force`: exit 0, result `skipped-running`.
- Availability check says "definitely up to date": exit 0, result
  `up-to-date`. An indeterminate state (network error, no matching asset —
  tri-state `None`) still attempts the download, like a `True` result.
- `--keep-both` installs the new release next to the old one (KEEP logic,
  version-suffixed filename); default replaces in place (REPLACE, same
  filename + desktop id, so the app-menu entry stays valid).
- On success the app's entry in `updates-state.json` is cleared so the next
  `--fetch-updates` does not re-notify for the now-installed release.

Real example output (success):

```json
{"schema_version": 1, "result": "updated", "message": "/home/user/AppImages/neovim.appimage was updated successfully", "app": {"name": "Neovim", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": null, "embedded_source": false, "running": false}, "downloaded_bytes": 10996237}
```

Real example output (up-to-date):

```json
{"schema_version": 1, "result": "up-to-date", "app": {"name": "Neovim", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": 10996237, "manager": "StaticFileUpdater", "embedded_source": false, "running": false}}
```

Real example output (skipped-running, exit 0):

```json
{"schema_version": 1, "result": "skipped-running", "message": "/home/user/AppImages/neovim.appimage was skipped because the application is running; use --force to override", "app": <app>}
```

### `--set-update-source <target> --manager <name> [key=value ...] [--unset] [--json]`

Sets or removes a per-app custom update source (stored in `apps.ini` under
`app.<md5-of-path>.update_manager`; it **wins over the AppImage's embedded
update string**).

- `--unset` removes the config: `{"schema_version": 1, "result": "unset", "app": <app>}`, exit 0.
- `<name>` must be one of the manager names from `--list-update-managers`,
  else exit 2.
- `key=value` pairs must match the manager's config keys **exactly**
  (missing or extra keys → exit 2 with the required list). Boolean keys
  accept `1/true/yes/on` and `0/false/no/off`. The manager's own
  validation runs last (e.g. a non-HTTP URL → exit 2).

Real example output (set):

```json
{"schema_version": 1, "result": "set", "app": {"name": "Neovim (v0.11.3)", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": "StaticFileUpdater", "embedded_source": false, "running": false}, "manager": "StaticFileUpdater", "config": {"url": "https://example.com/nvim.appimage"}}
```

### `--get-update-source <target> [--json]`

Reads back the per-app custom update source stored by `--set-update-source`
(`--list-installed` deliberately carries only the resolved manager name,
never the raw config — the panel's source editor pre-fills from this verb).
`<target>` resolves like `--remove` (unknown id → exit 1 error document).
Both result shapes exit 0:

Real example output (a source is configured, exit 0):

```json
{"schema_version": 1, "result": "ok", "manager": "StaticFileUpdater", "config": {"url": "https://example.com/nvim.appimage"}, "app": <app>}
```

No source configured (exit 0, the editor keeps its blank fields):

```json
{"schema_version": 1, "result": "no-source", "app": <app>}
```

Boolean config values come back normalized as `"true"`/`"false"` strings
(the on-disk layout `--set-update-source` stores).

### `--list-update-managers [--json]`

The available update managers and the config keys each accepts.

```json
{"schema_version": 1, "managers": [{"name": "StaticFileUpdater", "label": "Static URL", "config_keys": ["url"]}, {"name": "GithubUpdater", "label": "Github", "config_keys": ["repo", "repo_filename", "allow_prereleases"]}, {"name": "GitlabUpdater", "label": "Gitlab", "config_keys": ["repo_url", "repo_filename"]}, {"name": "CodebergUpdater", "label": "Codeberg", "config_keys": ["repo", "repo_filename", "allow_prereleases"]}, {"name": "ForgejoUpdater", "label": "Forgejo", "config_keys": ["repo_url", "repo_filename", "allow_prereleases"]}]}
```

Update-source semantics (ported from GearLever): `repo_filename` is a glob
matched against release asset names; when several assets match, the local
architecture is preferred; for GitHub, a `sha256:` asset digest is compared
against the local file when present, otherwise the size; `.zsync` assets use
the control file's `SHA-1:` line. The FTP updater is **not ported** (P2).

### `--fetch-updates [--json]`

One-shot background update check (F8) for the panel's timer service —
**never fails hard** (always exit 0 unless the usage itself is wrong).

Behaviour:

1. Offline → `{"schema_version": 1, "updates": [], "notified": false, "offline": true}` and the state file is not touched.
2. Otherwise the same checks as `--list-updates` run. Per-app signature =
   `"<available_version>|<download_size>"` (e.g. `"v0.12.0|11000000"`).
3. Updates whose signature **differs** from `updates-state.json` (or that
   are not in it) are "new". For new updates exactly **one** notification is
   sent, best-effort:

   ```sh
   notify-send -a 'AppImage for Omarchy' --expire-time=5000 \
       'AppImage updates available' '<N> update(s) available — click the AppImage icon in the bar.'
   ```

   `notified` is `true` only if `notify-send` ran and exited 0 (missing
   binary / non-zero exit → `false`, never an error).
4. The state file is saved with the signatures of **all** currently
   available updates (so unchanged updates do not re-notify).
5. `--update` clears the updated app's entry, which re-arms the
   notification for the next genuinely new release.

Real example output (update available, first run, notification delivered):

```json
{"schema_version": 1, "updates": [{"name": "Neovim (v0.11.3)", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": 10996237, "manager": "StaticFileUpdater", "embedded_source": false, "running": false}], "notified": true, "offline": false}
```

Second run with no change: same shape, `"notified": false`.

### `--settings [--json]`

Current settings (defaults merged with `settings.json`).

Real example output:

```json
{"schema_version": 1, "settings": {"appimages_default_folder": "~/AppImages", "manage_files_outside_default_folder": true, "move_appimage_on_integration": true, "update_check_enabled": true, "update_check_interval_minutes": 360, "update_check_delay_minutes": 5}}
```

Plain text: one `key: value` line per setting.

### `--set-setting <key>=<value> [--json]`

Changes exactly one setting. Unknown key or invalid value → exit 2.
Validation: booleans accept `1/true/yes/on` and `0/false/no/off`;
`update_check_interval_minutes` is an integer ≥ **15**
(`MIN_UPDATE_CHECK_INTERVAL`); `update_check_delay_minutes` is an integer
≥ 0; `appimages_default_folder` must be non-empty and start with `/` or `~`
(`~` is expanded at use time).

Real example output:

```json
{"schema_version": 1, "result": "set", "settings": {"appimages_default_folder": "~/AppImages", "manage_files_outside_default_folder": true, "move_appimage_on_integration": true, "update_check_enabled": true, "update_check_interval_minutes": 120, "update_check_delay_minutes": 5}}
```

## Other options

| Option | Meaning |
|---|---|
| `--json` | Machine-readable JSON on stdout (see schemas below) |
| `--yes` / `-y` | Skip interactive confirmation (required with `--json` for `--integrate` / `--remove` / `--update`) |
| `--replace` | Replace the conflicting app instead of keeping both (integrate) |
| `--keep-both` | Keep the old version when updating |
| `--force` | Update even while the app is running |
| `--delete` | Bypass the trash (irreversible) |
| `--help` / `-h` | Usage text, exit 0 |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including `--help`, "already integrated", `up-to-date`, `skipped-running`, and any `--fetch-updates` outcome) |
| 1 | Operation error (invalid file, not installed, extraction failed, download failed, ...) |
| 2 | Usage error (unknown option, missing argument, `--json` without `--yes`, invalid setting/manager config) |

No arguments prints usage and exits **2**.

## JSON schemas (schema_version 1)

### `<app>` object (all keys always present)

| Key | Type | Notes |
|---|---|---|
| `name` | string | Display name from the desktop entry (may contain a version suffix) |
| `path` | string | Absolute path of the managed AppImage |
| `desktop_id` | string or null | Basename of the `.desktop` file |
| `current_version` | string or null | `X-AppImage-Version` from the desktop entry |
| `available_version` | string or null | Release tag of the available update (`null` unless filled by a check; static-file sources have no version) |
| `download_size` | int or null | Byte size of the available update (`null` unless filled by a check) |
| `manager` | string or null | Update manager name (`StaticFileUpdater`, `GithubUpdater`, `GitlabUpdater`, `CodebergUpdater`, `ForgejoUpdater`) or `null` when no source exists |
| `embedded_source` | boolean | True when the source comes from the AppImage's embedded `.upd_info` string (false for custom configs and for no source) |
| `running` | boolean | True when the AppImage is currently running (via `ps -eo exe` **or** as the mount source of a `fuse.*` mount in `/proc/mounts`) |

### `--list-installed --json`

```json
{"schema_version": 1, "installed": [<app>, ...]}
```

Real example output (app with an embedded `gh-releases-zsync` string; the
custom-config case looks the same with `embedded_source: false`):

```json
{"schema_version": 1, "installed": [{"name": "Neovim", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": "GithubUpdater", "embedded_source": true, "running": false}]}
```

Empty list:

```json
{"schema_version": 1, "installed": []}
```

### `--integrate <path> --yes --json` (success)

```json
{"schema_version": 1, "result": "integrated", "message": "<path> was integrated successfully", "app": <app>}
```

Other `result` values: `already-integrated` (exit 0, app unchanged).

### `--remove <target> --yes --json` (success)

```json
{"schema_version": 1, "result": "removed", "message": "<path> was removed successfully", "app": <app>}
```

### Error documents (any command with `--json`, exit code != 0)

```json
{"schema_version": 1, "result": "error", "error": "<human-readable message>"}
```

Real example output:

```json
{"schema_version": 1, "result": "error", "error": "AppImage not integrated: 'ghost' (see --list-installed for valid names/ids)"}
```

## Filesystem layout

| Path (resolved via) | Content |
|---|---|
| `$XDG_DATA_HOME/applications` (default `~/.local/share/applications`) | Generated `.desktop` entries |
| `$XDG_DATA_HOME/Trash/{files,info}` | FreeDesktop trash used by `--remove` |
| `~/AppImages` (managed folder) | Integrated AppImages |
| `~/AppImages/.icons` | Extracted icons (referenced by absolute path in the desktop entry) |
| `$XDG_CONFIG_HOME/io.github.glasschan.appimage/settings.json` | User settings |
| `$XDG_CONFIG_HOME/io.github.glasschan.appimage/apps.ini` | Per-app metadata (`[app.<md5>]`) and update sources (`[app.<md5>.update_manager]`, `manager` + config keys, booleans stored as `true`/`false`), INI format compatible with GearLever |
| `$XDG_CONFIG_HOME/io.github.glasschan.appimage/updates-state.json` | Last background-check signatures, `{desktop_id: "available_version|download_size"}` (desktop id falls back to the AppImage basename) |

### XDG override behaviour (required for tests/sandboxes)

`XDG_DATA_HOME` and `XDG_CONFIG_HOME` are always honoured. The managed
folder is `$HOME/AppImages` (like GearLever's default) and follows `$HOME`;
it can be overridden with `appimages_default_folder` in `settings.json`
(`~` is expanded). Tests isolate everything by setting `XDG_DATA_HOME`,
`XDG_CONFIG_HOME` and `HOME` to sandbox directories.

### settings.json keys (all optional, all writable via `--set-setting`)

| Key | Default | Meaning |
|---|---|---|
| `appimages_default_folder` | `"~/AppImages"` | Managed folder |
| `manage_files_outside_default_folder` | `true` | List apps whose AppImage lives outside the managed folder |
| `move_appimage_on_integration` | `true` | Delete the original file after integrating |
| `update_check_enabled` | `true` | Master switch for the background update check (the service should not call `--fetch-updates` when false) |
| `update_check_interval_minutes` | `360` | How often the service checks (minimum 15) |
| `update_check_delay_minutes` | `5` | Delay before the first check after startup |

## Update-source selection (GearLever semantics)

1. A custom source set with `--set-update-source` **wins**.
2. Otherwise the AppImage's embedded `.upd_info` ELF section routes:
   `gh-releases-zsync|user|repo|channel|filename-glob` → `GithubUpdater`,
   `zsync|<http url>` → `StaticFileUpdater`. Anything else → no source.
3. The embedded `.upd_info` is read with a pure-Python ELF section parser
   (`readelf` is not used). The FTP updater is **not ported** (P2).

## Desktop entry details (GearLever-compatible)

Generated entries look like (real output for the neovim AppImage):

```ini
[Desktop Entry]
Name=Neovim (v0.11.3)
...
TryExec=/home/user/AppImages/neovim.appimage
Exec=env DESKTOPINTEGRATION=1 /home/user/AppImages/neovim.appimage %F
Icon=/home/user/AppImages/.icons/neovim.png
X-AppImage-Version=v0.11.3
X-AppImage-Name=Neovim
```

- `Exec` keeps the original arguments/field codes (`%F` etc.); the
  `DESKTOPINTEGRATION=1` env var is appended like GearLever does.
- `[Desktop Action ...]` groups are preserved and rewritten to the managed
  path.
- Locale keys (`Name[xx]`, `Comment[xx]`, ...) are preserved verbatim.
- Writes are atomic (`tempfile` + `os.replace`), mode 0644.
- A REPLACE update rewrites the same files in place (same AppImage filename
  and desktop id), so the app-menu entry stays valid — verified by
  `UpdateFlowTests` in `backend/tests/test_cli.py`.

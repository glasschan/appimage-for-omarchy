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
- External tools: none required. `unsquashfs` / `bsdtar` are used as
  optional fallbacks for exotic squashfs compressions; `zstd` CLI is used
  only on Python < 3.14 for zstd-compressed AppImages; `update-desktop-database`
  and `ps` are called best-effort and failures are ignored.
- The AppImage is **never executed** (metadata is read with a built-in
  squashfs reader).
- `OMARCHY_APPIMAGE_DEBUG=1` enables debug logging (stderr).

## Output purity

- With `--json`, **stdout contains exactly one JSON document** and nothing
  else (single trailing newline). All logging and human-readable text goes
  to stderr.
- Without `--json`, human-readable output goes to stdout, errors to stderr.

## Commands

### `--list-installed [--json]`

Lists integrated apps. Plain text (name, version, path) or JSON.

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
Per-app config entries are deleted either way.

## Other options

| Option | Meaning |
|---|---|
| `--json` | Machine-readable JSON on stdout (see schemas below) |
| `--yes` / `-y` | Skip interactive confirmation |
| `--replace` | Replace the conflicting app instead of keeping both |
| `--delete` | Bypass the trash (irreversible) |
| `--help` / `-h` | Usage text, exit 0 |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including `--help`, and "already integrated") |
| 1 | Operation error (invalid file, not installed, extraction failed, ...) |
| 2 | Usage error (unknown option, missing argument, `--json` without `--yes`) |

No arguments prints usage and exits **2**.

## JSON schemas (schema_version 1)

### `--list-installed --json`

```json
{"schema_version": 1, "installed": [<app>, ...]}
```

`<app>` object (all keys always present):

| Key | Type | Notes |
|---|---|---|
| `name` | string | Display name from the desktop entry (may contain a version suffix) |
| `path` | string | Absolute path of the managed AppImage |
| `desktop_id` | string or null | Basename of the `.desktop` file |
| `current_version` | string or null | `X-AppImage-Version` from the desktop entry |
| `available_version` | null | Reserved for P1 update support |
| `download_size` | null | Reserved for P1 update support |
| `manager` | null | Update manager name — always null in the MVP |
| `embedded_source` | false | Reserved for P1 update support |
| `running` | boolean | True when the AppImage is currently running (detected via `ps -eo exe` **or** as the mount source of a `fuse.*` mount in `/proc/mounts`, which is how FUSE-launched type-2 AppImages show up) |

Real example output (captured from an actual run):

```json
{"schema_version": 1, "installed": [{"name": "Neovim (v0.11.3)", "path": "/tmp/appimage-e2e/home/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": null, "embedded_source": false, "running": false}]}
```

Empty list:

```json
{"schema_version": 1, "installed": []}
```

### `--integrate <path> --yes --json` (success)

```json
{"schema_version": 1, "result": "integrated", "message": "<path> was integrated successfully", "app": <app>}
```

Real example output:

```json
{"schema_version": 1, "result": "integrated", "message": "/tmp/appimage-e2e/home/AppImages/neovim.appimage was integrated successfully", "app": {"name": "Neovim (v0.11.3)", "path": "/tmp/appimage-e2e/home/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": null, "embedded_source": false, "running": false}}
```

Other `result` values: `already-integrated` (exit 0, app unchanged).

### `--remove <target> --yes --json` (success)

```json
{"schema_version": 1, "result": "removed", "message": "<path> was removed successfully", "app": <app>}
```

Real example output:

```json
{"schema_version": 1, "result": "removed", "message": "/tmp/appimage-e2e/home/AppImages/neovim.appimage was removed successfully", "app": {"name": "Neovim (v0.11.3)", "path": "/tmp/appimage-e2e/home/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": null, "embedded_source": false, "running": false}}
```

### Error documents (any command with `--json`, exit code != 0)

```json
{"schema_version": 1, "result": "error", "error": "<human-readable message>"}
```

Real example output:

```json
{"schema_version": 1, "result": "error", "error": "AppImage not integrated: 'nope' (see --list-installed for valid names/ids)"}
```

## Filesystem layout

| Path (resolved via) | Content |
|---|---|
| `$XDG_DATA_HOME/applications` (default `~/.local/share/applications`) | Generated `.desktop` entries |
| `$XDG_DATA_HOME/Trash/{files,info}` | FreeDesktop trash used by `--remove` |
| `~/AppImages` (managed folder) | Integrated AppImages |
| `~/AppImages/.icons` | Extracted icons (referenced by absolute path in the desktop entry) |
| `$XDG_CONFIG_HOME/io.github.glasschan.appimage/settings.json` (default `~/.config/...`) | User settings |
| `$XDG_CONFIG_HOME/io.github.glasschan.appimage/apps.ini` | Per-app metadata (e.g. `default_exec_arguments`), INI format compatible with GearLever's sections |

### XDG override behaviour (required for tests/sandboxes)

`XDG_DATA_HOME` and `XDG_CONFIG_HOME` are always honoured. The managed
folder is `$HOME/AppImages` (like GearLever's default) and follows `$HOME`;
it can be overridden with `appimages_default_folder` in `settings.json`
(`~` is expanded). Tests isolate everything by setting `XDG_DATA_HOME`,
`XDG_CONFIG_HOME` and `HOME` to sandbox directories.

### settings.json keys (all optional)

| Key | Default | Meaning |
|---|---|---|
| `appimages_default_folder` | `"~/AppImages"` | Managed folder |
| `manage_files_outside_default_folder` | `true` | List apps whose AppImage lives outside the managed folder |
| `move_appimage_on_integration` | `true` | Delete the original file after integrating |

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

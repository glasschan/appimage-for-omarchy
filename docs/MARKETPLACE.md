# Marketplace submission — "AppImage for Omarchy"

Plugin id `io.github.glasschan.appimage` · public repo `https://github.com/glasschan/appimage-for-omarchy`

## Submission link

Open the form (title pre-filled via URL; GitHub issue forms do not support
prefilling the other fields — select them in the browser):

```
https://github.com/HANCORE-linux/omarchy-plugin-marketplace/issues/new?template=submit-plugin.yml&title=%5BPlugin%5D%3A%20AppImage%20for%20Omarchy
```

`labels=submission` is applied automatically by the template.

## Ready-to-paste answers (field labels are quoted verbatim from the template)

- **"Repository URL"** — `https://github.com/glasschan/appimage-for-omarchy`
- **"Category"** (dropdown, single, case-sensitive) — select **`System`**.
  The plugin manages installed applications (integrate/list/launch/update/remove
  AppImages) and runs a background service; none of the other options
  (Appearance, Desktop, Developer Tools, Hardware, Productivity, Widgets, Other)
  fit a system-level package manager. Note: this is the marketplace taxonomy —
  our own `barWidget.category: "Apps"` in manifest.json is unrelated.
- **"Tags"** (dropdown, multi-select — max three, more are rejected) — select
  exactly: **`Bar`** (bar widget kind), **`Quickshell`** (Quickshell/QML UI),
  **`System`** (package management). There is no AppImage/package tag.
- **"Suggest a missing tag"** (optional, free text) — `appimage`
- **"Maintainer notes"** (optional textarea) —

  ```text
  Requires python3 only (backend is Python standard-library, no pip deps);
  README documents `omarchy pkg add python` as fallback. Install via
  ./install.sh (copies runtime files into
  ~/.config/omarchy/plugins/io.github.glasschan.appimage only, refuses to
  write outside $HOME); remove via ./uninstall.sh (disables plugin, deletes
  only its own plugin dir). Run `omarchy restart shell` once after install.
  License: GPL-3.0 (derivative of GearLever, also GPL-3.0); LICENSE at root,
  GPL headers in all backend sources, Tabler icon license in
  icons/LICENSE-TABLER.md. Install, remove, click handling, Escape-to-close,
  and enable/disable lifecycle verified by a real-machine e2e run on
  2026-08-28.
  ```

- **"Submission checklist"** (all five are required checkboxes) — tick all:
  1. *"...public and contains installation and removal instructions."* —
     README `## Install` documents `./install.sh` and `./uninstall.sh`.
  2. *"...documented the plugin license and any external dependencies."* —
     README states GPL-3.0 + GearLever attribution and lists `python3`
     under `## Requirements`.
  3. *"...own or have permission to submit this plugin and its preview assets."* —
     glasschan is the sole author (manifest `author`).
  4. *"...does not overwrite user configuration without explicit consent."* —
     install.sh/uninstall.sh write only their own plugin dir under `$HOME`
     (hard-fail guards); destructive backend actions require `--yes`.
  5. *"...approval is for listing and is not a security review."* — acknowledged.

## What the automated validation will check

The marketplace's `validate-submission.yml` workflow triggers on the issue
(`submission` label / `[Plugin]:` title) and runs `scripts/validate-submission.mjs`
against the current default-branch commit:

- Repo must be **public, active, unarchived** (GitHub API metadata check) — ours is.
- Exactly **one manifest named `manifest.json` at the repo root** — ours is at root.
- Manifest schema ("Quattro compatibility"): `schemaVersion` must be `1`;
  non-empty string `id`/`name`/`version`/`author`/`description` within field
  limits (id 128, name 120, version 64, author 120, description 500 chars);
  optional `license` (120); lowercase namespaced id, no reserved `omarchy.*`;
  `kinds` ⊆ {bar, bar-widget, menu, overlay, panel, service}; an entryPoint per
  kind, safe relative paths; `barWidget.defaultSection` ∈ left/center/right.
  → All satisfied by our manifest.json; our CI job `plugin-manifest` already
  enforces required keys, entryPoint existence, exact filename case, and the
  id constraints.
- **Entry points must exist in the git tree; no symlinks** (mode 120000)
  anywhere in the plugin folder → BarWidget.qml / Panel.qml / Service.qml all
  present; our CI fails on any symlink.
- **Root README** (any case) and **root LICENSE/COPYING** required → present.
- Optional **root preview**: `preview.png|jpg|jpeg|webp|avif`, input capped at
  50 MB / 40 MP; marketplace strips metadata and regenerates card images —
  no manual resizing needed. We ship `preview.png`.
- **Uniqueness**: repository and plugin id must not already be listed; retired
  ids are never reusable → `io.github.glasschan.appimage` is namespaced and new.
- The report records the exact commit SHA; approval is per-commit and
  explicitly not a security review (plugins run as unsandboxed upstream code).

## Pre-submit checklist

1. [ ] `preview.png` exists at the repo root and is committed (currently absent locally).
2. [ ] Commit and push all local work to `main` — a `.github/workflows/ci.yml`
       edit is sitting uncommitted; validation scans the pushed commit only.
3. [ ] Tag `v0.2.0` on the release commit and push the tag (origin currently
       has only `v0.1.0`); manifest `version` is already `0.2.0`.
4. [ ] Green CI on the pushed commit (backend, frontend-js, plugin-manifest).
5. [ ] Open the issue via the prefilled URL above and tick the five checkboxes.

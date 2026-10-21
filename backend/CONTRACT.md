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
  (a missing binary just means no notification, never a failure), and
  `ldconfig -p` is used best-effort by `--list-installed` only (a missing
  binary or a non-zero exit just degrades the fuse2 probe to the library
  directory globs — never a failure). `gdbus` and `zenity` are probed and
  used by `--pick-file` only (a missing binary is a typed `unavailable`
  result, never a failure).
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

Lists integrated apps. Plain text (name, version, path) or JSON. Runs the
janitor sweep first (see *Document lifecycle* under `--update --async`)
— best-effort, it can never fail the listing.

For every app an update source is resolved **locally** (the per-app config
in `apps.ini`, else the AppImage's embedded `.upd_info` ELF section — no
network access), so `manager` / `embedded_source` are filled. An app whose
source comes from the embedded string reports `embedded_source: true` and
the routed manager name (e.g. `GithubUpdater` for a `gh-releases-zsync|...`
string). `available_version` / `download_size` stay `null` here; only
`--list-updates` populates them.

The listing is desktop-driven: a record in `apps.ini` whose `.desktop` file
is gone is **not** listed as installed. It is logged on stderr instead
(`stale integration record: the desktop file <path> is gone; orphaned
AppImage: <path> (remove it with --remove <desktop id>)`), so the user can
reclaim the file — the record itself is removed only on an explicit
`--remove <desktop id> --yes`.

`running` and `running_pids` are computed per app from the same process
view and by the same identity rule (Process identity under Security
model), so the two keys of one object can never disagree about what is
ours: `running: true` with `running_pids: 0` means the `fuse.*` mount is
still attributed to the file while no process of ours answers —
`running_pids` is display-only and no command ever reads it back.

**`runtime` block (fuse2).** Every successful JSON envelope also carries an
additive `runtime` block: `{"fuse2": <state>}` with exactly one of

| State | Meaning |
|---|---|
| `present` | the `libfuse.so.2` shared object was found (type-2 AppImages can launch) |
| `missing` | the probe ran to a decision and the library is absent |
| `unknown` | the probe could not decide (see below) — never an error |

The probe is best-effort and cheap (one linker-cache read + a few globs,
once per call, never per app). Signal order: the dynamic-linker cache
(`ldconfig -p`) first — a `libfuse.so.2` entry is decisive; otherwise the
standard library directories (`lib/x86_64-linux-gnu`, `lib64`, `lib` under
`/`, `/usr`, `/usr/local`) are globbed for `libfuse.so.2*` — a hit is
decisive too (even when the cache phase failed). No glob hit after a
*successful* cache read reports `missing`; a failed cache phase (binary
absent, non-zero exit) with no glob hit reports `unknown`. Probing never
errors the call: the block is always present and any unexpected probe
failure is logged and reported as `unknown`. Consumers must treat
`unknown` as "no claim" (no fuse2 hint may be shown unless the state is
exactly `missing`).

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

`<path>` may also be an **https URL**. The URL is classified (first match
wins) and routed to a download/verify path; after a successful install the
matching per-app update source is auto-populated in `apps.ini` (an existing
source is **never overwritten** — a manual `--set-update-source` choice
wins):

| # | URL form | Download | Digest | Auto-source |
|---|---|---|---|---|
| 1 | `github.com/<o>/<r>/releases/download/<tag>/<f>` | asset | `sha256:` **required** (GitHub API) | `GithubUpdater {repo, repo_filename, allow_prereleases=false}` |
| 2 | any host, path ends `.zsync` | control file's target | `SHA-1:` from the control file when present | `StaticFileUpdater {url: <the .zsync url>}` |
| 3 | host `gitlab.com` | GitLab release API asset | none (reduced) | `GitlabUpdater {repo_url, repo_filename}` |
| 4 | host `codeberg.org` | Gitea API on codeberg.org | none (reduced) | `CodebergUpdater {repo, repo_filename, allow_prereleases=false}` |
| 5 | other host, `/releases/download/<tag>/<f>` | Gitea API at that host | none (reduced) | `ForgejoUpdater {repo_url, repo_filename, allow_prereleases=false}` |
| 6 | anything else (plain https URL) | the pasted URL | none (reduced) | `StaticFileUpdater {url: <the pasted url>}` |

`repo_filename` is the asset filename with the release's version (tag minus
a leading `v`) rewritten to a `*` glob, so a future release still matches;
if the version isn't in the name the exact filename is kept (`GithubUpdater`
case included).

**Version of a URL integrate (v0.9.4).** `X-AppImage-Version` is the only
version source, and real AppImages ship without it (Terax, OpenTypeless).
When the downloaded image states no version of its own, the **release tag**
the URL names (cases 1 and 3–5 — from the URL itself or the release API;
never invented for cases 2 and 6) is recorded as the app's version: it is
written to the installed desktop entry's `X-AppImage-Version` (and to the
keep-both `Name (version)` suffix) by `provider.apply_release_version`, so
the app is listed with the release it actually is instead of with no
version at all. An image that states its own `X-AppImage-Version` always
wins. `--update` does the same with the tag of the release its manager just
downloaded (`provider.update_from_url`), which also repairs an install made
by an older version of the plugin.

**Relaxed digest rule (deliberate deviation, `--integrate <url>` only).**
For the verbs in this section a cryptographic digest is verified whenever
the source publishes one — GitHub `sha256:` (required) and a `.zsync`
`SHA-1:` line are **not weakened** — but a URL is **never refused merely
because no digest exists**. In that case the artifact is downloaded, then
accepted only if it passes the AppImage magic-byte check and its byte count
equals the advertised size when one is known (release-API `size`; an HTTP
`Content-Length` short body already fails inside the transport), and a
WARNING records that assurance was reduced. If the release-API lookup for
cases 3–5 fails (404/network) the pasted URL is installed as a case-6
static source instead of erroring. **`--update` and the update managers are
unaffected**: they keep the strict mandatory-digest policy below, and the
auto-populated cases 3–5 sources therefore still refuse to install updates.

### `--preview-integrate <path-or-url> [--json]`

The offline pre-install preview document: what `--integrate` would do,
without installing, downloading or executing anything. Read-only and
**advisory** — the integrate command remains the single authority and
re-classifies/re-verifies at its own runtime (a file can change between
preview and confirm; nothing is cached or trusted across the two).

- **Zero network**: classification is by URL shape (the exact classifier
  of `--integrate <url>`, first match wins, same source `kind` values)
  and no request is ever made — release metadata, digests and `.zsync`
  control files are only fetched at integrate time. Fully usable offline.
- **The AppImage is never executed.** For local files the embedded
  desktop entry is read in place (stdlib squashfs reader, bounded by the
  same superblock quotas extraction uses, nothing written to disk).
- A missing path, a non-regular file, a non-AppImage file and a
  non-https `scheme://` URL (integrate's egress policy) produce the
  typed error document (exit 1).
- Read-only, so `--json` does **not** require `--yes` here.
- `--json` is the only option this verb takes: any other
  `--`-prefixed argument is a usage error (exit 2), never ignored.

Verification matrix — what integrate WILL verify, so a source that
installs without a publisher-attested digest says so before the
download starts:

| source `kind` | `verification.method` | `reduced_assurance` |
|---|---|---|
| `local` | `none` | `false` — no download; the AppImage magic-byte check only |
| `github` | `github-sha256` | `false` — API digest mandatory at integrate time |
| `zsync` | `zsync-sha1` | `false` — SHA-1 when the control file publishes one, else the reduced checks; the control file is only readable at integrate time |
| `gitlab` / `codeberg` / `forgejo` / `static` | `none` | `true` — reduced assurance |

Flags: `already_integrated` is true when the file's content already
matches an installed AppImage (integrate would print
`already-integrated` and change nothing; `destination.path` is then the
installed file itself). `name_conflict` is true when another version of
the same app is installed (integrate keeps both — the derived
`destination` already carries the version-suffixed, non-clobbering file
name — or replaces it with `--replace`).

Plain text (no `--json`): a short human-readable preview block.

The document schema is under "JSON schemas" below; keys are additive
within `schema_version` 1 (same policy as `exec_arguments`).

### `--remove <target> [--yes] [--delete] [--json]`

Removes an integrated app. `<target>` is resolved in this order:

1. exact AppImage file path (e.g. `~/AppImages/neovim.appimage`)
2. desktop id, with or without `.desktop` (e.g. `neovim` or `neovim.desktop`)
3. desktop `Name`, case-insensitive (e.g. `Neovim (v0.11.3)`)
4. the desktop id of a **stale record** — an `apps.ini` record whose
   `.desktop` file is gone (see `--list-installed`)

The AppImage, its `.desktop` file and its icon are moved to the FreeDesktop
trash (`$XDG_DATA_HOME/Trash`); `--delete` removes them permanently instead.
Per-app config entries (including the update source) are deleted either way.
**All commands taking a `<target>` use this same resolution order.**

Nothing is ever removed automatically: a stale record is only cleaned up
when the user asks for it explicitly (`--remove <desktop id> --yes`).

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
{"schema_version": 1, "updates": [{"name": "Neovim (v0.11.3)", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": 10996237, "manager": "StaticFileUpdater", "embedded_source": false, "running": false, "running_pids": 0, "exec_arguments": "%F"}]}
```

### `--update <target> [--yes] [--keep-both] [--force] [--kill-running] [--json]`

Downloads the new release through the app's update source and installs it.

- `--json` requires `--yes` (exit 2 otherwise).
- No update source configured: exit 1, error
  `No update method was found for this AppImage (set one with --set-update-source)`.
- App currently running (the `running` flag, `utils.is_app_running`: a
  `ps` `exe` equal to the AppImage path, or a `fuse.*` mount in
  `/proc/mounts` attributed to it — what may then actually be *signalled*
  is narrower, see Process identity under Security model), and neither
  `--force` nor `--kill-running` given: exit 0, result `skipped-running`,
  and **no process is signalled**. An interactive terminal is asked one
  `y/N` question first — the update pipeline's only interactive question,
  skipped by `--yes` or `--json`; a non-TTY (`--async`'s grandchild, a
  script) gets the result straight away with a message that names the fix:
  `... was skipped because the application is running; pass
  --kill-running to close it and update, or --force to replace it in
  place`. `--keep-both` is exempt: it installs beside the running file,
  so the guard neither asks nor skips (see its bullet below).
- **The availability check comes first.** The target is asked whether
  there is anything to install *before* any process is touched: a
  `up-to-date` target returns exit 0 with result `up-to-date`, never
  closes the app (not even with `--kill-running`), never asks the
  question and never records `force_quit`. When there *is* an update, the
  close still happens **before the download starts**, so nothing is
  ever downloaded for an app that cannot be replaced. When the check
  itself cannot run (no reachable source) the refusal needs no knowledge
  of the update, so a non-interactive, no-grant run still answers
  `skipped-running`; a granted run, or one that would ask the question,
  propagates the error instead of silently skipping.
- **`--kill-running` vs `--force`** — the two ways past that guard, and
  they differ in exactly one respect: whether the running app is closed.
  - `--kill-running` closes the app, then updates, and **never asks**:
    the flag is the consent, on a terminal as much as in a script. The
    grant is unconditional for the whole job and covers two moments: at
    job start (fail fast — nothing is downloaded for an app that cannot
    be replaced) and again immediately before the atomic replace, so a
    process relaunched during the download is closed too. Processes are
    picked by the process-identity rule (`procs.pids_for`) and signalled
    `SIGTERM` first, escalating to `SIGKILL` after a grace period
    (`procs.quit_app`). What was actually closed is recorded in the job
    document as `force_quit` (see Job documents); the sync `--json`
    result does not carry it.
  - A target that **survives** the round — another user's process, or one
    that ignores both signals — makes the update refuse rather than
    replace: exit 1, error `<path> is still running before the update
    start: process(es) <pid>, ... could not be closed; refusing to
    replace the file` (the stage is `the atomic replace` when the
    survivor appeared during the download and the pre-replace re-check
    caught it). Replacing the file under a live process while the panel
    claimed the app was closed is the failure this refuses; the pids in
    the message are the ones the user has to close by hand.
  - `--force` replaces the AppImage **while the app keeps running**, in
    place, without closing anything. Existing processes keep the old
    inode open until they exit; the file on disk is the new release
    immediately.
  - Both given: the explicit close request wins, i.e. `--kill-running`
    behaviour.
- Availability check says "definitely up to date": exit 0, result
  `up-to-date`. An indeterminate state (network error, no matching asset —
  tri-state `None`) still attempts the download, like a `True` result.
- Authenticity policy (v0.4.2): installation only proceeds for sources
  that expose a cryptographic digest (GitHub asset digest, zsync SHA-1
  control-file line). A source without one (plain static URL, GitLab,
  Codeberg, Forgejo) fails the download with a clear refusal message —
  exit 1, error document — even though the availability check may still
  report the update.
- `--keep-both` installs the new release next to the old one (KEEP logic,
  version-suffixed filename); default replaces in place (REPLACE, same
  filename + desktop id, so the app-menu entry stays valid). `--keep-both`
  together with `--kill-running` accepts the flag but **closes nothing**:
  the new file lands beside the running one and `os.replace` never touches
  the running app's path, so both the job-start quit and the pre-replace
  hook are skipped and the document carries no `force_quit`. The guard
  itself is skipped too — no question, no `skipped-running` — because
  nothing is replaced under the running app; without `--keep-both` the
  guard applies exactly as documented above.
- On success the app's entry in `updates-state.json` is cleared so the next
  `--fetch-updates` does not re-notify for the now-installed release.

Real example output (success):

```json
{"schema_version": 1, "result": "updated", "message": "/home/user/AppImages/neovim.appimage was updated successfully", "app": {"name": "Neovim", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": null, "embedded_source": false, "running": false, "running_pids": 0, "exec_arguments": "%F"}, "downloaded_bytes": 10996237}
```

Real example output (up-to-date):

```json
{"schema_version": 1, "result": "up-to-date", "app": {"name": "Neovim", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": 10996237, "manager": "StaticFileUpdater", "embedded_source": false, "running": false, "running_pids": 0, "exec_arguments": "%F"}}
```

Real example output (skipped-running, exit 0):

```json
{"schema_version": 1, "result": "skipped-running", "message": "/home/user/AppImages/neovim.appimage was skipped because the application is running; pass --kill-running to close it and update, or --force to replace it in place", "app": <app>}
```

### `--update <target> --async [--yes] [--keep-both] [--force] [--kill-running] [--json]`

Runs the whole `--update` pipeline as a **detached background job** and
returns immediately.

- Same target resolution and flags as plain `--update`; `--json` still
  requires `--yes` (exit 2 otherwise).
- The app's desktop id must be usable as a job file name
  (`^[A-Za-z0-9][A-Za-z0-9._-]*$` after the `.desktop` suffix is
  stripped). A **legal** `.desktop` filename may violate that (a space, a
  non-ASCII char); plain `--update` accepts it, `--async` cannot name a
  job file for it and refuses **before any side effect** (no sweep, no
  fork) with exit 1 and the error document `<path> cannot be updated in
  the background: its desktop id <id> is not usable as a job file name
  (allowed: letters, digits, ".", "_" and "-", starting with a letter
  or digit)`.
- **Atomic claim, then the liveness check.** The parent opens the
  per-job log and takes an exclusive non-blocking `flock` on that fd
  *before* it consults any document; the claim is held by the live
  grandchild (the inherited open file description survives the `dup2`
  onto its stdout/stderr) and released by the kernel when it dies, so
  there is no claim file to go stale. A second start is thereby refused
  even when both start before either job document exists (two starts
  used to pass the active-document check and truncate/write the same
  log). If an **active** job document already exists for the app
  (liveness rule below), the command refuses too: exit 1, error document
  `an update job is already active for <desktop id> (pid <pid>); refusing
  to start a second one` (pid omitted when only the claim said so) — the
  running job's document is left untouched and its log is never reopened
  for writing.
- Otherwise the updater detaches as a grandchild (double `fork()`,
  `setsid`, stdin from `/dev/null`, stdout+stderr into the per-job log)
  and the parent reports `job-started` and exits **0** immediately; the
  job survives the invoking process. No shell, no exec: the grandchild
  continues the same process image.
- Everything else plain `--update` reports — the running guard
  (`skipped-running`), `up-to-date`, the download, digest verification,
  the atomic replace, and the pending-notification-marker drop — happens
  **inside the job** through the same shared update core, and is
  published as the job document. A missing update source therefore
  surfaces as a terminal `error` document, not a spawn-time failure.
- `--kill-running` is passed down to the grandchild and applies for the
  job's whole life, including the re-check before the replace. The
  grandchild's stdin is `/dev/null`, so **it** never asks — but `--async`
  is no longer implicitly non-interactive: when stdin is a TTY and the app
  is running and neither `--yes` nor `--keep-both` is given, the **parent**
  asks the same `y/N` question before it spawns anything, and a yes
  appends `--kill-running` to the grandchild's argv. A no (or Ctrl-C, or
  EOF) starts no job at all and answers like the sync path: exit 0,
  result `skipped-running` with the message that names the two flags.
  `--yes` and `--json` never ask, and `--keep-both` never asks because
  nothing is replaced in place.
- The surviving-target refusal (see `--kill-running` above) applies inside
  the job too: it surfaces as a terminal `error` document that still
  carries the `force_quit` record of whatever *was* closed.

Real example output (job-started, exit 0):

```json
{"schema_version": 1, "result": "job-started", "desktop_id": "neovim.desktop", "job_path": "/home/user/.local/state/io.github.glasschan.appimage/jobs/neovim.json", "log_path": "/home/user/.local/state/io.github.glasschan.appimage/jobs/neovim.log", "pid": 12345}
```

#### Job documents

One JSON document per app at
`$XDG_STATE_HOME/io.github.glasschan.appimage/jobs/<desktop id stem>.json`
(`$XDG_STATE_HOME` is honoured like `XDG_DATA_HOME`/`XDG_CONFIG_HOME`,
default `~/.local/state`). Every rewrite is atomic (temp file +
`os.replace`) — on every phase change and progress tick, plus a heartbeat
refresh at least every 2 s (`JOB_HEARTBEAT_INTERVAL_SECONDS`).
`schema_version` stays **1**.

| Key | Type | Notes |
|---|---|---|
| `desktop_id` | string | The app's desktop id (e.g. `neovim.desktop`) |
| `phase` | string | Writer-set: `starting` / `downloading` / `verifying` / `replacing` / `done` / `error`. Terminal: `done`, `error`. `verifying` is best-effort: sources without a `Content-Length` go straight from `downloading` to `replacing` |
| `progress` | float | Download fraction 0.0–1.0 (1.0 from `replacing` on) |
| `total_bytes` | int or null | Download size declared at the availability check; `null` when unknown. For `.zsync` sources that is the control file's size, so mid-download `downloaded_bytes` are approximate — the terminal value is exact |
| `downloaded_bytes` | int | Derived from the fraction while downloading; exact once terminal |
| `pid` | int | The detached job's process id |
| `started`, `heartbeat` | float | Epoch seconds; `heartbeat` refreshes on every rewrite |
| `message` | string | Human-readable phase/status text |
| `status` | string or null | Terminal result: `updated` / `up-to-date` / `skipped-running` / `error`; `null` while running |
| `new_version` | string or null | Installed release (`updated` jobs) |
| `error` | string or null | Error text (`error` jobs); the traceback tail goes to the log, never into the document |
| `force_quit` | object or absent | **Additive** record of the running-app grant: `{"count": n, "sigkill": bool}` — how many processes this job signalled and whether any needed `SIGKILL`. Present **only** when the job actually closed something (`--kill-running`, or the consented question); absent in every other document, including a granted job that found nothing to close **and every `up-to-date` document** (nothing was applied, so no close can be claimed — the finishing writer drops a stale record). `count` is the whole job's: the processes signalled by the job-start quit plus those the pre-replace re-check had to close, i.e. every pid that got a `SIGTERM`/`SIGKILL`, not just the ones still alive at the end. The record is written on **terminal `error` documents too** — a job that closed the app and then failed is exactly the case where the user needs to know. The field never changes `status`, `phase` or `schema_version`, and the CLI's `--json` result does not carry it; when it is present the panel's banner appends its closed-app clause (`Model.js` `jobDoneBanner` for `updated`, `jobErrorBanner` for `error`), and a `sigkill: true` picks the force-quit wording over the plain close wording |

**Liveness rule:** a job is **ACTIVE** if and only if its phase is
non-terminal **and** its `pid` is alive **and** its `heartbeat` is
fresher than `JOB_STALE_AFTER_SECONDS` = **120 s** (comfortably above the
downloader's 30 s per-read stall guard). `interrupted` is **never
written**: it is the reader-side classification for a non-terminal
document that fails the rule (dead pid or stale heartbeat); unparsable
documents classify the same way.

The per-job log lives next to the document (`<stem>.log`, append mode)
and is size-capped at 256 KiB (`JOB_LOG_MAX_BYTES`): an over-cap log is
truncated by the next job that legitimately opens it (i.e. only under
the held claim, so a refused start can never wipe a live job's log), and
the running job drops it back to empty whenever it passes the cap
(`JobWriter.enforce_log_cap`, driven by the 2 s heartbeat thread) — the
log therefore always holds the most recent phase and never grows
without bound.

#### Document lifecycle (janitor sweep, additive)

Every `--list-installed` and every async job start (in the spawning
**parent**, before the fork — never inside the grandchild) runs a
best-effort sweep that **never fails the command** and removes,
silently (debug log only — there is no expiry notification):

- **`--list-installed` is throttled**: it runs on the panel's 10 s poll
  and a sweep is an unbounded `listdir` + `stat` walk of the shared temp
  dir in the CLI's foreground, so the listing hook sweeps at most once
  per `janitor.SWEEP_THROTTLE_SECONDS` = **300 s**
  (`janitor.sweep_throttled()`, recorded in
  `$XDG_STATE_HOME/io.github.glasschan.appimage/last-sweep` — the plugin
  state dir, never the temp area the sweep cleans). A **missing or
  corrupt marker sweeps** (the throttle is an optimisation, never a
  precondition). An async job start still sweeps **unconditionally**
  (`janitor.sweep()`), and both entry points share the same removal
  semantics.

- a **terminal** (`done`/`error`) or **`interrupted`** document — together
  with its `<stem>.log` — once `JOB_ACK_WINDOW_SECONDS` = **24 h** have
  passed since its last activity. The window clock is the document's
  `heartbeat` (refreshed on every rewrite, so a terminal document's
  heartbeat is its finished-at time and a dead job's is its last sign of
  life); a document without a usable `heartbeat` (corrupt JSON, malformed
  field) ages out from its **file mtime** under the same window.
- an **`appimage-update-*` directory** in the temp area whose *effective
  mtime* — its own mtime vs. the newest of its immediate entries (a live
  download keeps writing its one file, so the file's mtime is the live
  signal while the directory's own mtime goes stale at entry creation) —
  is older than `UPDATE_TEMP_GRACE_SECONDS` = **600 s** (5× the 120 s
  liveness threshold, 20× the downloader's 30 s per-read stall guard).
  This disk-only rule reclaims legacy orphans from hard-killed pre-async
  downloads, which carry neither a registry entry nor a job document.
- the same rule for an **`omarchy-integrate-*` directory**
  (`janitor.INTEGRATE_TEMP_PREFIX`, the `--integrate <url>` per-run
  `mkdtemp`). A clean exit removes it in `cmd_integrate`'s `finally`, but
  a **signalled** child skips that: the panel's Quickshell `Process` is
  killed with the panel QML object when the panel closes, mid-download.
  The sweep is what reclaims the truncated multi-MiB AppImage it leaves.
  Both prefixes live in `janitor.UPDATE_TEMP_PREFIXES`; no other prefix
  (`gearlever_appimage_*` above all) is ever touched, and symlinks are
  never followed.

**ACTIVE documents are never removed.** Readers may therefore rely on:
a document they can read survives at least until 24 h after its last
heartbeat; `read_job() == None` means "no job known" (never expired, and
an expired one disappears rather than reading as `interrupted`).
Removals are capped per sweep (`janitor.MAX_REMOVALS_PER_SWEEP` = 200)
so a pathological state dir cannot grow unbounded removal work (the scan itself reads each document once).

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
{"schema_version": 1, "result": "set", "app": {"name": "Neovim (v0.11.3)", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": "StaticFileUpdater", "embedded_source": false, "running": false, "exec_arguments": "%F"}, "manager": "StaticFileUpdater", "config": {"url": "https://example.com/nvim.appimage"}}
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
architecture is preferred; for GitHub, a `sha256:` asset digest is
compared against the local file when present (otherwise the size —
availability stays informative either way, only installation demands a
digest); `.zsync` sources use the control file's `SHA-1:` line. The FTP
updater is **not ported** (P2).

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
{"schema_version": 1, "updates": [{"name": "Neovim (v0.11.3)", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": 10996237, "manager": "StaticFileUpdater", "embedded_source": false, "running": false, "running_pids": 0, "exec_arguments": "%F"}], "notified": true, "offline": false}
```

Second run with no change: same shape, `"notified": false`.

### `--pick-file [--json]`

Opens a native **single-file, AppImage-filtered** file dialog and prints
the picked file. The backend **orchestrates, it does not dialog**: the
dialog lives in child processes, so a crashed or hung dialog can never
take down the caller (the panel's backend process). This verb never
mutates anything, so it works with or without `--yes`.

Probe order (first available wins):

1. **Desktop portal** — `org.freedesktop.portal.FileChooser.OpenFile`
   through the glib DBus CLI, when `gdbus` exists and introspection of
   `/org/freedesktop/portal/desktop` exposes the FileChooser interface
   (the probe is side-effect free: probing never opens a dialog). The
   call is **monitor-then-call**: a `gdbus monitor --session --dest
   org.freedesktop.portal.Desktop` child is started first, then the
   `gdbus call` runs — the method reply only carries the request handle
   (an object path, pinned by a constant `handle_token`), the actual
   answer arrives as the `Request.Response` signal on the monitored
   stream, matched by that handle. Portal response code 1 (user cancel)
   → `cancelled`; any other non-zero code is a typed error.
2. **zenity** — `zenity --file-selection` (single file: no `--multiple`)
   with an `AppImage | *.AppImage *.appimage` file filter. Exit 1 =
   `cancelled`.

A portal **error** falls through to zenity (a broken portal must not cost
the user the feature); an explicit user **cancel** never does (reopening
a second dialog after a cancel is never wanted). Neither backend
available → typed `unavailable` result (exit 0) so the panel keeps
today's path-field UX.

**Discipline** (same as every other child process, see the security
model): one absolute whole-operation deadline of **600 s**
(`DEFAULT_DEADLINE_SECONDS` in `omarchy_appimage/pick_file.py`) covers
probe, call, signal wait and zenity together — a stuck dialog yields a
typed timeout error, never a hang. One-shot children run through the
bounded-output `run_command` (10 MiB/stream cap, own process group,
killpg reap); the monitor child also runs in its own process group, is
always reaped (killpg) and its stream is read line-by-line with a
per-line cap of 1 MiB. Subprocess argv is fully constant — the dialog
title is a constant, nothing user-controlled is ever interpolated, no
shell is involved; the picked path is returned as JSON data only.

The picked portal URI is validated: `file://` scheme only, empty or
`localhost` host, percent-decoded to an absolute plain path. Anything
else (https, remote share) is a typed `not-local` error.

Result payloads (all exit 0 except `error`):

```json
{"schema_version": 1, "result": "picked", "path": "/home/user/Downloads/nvim-linux-x86_64.appimage", "backend": "portal"}
{"schema_version": 1, "result": "cancelled", "backend": "zenity"}
{"schema_version": 1, "result": "unavailable", "message": "no file chooser is available (neither the desktop portal nor zenity); type or paste the path instead"}
```

`backend` is `"portal"` or `"zenity"` (on `picked` / `cancelled`).

Typed error documents (exit 1) add a `reason` key — `timeout`
(whole-operation deadline exceeded), `backend-failed` (the dialog
crashed, ended without answering, returned nothing, or its binary is
gone) or `not-local` (non-`file://` or remote selection):

```json
{"schema_version": 1, "result": "error", "reason": "timeout", "error": "the file dialog did not answer in time"}
```

Plain text (no `--json`): `picked` prints the path, `cancelled` /
`unavailable` print their message, errors go to stderr.

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
| `--force` | Replace the AppImage even while the app is running, without closing it |
| `--kill-running` | On `--update` / `--update --async`: close the running app instead of skipping it — the flag is the consent, no question is asked (see `--update`). Ignored by `--keep-both`, which installs beside the running file and closes nothing |
| `--async` | With `--update`: run the whole update as a detached background job and report `job-started` immediately (see the `--update --async` section) |
| `--delete` | Bypass the trash (irreversible) |
| `--help` / `-h` | Usage text, exit 0 |

## Security model (marketplace security-review hardening)

- **https-only egress** — every request URL must be `https://`; plain
  `http://` update-source configs are rejected by validation, and the
  transport refuses anything else (`file`, `ftp`, ...).
- **SSRF guard** — each hostname is resolved locally and *every* returned
  address must be a global (public) IP; loopback, private, link-local,
  CGNAT and reserved targets are unreachable. The socket is pinned to a
  validated address (TLS still verifies the hostname), which removes the
  DNS-rebinding resolve/connect window. Redirects are followed manually
  (max 5 hops) and re-validated per hop.
- **Byte caps** — metadata responses are capped at 4 MiB, downloads at
  4 GiB; caps are enforced against Content-Length *and* the running body
  size mid-stream. A failed download never leaves a partial file behind.
- **Absolute deadlines** — request timeouts are whole-operation
  wall-clock budgets, not just per-socket-operation stall guards:
  metadata requests get one absolute budget (20 s), downloads get a
  per-read stall guard (30 s) plus an absolute deadline of 1 h, so a
  slow-drip server cannot keep a request or a multi-GiB transfer alive
  indefinitely. Known residual: the response-header phase is bounded
  only by the per-operation socket timeout (an http.client structural
  limit); the absolute budget covers the body phase.
- **Mandatory digest verification (fail closed)** — an artifact is only
  installed after verifying it against a publisher-attested
  *cryptographic* digest; advertised-size equality alone never
  authorizes an install:
  - *GitHub* — the release asset's `sha256:<64 hex>` digest is
    **required**; a missing or malformed digest refuses before any byte
    is downloaded. When the embedded `gh-releases-zsync` flow selected a
    `.zsync` twin, the control file's `SHA-1:` line is additionally
    cross-checked.
  - *zsync static sources* — installable only through a `.zsync` control
    file with a `SHA-1:` line (the AppImageSpec's publisher-attested
    binding for the target artifact). SHA-1 rationale: the control file
    is published by the same party as the artifact, so a collision
    attack requires publisher cooperation — which would equally defeat
    any digest scheme, sha256 included (the publisher also controls the
    release metadata). The binding's security rests on second-preimage
    resistance, against which SHA-1 remains unbroken; no such claim is
    made for sizes.
  - *Plain static URLs, GitLab, Codeberg, Forgejo* — these sources
    expose no digest metadata, so installation **refuses outright**
    (no download is attempted). The tri-state availability checks stay
    fully informative; only installation fails closed.
  A verification failure raises an error and the partial artifact is
  removed.
- **Atomic install** — the AppImage and its icon are written through an
  exclusive mkstemp descriptor next to their destination, fchmod'd,
  fsync'd, then `os.replace()`d into place; a planted symlink or FIFO at
  the destination is replaced, never written through. The source is
  realpath-resolved and opened no-follow (a symlinked AppImage still
  integrates; non-regular sources are refused).
- **Containment-bound uninstall** — paths from the (mutable) desktop
  entry are only removed when bound to the app: equal to the path
  recorded at install time in `apps.ini`, or inside the managed-folder
  layout (icon additionally must match the desktop id's stem). Directories
  and FIFOs are never removed.
- **Extraction quotas** — external extractors list the image first and
  refuse to extract past 20 000 entries / 512 MiB (fail closed); the
  extracted tree is sanitized afterwards (symlinks escaping the temp root
  are unlinked, quotas re-checked).
- **Process identity** — a process belongs to an AppImage only when its
  `/proc/<pid>/exe` proves it: the AppImage path itself (the type-2 runtime
  parent) or a path under a `fuse.*` mount that `/proc/mounts` attributes
  to this AppImage (`procs.pids_for`). `/proc/mounts` only ever holds the
  AppImage's **basename** as the mount source, never the absolute path, so
  `mount_points_for` matches on basenames; a stale entry whose mount is
  gone attributes nothing. Process names, desktop `Exec` lines and
  `/proc/<pid>/environ` are never used (the runtime parent exports no
  `APPIMAGE`/`APPDIR` to itself and a mount child's environ reads as
  zeroed) — a wrong kill is worse than a missed one, so nothing else is
  ever signalled. Quitting (`procs.quit_app`) sends one SIGTERM round to
  the parent and its mount children, then SIGKILLs only the survivors
  after the grace; a process it may not signal is reported and left alone.
  The identity is **re-proved before every signal round**: one `ps` scan
  collects the set, and each pid's `/proc/<pid>/exe` is read again
  immediately before it is signalled (both rounds), because a pid can be
  recycled in between. A pid that no longer proves membership — the exact
  AppImage path for a parent, a path under the resolved mount for a
  child, both realpath-normalised — is reported as `identity-changed`,
  never signalled, and does not count as a survivor. The scan itself is
  one bounded `ps -eo pid=,exe=` (10 s), never an unbounded one.
  A grant to quit is CLI-level and explicit — `--kill-running`, or a yes
  to the one interactive question — and it is the only thing that makes
  the update signal anything: without it the command reports
  `skipped-running` and sends no signal at all, and `--force` replaces
  under the running app without touching it.
- **Bounded subprocess output** — helper commands' stdout/stderr are
  capped at 10 MiB per stream; past the cap the reader stops and the
  timeout reaps a still-writing child.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including `--help`, "already integrated", `up-to-date`, `skipped-running`, any `--fetch-updates` outcome, and `--pick-file` `cancelled` / `unavailable` results) |
| 1 | Operation error (invalid file, not installed, extraction failed, download failed, file-dialog timeout/crash, ...) |
| 2 | Usage error (unknown option, missing argument, `--json` without `--yes`, invalid setting/manager config) |

No arguments prints usage and exits **2**.

## JSON schemas (schema_version 1)

### `<app>` object (all keys always present)

| Key | Type | Notes |
|---|---|---|
| `name` | string | Display name from the desktop entry (keep-both entries carry a ` (<version>)` suffix, added only when the image really reports a version) |
| `path` | string | Absolute path of the managed AppImage |
| `desktop_id` | string or null | Basename of the `.desktop` file |
| `current_version` | string or null | `X-AppImage-Version` from the desktop entry — **the only version source**: an image that ships none reports `null`. GearLever's md5-prefix fallback is gone, so a hash is never presented as a version (v0.9.3; it used to reach the desktop `Name` as `Terax (77efdf)`) |
| `available_version` | string or null | Release tag of the available update (`null` unless filled by a check; static-file sources have no version) |
| `download_size` | int or null | Byte size of the available update (`null` unless filled by a check) |
| `manager` | string or null | Update manager name (`StaticFileUpdater`, `GithubUpdater`, `GitlabUpdater`, `CodebergUpdater`, `ForgejoUpdater`) or `null` when no source exists |
| `embedded_source` | boolean | True when the source comes from the AppImage's embedded `.upd_info` string (false for custom configs and for no source) |
| `running` | boolean | True when the AppImage is currently running. Computed by **one rule shared with `running_pids`** (`utils._running_by_ps` → `procs.pids_for`'s `parent` list, plus the `fuse.*` mount signal for the runtime that has no attributable exe): a process whose `/proc/<pid>/exe` is the AppImage path (compared through `os.path.realpath`, so a symlinked install directory matches either way round), or a `fuse.*` mount in `/proc/mounts` attributed to it (matched on the mount source's basename — see Process identity under Security model). Because the mount signal can outlive the processes, `running: true` with `running_pids: 0` is possible: the mount is still there, no process of ours is |
| `running_pids` | int | How many processes belong to this AppImage: the runtime parent (its `exe` is the AppImage path) **plus** the helpers running out of the `fuse.*` mount attributed to it — the set `--kill-running` would close (`procs.pids_for`). `0` when the app is not running (a non-running row is never scanned) and `0` when the count cannot be determined (a failed scan is a warning, never an error, and never flips `running`). **Display-only**: the panel's prompt names it; no command reads it back. A listing builds one process table (and one `/proc/mounts` read) and reuses it for every running row, because the panel polls the listing every 10 s |
| `exec_arguments` | string | The desktop entry's `Exec=` arguments, e.g. `"--no-sandbox %U"` or `"%U"`; `""` when the app has none. The desktop field codes (`%f %F %u %U %i %c %k`) stay **verbatim** — stripping them is the UI's job. The current desktop entry wins; when it carries no arguments, the install-time `default_exec_arguments` from `apps.ini` is used instead |

> `exec_arguments` and `running_pids` are **additive** keys: every other
> key keeps its meaning, and a reader that ignores unknown keys (the QML
> layer does) is unaffected — the same way `manager` / `embedded_source` /
> `download_size` were added. `schema_version` therefore stays **1**.

### `--list-installed --json`

```json
{"schema_version": 1, "installed": [<app>, ...], "runtime": {"fuse2": "present" | "missing" | "unknown"}}
```

> `runtime` is an **additive** envelope key (see the `runtime` block
> section under `--list-installed` above): every other key keeps its
> meaning and a reader that ignores unknown keys is unaffected —
> `schema_version` therefore stays **1**.

Real example output (app with an embedded `gh-releases-zsync` string; the
custom-config case looks the same with `embedded_source: false`):

```json
{"schema_version": 1, "installed": [{"name": "Neovim", "path": "/home/user/AppImages/neovim.appimage", "desktop_id": "neovim.desktop", "current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": "GithubUpdater", "embedded_source": true, "running": false, "running_pids": 0, "exec_arguments": "%F"}], "runtime": {"fuse2": "present"}}
```

Empty list:

```json
{"schema_version": 1, "installed": [], "runtime": {"fuse2": "missing"}}
```

### `--integrate <path> --yes --json` (success)

```json
{"schema_version": 1, "result": "integrated", "message": "<path> was integrated successfully", "app": <app>}
```

Other `result` values: `already-integrated` (exit 0, app unchanged).

### `--preview-integrate <path-or-url> --json`

```json
{"schema_version": 1, "result": "preview", "source": {"kind": "local", "input": "<path-or-url as given>"}, "verification": {"method": "github-sha256|zsync-sha1|none", "reduced_assurance": true|false, "note": "<human-readable sentence for the panel>"}, "destination": {"folder": "<managed folder, absolute>", "filename": "<file name integrate would pick>" | null, "path": "<folder>/<filename>" | null}, "app": {"name": "...", "version": "..."} | null, "already_integrated": false, "name_conflict": false, "advisory": true}
```

| Key | Meaning |
|---|---|
| `source.kind` | `local`, `github`, `zsync`, `gitlab`, `codeberg`, `forgejo` or `static` — the same classification `--integrate <url>` uses |
| `verification` | The matrix above; `note` is the exact sentence the panel may render verbatim |
| `destination` | For a local file: folder + exact file name + full path, derived with the install-time naming helper (conflict suffixing included; `path` is the installed file itself when `already_integrated`). For a URL: only `folder` is known — `filename`/`path` are `null`, the final name comes from the artifact's embedded desktop entry at integrate time |
| `app` | Local files only: `name`/`version` read in place from the embedded desktop entry; `null` when the entry cannot be read in place (the preview stays valid) or for URLs (nothing was downloaded) |
| `already_integrated` | Content already matches an installed AppImage |
| `name_conflict` | Another version of the same app is installed |
| `advisory` | Always `true`; the integrate command re-classifies/re-verifies at its own runtime |

Real example output (local file):

```json
{"schema_version": 1, "result": "preview", "source": {"kind": "local", "input": "/home/user/Downloads/nvim.appimage"}, "verification": {"method": "none", "reduced_assurance": false, "note": "no download: the local file itself is integrated (AppImage magic-byte check only)"}, "destination": {"folder": "/home/user/AppImages", "filename": "neovim.appimage", "path": "/home/user/AppImages/neovim.appimage"}, "app": {"name": "Neovim", "version": "v0.11.3"}, "already_integrated": false, "name_conflict": false, "advisory": true}
```

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

`--pick-file` error documents additionally carry a typed `reason` key
(`timeout` / `backend-failed` / `not-local`); every other key keeps its
meaning, so readers that ignore unknown keys are unaffected
(`schema_version` stays 1).

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
| `$XDG_STATE_HOME/io.github.glasschan.appimage/jobs/` | Background job documents (`<desktop id stem>.json`) and their size-capped logs (`<stem>.log`) written by `--update --async` |

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

- The `Name` suffix exists so the *launcher* shows the version next to the
  app; it is written only when `X-AppImage-Version` is present. **No md5
  hash is ever substituted for a missing version** (v0.9.3): the panel
  strips a trailing ` (<version>)` from a row's title because the row's
  meta line already prints the version.

- `Exec` keeps the original arguments/field codes (`%F` etc.); the
  `DESKTOPINTEGRATION=1` env var is appended like GearLever does.
- `[Desktop Action ...]` groups are preserved and rewritten to the managed
  path.
- Locale keys (`Name[xx]`, `Comment[xx]`, ...) are preserved verbatim.
- Writes are atomic (`tempfile` + `os.replace`), mode 0644.
- A REPLACE update rewrites the same files in place (same AppImage filename
  and desktop id), so the app-menu entry stays valid — verified by
  `UpdateFlowTests` in `backend/tests/test_cli.py`.

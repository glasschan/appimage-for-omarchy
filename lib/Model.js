.pragma library

// Shared store for the AppImage plugin: `.pragma library` makes this one
// engine-wide instance shared by the bar widget and every panel. A JS
// library gets no property-change notifications, so mutations go through
// the functions below and are announced to `subscribe()` listeners.

// ---- UI strings (single table; future i18n swaps this object) -------------

var strings = {
  barTooltip: "AppImages",
  barTooltipCount: "AppImages (%1 installed)",
  panelTitle: "AppImages",
  integrate: "Integrate",
  refresh: "Refresh",
  launch: "Launch",
  removeTooltip: "Remove",
  removeConfirm: "Remove?",
  dismiss: "Dismiss",
  loading: "Loading…",
  emptyTitle: "No AppImages integrated",
  emptyHint: "Download an AppImage, then press Integrate to add it to your app menu.",
  runningLabel: "running",
  itemsOne: "1 AppImage",
  itemsMany: "%1 AppImages",
  runningCount: "%1 running",
  timeout: "Backend timed out",
  backendFailed: "Backend call failed",
  // Launch failure (F4): spawn success says nothing about the AppImage
  // runtime, so only a post-launch listing can claim a failure. The fuse2
  // hint is appended ONLY when that same listing reports fuse2 "missing";
  // text only, naming the library, never a command or button (repo wording
  // rule; the security gates scan install-command sources).
  launchFailed: "%1 did not start.",
  launchFuse2Hint: "The most common cause is a missing fuse2 runtime (the libfuse.so.2 library).",
  // Spawn failures only: "never ran at all". Watchdog timeouts get
  // strings.timeout instead. Hedged "may be missing" on purpose: this
  // string also shows when the backend command itself is empty.
  backendMissing: "Backend did not run — python3 may be missing. Reopen the panel and use the Install Python card.",
  // Python dependency card: python3 is the backend's only runtime dep and
  // Omarchy does not guarantee it. Installing a system package is the
  // user's call, so the panel never runs the install itself — the button
  // opens a floating terminal running `/usr/bin/omarchy pkg add python`
  // (sudo prompt included) and polls the result.
  pythonMissingTitle: "Python 3 is required",
  pythonMissingHint: "The backend is a Python script, and python3 is not on this system. "
    + "Nothing is installed without your okay: this panel shows the exact command "
    + "it will run (/usr/bin/omarchy pkg add python), and the button below opens a "
    + "visible terminal that runs it for you to authorize.",
  installPython: "Install Python",
  installingPython: "Installing Python…",
  pythonReady: "Python installed — loading your AppImages",
  pythonInstallCanceled: "Installation was canceled.",
  pythonInstallFailed: "Python installation did not finish. Check the Omarchy terminal and try again.",
  pythonInstallWaiting: "Installation is still waiting. Check the Omarchy terminal and try again.",
  // Prep step died or hung before the terminal ever opened, so the
  // terminal-focused copies above would point at a terminal that does not
  // exist. Nothing was installed and nothing changed.
  pythonInstallPrepFailed: "Could not open the installer terminal — nothing was installed or changed. Try again.",
  // Covers both "unset/empty" and "present but not a private absolute
  // dir" — the same wording the prep script's exit 5 maps to.
  pythonNoRuntimeDir: "The runtime directory is missing or not a private absolute path (owner-only 0700) — install refused.",
  integrateWorking: "Integrating…",
  // Shown on the next open when the panel went away (closed, reloaded)
  // while an integrate was still running: the destroyed panel takes its
  // backend child with it, so the download vanishes mid-transfer.
  integrateInterrupted: "Integration was interrupted — the panel closed before it finished. Nothing was installed; try again.",
  // A URL integrate streams progress documents and the status line follows
  // them. integrateWorking stays the pre-first-event text and the whole
  // status story of a local-path integrate, which never streams.
  integrateDownloadingPct: "Downloading %1%…",
  integrateDownloadingBytes: "Downloading %1 MiB…",
  integrateVerifying: "Verifying…",
  integrateInstalling: "Installing…",
  statusIntegrated: "%1 integrated",
  statusAlreadyIntegrated: "Already integrated",
  pickerTitle: "Integrate an AppImage",
  pickerPlaceholder: "Path to a .AppImage file, or a GitHub release URL…",
  pickerBrowse: "Browse…",
  pickerBrowsing: "Browsing…",
  pickerDownloads: "From Downloads",
  pickerDownloadsEmpty: "No AppImages found in Downloads",
  pickerGo: "Integrate",
  pickerCancel: "Cancel",
  // --pick-file outcomes: cancelled stays subtle; unavailable is a status,
  // never an error. Typed errors (timeout / backend-failed / not-local)
  // surface through the backend's own message and only fall back to
  // pickerPickFailed when a document arrives without one.
  pickCanceled: "No file chosen",
  pickerUnavailable: "No file chooser is available — type or paste the path instead.",
  pickerPickFailed: "The file dialog failed",
  // Confirm-before-integrate preview: the picker first shows what
  // --integrate would do and only an explicit confirm runs the real
  // command. The verification.note is rendered verbatim; the flags drive
  // the reduced-assurance line and the already-integrated notice.
  previewChecking: "Reading…",
  previewTitle: "Ready to integrate",
  previewAppUnknown: "Unknown",
  previewAppUnknownUrl: "Unknown until download",
  previewVersion: "version %1",
  previewDestinationLater: "final file name decided at integrate time",
  previewReducedAssurance: "Will install with reduced assurance",
  previewAlreadyIntegrated: "Already integrated — confirming changes nothing (the backend reports it as already installed).",
  previewNameConflict: "Another version of this app is installed — integrate keeps both.",
  previewConfirm: "Confirm & integrate",
  previewKeysHint: "Enter confirms · Esc goes back",
  // Drop-to-integrate: a drop only seeds the picker and runs the same
  // preview flow as every other route — it never installs. dropBusy is the
  // ignore-with-status line for a drop racing an in-flight call (nothing is
  // queued); the other two cover the typed rejections of
  // Model.dropUriToPath.
  dropHint: "Drop to integrate",
  dropIgnoredOne: "1 other dropped file ignored",
  dropIgnoredMany: "%1 other dropped files ignored",
  dropBusy: "Busy — drop the file again once the current action finishes.",
  dropNotFile: "That is not a local AppImage file — drop a file from your file manager.",
  dropNoAppImage: "No AppImage in the dropped selection — drop a .AppImage file.",
  // F6–F9 updates (M2).
  checkUpdates: "Check updates",
  checkingUpdates: "Checking updates…",
  updatesAvailable: "%1 updates available",
  updatesNone: "No updates available",
  updateAvailable: "⬆ %1 available",
  // An installability refusal reads "cannot verify" instead of a pending
  // release: the row never offers an update the backend would refuse.
  cannotVerify: "cannot verify",
  // A second operation is a wait, never a backend failure; it only shows
  // when no other status is up.
  operationBusy: "One operation at a time",
  updateAction: "Update",
  updating: "%1 updating…",
  updated: "Updated to %1",
  upToDate: "Up to date",
  skippedRunning: "Skipped — the app is running",
  // "Update all": the click only OPENS the consent card — nothing is
  // spawned until the user accepts. The process line is count-free when a
  // running app's count is not determinable: the card never invents a
  // number.
  updateAllTooltip: "Update all",
  updateAllTitle: "Update all pending apps",
  updateAllApps: "Apps: %1",
  updateAllProcessesNone: "No running app will be closed.",
  updateAllProcessesOne: "1 process will be closed to apply the updates.",
  updateAllProcessesMany: "%1 processes will be closed to apply the updates.",
  updateAllProcessesUnknown: "Running apps will be closed to apply the updates.",
  updateAllConfirm: "Update all",
  updateAllKeysHint: "Enter confirms · Esc cancels",
  // Force-quit before update: Update on a running row opens a confirm card
  // first — nothing is spawned until Confirm. running_pids 0 (not
  // determinable, a stale flag, or an older backend) keeps the count-free
  // sentence. forceQuitApplied is the done-banner's closed-app clause,
  // appended only when the finished document carries a usable force_quit
  // record; SIGKILL reads differently because the app had to be forced.
  // forceQuitFailed is the same clause on an `error` document.
  forceQuitTitle: "App is running",
  forceQuitBody: "%1 is running — it will be closed to apply the update.",
  forceQuitBodyOne: "%1 is running — 1 process will be closed to apply the update.",
  forceQuitBodyMany: "%1 is running — %2 processes will be closed to apply the update.",
  forceQuitConfirm: "Force quit & update",
  forceQuitKeysHint: "Enter confirms · Esc cancels",
  forceQuitApplied: " — the app was closed to apply the update",
  forceQuitAppliedSigkill: " — the app was force-quit to apply the update",
  forceQuitFailed: " — the app was closed before the update failed",
  forceQuitFailedSigkill: " — the app was force-quit before the update failed",
  // A derived interruption (dead updater or a heartbeat stale past the
  // contract's 120 s): the job document never says "interrupted" itself,
  // so this copy owns the banner. The row is not active, so Update is
  // usable again and a retry starts a fresh job.
  jobInterrupted: "Update interrupted — it did not finish. Press Update to try again.",
  settingsTitle: "Settings",
  pinTooltip: "Pin panel",
  pinTooltipUnpin: "Unpin panel",
  settingsUpdateChecks: "Update checks",
  checkIntervalMinutes: "Check interval (minutes)",
  firstCheckDelayMinutes: "First check delay (minutes)",
  enabledLabel: "Enabled",
  disabledLabel: "Disabled",
  notificationsOnNewUpdates: "New updates are announced with a desktop notification.",
  settingsSave: "Save",
  settingsSaved: "Settings saved",
  // Per-app update source strings. sourceFieldLabels is keyed by manager
  // config key; manager names resolve through MANAGERS' labelKey so both
  // stay central for i18n.
  sourceEmbedded: "%1 (embedded)",
  sourceManagerLabel: "Manager",
  sourceFieldLabels: {
    url: "URL",
    repo: "Repository",
    repo_url: "Repository URL",
    repo_filename: "Release filename",
    allow_prereleases: "Allow prereleases"
  },
  sourceEditTooltip: "Edit source",
  sourceSave: "Save",
  sourceUnset: "Unset",
  sourceSaved: "Update source saved",
  sourceUnsetDone: "Update source removed",
  managerStatic: "Static URL",
  managerGithub: "GitHub releases",
  managerGitlab: "GitLab releases",
  managerCodeberg: "Codeberg releases",
  managerForgejo: "Forgejo releases"
}

function formatCount(n, template) {
  return String(template).replace("%1", String(n))
}

// Escape the three characters a StyledText line would read as markup. The
// row meta line is one bounded, elided Text carrying HTML so its fragments
// cannot paint over the row's action buttons.
function escapeHtml(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
}

// The row's IDENTITY line: the installed version and the running dot.
// `accent` draws in the accent colour, otherwise quiet text.
function rowIdentityMeta(item) {
  var segs = []
  if (!item) return segs
  var version = String(item.version || "")
  if (version !== "") segs.push({ text: version, accent: false })
  if (item.running === true) {
    segs.push({ text: "● " + String(strings.runningLabel), accent: true })
  }
  return segs
}

// The row's STATUS line: the pending release (or the "cannot verify"
// state) and the live job progress. `progress` is the job progress label
// the panel knows while the row's job is active ("" otherwise): only the
// panel knows the job state, and the empty string keeps this pure.
// A source that cannot prove its artifact is reported as a capability, not
// an offer — the row says "cannot verify" and never shows the pending
// release, because the backend would refuse that install. A row with
// neither a pending release nor a live job produces no segments, so the
// status line stays hidden.
function rowStatusMeta(item, progress) {
  var segs = []
  if (!item) return segs
  var pending = String(item.updateVersion || "")
  var unverifiable = item.installable === false
    && (pending !== "" || item.hasUpdate === true)
  if (unverifiable) {
    segs.push({ text: strings.cannotVerify, accent: false })
  } else if (pending !== "") {
    segs.push({ text: formatCount(pending, strings.updateAvailable), accent: true })
  }
  var live = String(progress || "")
  if (live !== "") segs.push({ text: live, accent: true })
  return segs
}

// Force-quit card sentence: names the app and how many processes the
// confirmed update will close. runningPids 0 (not running, not
// determinable, or an older backend) keeps the count-free fallback: the
// card never invents a number.
function forceQuitBody(name, runningPids) {
  var n = toInt(runningPids, 0)
  var template = n === 1 ? strings.forceQuitBodyOne
    : n > 1 ? strings.forceQuitBodyMany
      : strings.forceQuitBody
  return String(template).replace(/%([12])/g, function (_m, slot) {
    return slot === "1" ? String(name) : String(n)
  })
}

// The "Update all" consent card's wording. `apps` is the pinned list
// ({ id, name, running, runningPids }) captured when the card opened, so a
// refresh cannot rewrite what the user is consenting to. A running app
// whose count is not determinable keeps the count-free sentence: the card
// never invents a total it cannot know.
function updateAllSummary(apps) {
  var list = Array.isArray(apps) ? apps : []
  var names = []
  var total = 0
  var unknown = false
  for (var i = 0; i < list.length; i++) {
    var app = list[i] || {}
    var name = String(app.name || app.id || "")
    if (name !== "") names.push(name)
    if (app.running !== true) continue
    var pids = toInt(app.runningPids, 0)
    if (pids > 0) total += pids
    else unknown = true
  }
  var line
  if (unknown) line = strings.updateAllProcessesUnknown
  else if (total === 0) line = strings.updateAllProcessesNone
  else if (total === 1) line = strings.updateAllProcessesOne
  else line = formatCount(total, strings.updateAllProcessesMany)
  return {
    appsLine: formatCount(names.join(", "), strings.updateAllApps),
    processLine: line,
    totalProcesses: total
  }
}

// ---- state -----------------------------------------------------------------

var state = {
  // Normalized items; `id` is the desktop_id CONTRACT.md mandates for
  // --remove, `path` feeds the detached launch.
  items: [],
  lastSyncMs: 0,
  lastError: "",
  lastErrorSubject: "",
  // An armed launch verdict lives in this engine-wide store, not on the
  // panel object: the shell unloads the panel QML on close, taking its
  // timers with it. { id, atMs } or null.
  launchVerdict: null,
  status: "",
  // The progress text a URL integrate last published, so
  // releaseOrphanedRuns() can clear a line the killed run left behind:
  // its cleared-status list is static strings, and a memoised
  // "Downloading 45%…" would otherwise outlive the reloaded panel.
  integrateProgressStatus: "",
  // The listing envelope's `runtime` block: { fuse2: "present" | "missing"
  // | "unknown" }. "unknown" is the no-claim default; the launch-failure
  // fuse2 hint fires only on exactly "missing".
  runtime: { fuse2: "unknown" },
  // busy.* are engine-wide booleans for whole-surface operations. Per-app
  // update jobs deliberately have NO flag here: their state is derived from
  // the on-disk job document (state.jobStates), so no reload can leave a
  // spinner without a document behind it.
  busy: { list: false, launch: false, remove: false, integrate: false,
          updates: false, pick: false },
  // argv array (e.g. ["python3", "<pluginDir>/backend/main.py"]); null until
  // configured or derived from the manifest source dir.
  backendCommand: null,
  mockMode: false,
  startupSyncClaimed: false,
  // settings mirrors the backend's --settings document; settingsRevision is
  // the service's reschedule signal (a JS library has no property
  // notifications, so the revision IS the notification).
  // lastUpdatesCheckMs timestamps the last finished sweep.
  settings: { enabled: true, intervalMinutes: 360, delayMinutes: 5 },
  settingsRevision: 0,
  lastUpdatesCheckMs: 0,
  updatesChecking: false,
  // Who holds the update-check claim ("panel" | "service" | ""). The claim
  // outlives the caller's QML object, so a panel open may only release a
  // claim the PANEL took: Service.qml is never unloaded, and stealing its
  // claim would let the panel run a second sweep beside it.
  updateCheckOwner: "",
  updateCheckToken: 0,
  // Live claims: name -> { owner, token, deadlineMs }.
  claims: {},
  // Background update jobs: desktop_id -> { decision, consumed }. decision
  // is a JobDocs.adoptJob-shaped view of the app's CURRENT on-disk document;
  // consumed is the sign-of-life already surfaced to the user. Rows render
  // this and nothing else.
  jobStates: {}
}

var subscribers = []
var revision = 0

function subscribe(fn) {
  subscribers.push(fn)
  return function() {
    var next = []
    for (var i = 0; i < subscribers.length; i++) {
      if (subscribers[i] !== fn) next.push(subscribers[i])
    }
    subscribers = next
  }
}

function changed() {
  revision++
  for (var i = 0; i < subscribers.length; i++) {
    try { subscribers[i]() } catch (e) { /* one broken listener must not break the rest */ }
  }
}

// ---- configuration ---------------------------------------------------------

function setMockMode(value) {
  state.mockMode = value === true
  changed()
}

function configureBackend(command) {
  state.backendCommand = Array.isArray(command) && command.length > 0
    ? command.slice()
    : state.backendCommand
  changed()
}

// One-shot guard for the bar widget's startup refresh: a bar surface exists
// per monitor, but a single backend probe at login is enough.
function claimStartupSync() {
  if (state.startupSyncClaimed) return false
  state.startupSyncClaimed = true
  return true
}

// ---- operations and claims --------------------------------------------------
//
// One claim table for the whole engine: { owner, token, deadlineMs }. The
// token makes a stale release unable to free a newer claim; the deadline
// lets anyone take over after a surface dies without releasing. Quickshell
// kills a closed panel's Process objects, so their onDone never runs and a
// claim has to be able to expire. A failed claim is a WAIT, not an error.

var CLAIM_CHECK_LEASE_MS = 150000  // the panel's 120 s check watchdog + 30 s

var claimSeq = 0

function claimOperation(name, owner, ttlMs, nowMs) {
  var key = String(name || "")
  if (key === "") return null
  var now = typeof nowMs === "number" ? nowMs : Date.now()
  var live = state.claims[key]
  if (live && live.deadlineMs > now) return null
  var handle = {
    name: key,
    owner: typeof owner === "string" ? owner : "",
    token: ++claimSeq,
    deadlineMs: now + Math.max(0, toInt(ttlMs, 0))
  }
  state.claims[key] = handle
  changed()
  return { name: handle.name, owner: handle.owner, token: handle.token,
           deadlineMs: handle.deadlineMs }
}

// Releasing twice is harmless: only the taking whose token still owns the
// claim can free it.
function releaseOperation(handle) {
  if (!handle || typeof handle.name !== "string") return false
  var live = state.claims[handle.name]
  if (!live || live.token !== handle.token) return false
  delete state.claims[handle.name]
  changed()
  return true
}

function claimActive(name, nowMs) {
  var live = state.claims[String(name || "")]
  if (!live) return false
  var now = typeof nowMs === "number" ? nowMs : Date.now()
  return live.deadlineMs > now
}

// The one snapshot the panel and the service render.
function snapshot() {
  return {
    busy: {
      list: state.busy.list === true,
      remove: state.busy.remove === true,
      integrate: state.busy.integrate === true,
      pick: state.busy.pick === true,
      updates: state.busy.updates === true
    },
    claims: state.claims,
    launchVerdict: state.launchVerdict,
    updatesChecking: state.updatesChecking === true,
    updateCheckOwner: state.updateCheckOwner,
    status: state.status,
    lastError: state.lastError
  }
}

// Engine-wide single flight for update checks (F6/F8): the panel's
// --list-updates and the service's --fetch-updates both go through here so
// a sweep can never stack on top of another one. Returns false when a live
// claim exists; an EXPIRED claim is taken over (its owner is gone).
function claimUpdateCheck(owner) {
  if (claimActive("updates")) return false
  var handle = claimOperation("updates", owner, CLAIM_CHECK_LEASE_MS)
  if (!handle) return false
  state.busy.updates = true
  state.updatesChecking = true
  state.updateCheckOwner = handle.owner
  state.updateCheckToken = handle.token
  changed()
  return true
}

function releaseUpdateCheck(owner) {
  // Only the owner may release: an expired claim was taken over, and the
  // loser's onDone must not free the winner's claim.
  if (typeof owner === "string" && owner !== ""
      && state.updateCheckOwner !== owner) return false
  if (state.updateCheckToken) {
    releaseOperation({ name: "updates", token: state.updateCheckToken })
  } else if (state.claims.updates) {
    delete state.claims.updates
  }
  if (!state.busy.updates && !state.updatesChecking
      && state.updateCheckOwner === "") return
  state.busy.updates = false
  state.updatesChecking = false
  state.updateCheckOwner = ""
  state.updateCheckToken = 0
  changed()
}

// Release every run flag a PANEL instance owned, for a panel that no longer
// exists. The shell's panel Loader unloads the whole panel QML object on
// close, which destroys the Process objects and — Quickshell's kill is
// `running: false` — SIGTERMs the backend child. Every clear site for these
// flags sits INSIDE the run's onDone callback, which a killed run never
// reaches, and this store outlives every panel instance, so the flags would
// survive as a permanent claim. A freshly loaded panel cannot have a run of
// its own in flight, so releasing on open is always correct. `lastError` is
// a real outcome and is never cleared here; only the in-flight status line
// goes with the flags.
function releaseOrphanedRuns() {
  // The progress line a URL integrate last published dies with the panel
  // the same way as the static in-flight texts.
  var runStatuses = [strings.integrateWorking, strings.checkingUpdates,
                     state.integrateProgressStatus]
  state.integrateProgressStatus = ""
  state.busy.list = false
  state.busy.remove = false
  state.busy.integrate = false
  state.busy.pick = false
  // The update-check claim is released only when the PANEL took it:
  // Service.qml is never unloaded, so its claim is not an orphan and
  // stealing it would let the panel run a second sweep beside the service.
  for (var name in state.claims) {
    if (state.claims[name].owner === "panel") delete state.claims[name]
  }
  if (state.updateCheckOwner === "panel") {
    state.busy.updates = false
    state.updatesChecking = false
    state.updateCheckOwner = ""
    state.updateCheckToken = 0
  }
  for (var i = 0; i < runStatuses.length; i++) {
    if (state.status === runStatuses[i]) state.status = ""
  }
  changed()
}

// ---- python3 dependency (authorized in-panel install) -----------------------
//
// python3 is the backend's only runtime dependency and Omarchy does not
// guarantee it. Installing a system package is the user's decision, so the
// Install button opens a visible floating terminal running the exact
// command — the terminal (and its sudo prompt) IS the authorization. The
// terminal does NOT echo the command, so no copy may claim it "shows" it.
//
// The terminal command and the panel probe coordinate through two marker
// files under $XDG_RUNTIME_DIR: ".complete" on success, the exit status
// ("130" = Ctrl-C) written into ".failed".
//
// Shell-source invariant: `omarchy launch floating terminal with
// presentation <cmd>` feeds <cmd> to bash as SOURCE, so pythonInstallCommand()
// is ZERO-ARGUMENT and returns a CONSTANT string. Environment-derived values
// never appear as interpolated source — the terminal shell reads
// XDG_RUNTIME_DIR itself via "${XDG_RUNTIME_DIR:?}" which fails visibly if
// unset — and the runtime dir travels as argv DATA to the prep step, never
// inside a shell string. scripts/ci/security-gates.sh rule
// shell-source-constancy enforces this mechanically.

var PYTHON_MARKER_BASE = "appimage-plugin-python-install."

function pythonMarkerPaths(runtimeDir) {
  var dir = String(runtimeDir || "")
  return {
    failure: dir + "/" + PYTHON_MARKER_BASE + "failed",
    complete: dir + "/" + PYTHON_MARKER_BASE + "complete"
  }
}

// The exact shell line the floating terminal runs. CONSTANT SOURCE, per the
// invariant above. Absolute executable paths because the line must not
// depend on PATH. POSIX sh only — the presentation terminal is whatever the
// user's omarchy default is. `|| status=$?` captures the install result
// without set -e semantics; `(exit "$status")` reproduces it as the exit.
function pythonInstallCommand() {
  return "/usr/bin/rm -f \"${XDG_RUNTIME_DIR:?}/" + PYTHON_MARKER_BASE + "failed\""
    + " \"${XDG_RUNTIME_DIR:?}/" + PYTHON_MARKER_BASE + "complete\""
    + "; status=0; /usr/bin/omarchy pkg add python || status=$?"
    + "; if [ \"$status\" -eq 0 ]; then : > \"${XDG_RUNTIME_DIR:?}/" + PYTHON_MARKER_BASE + "complete\""
    + "; else printf '%s\\n' \"$status\" > \"${XDG_RUNTIME_DIR:?}/" + PYTHON_MARKER_BASE + "failed\"; fi"
    + "; (exit \"$status\")"
}

// argv for Process.startDetached. Absolute argv[0] keeps the one hop
// execvp could have resolved from PATH on the fixed-path invariant.
function pythonInstallArgv() {
  return ["/usr/bin/omarchy", "launch", "floating", "terminal", "with",
          "presentation", pythonInstallCommand()]
}

// Panel-side syntactic gate on XDG_RUNTIME_DIR, run before anything spawns.
// The authoritative check (directory, owned by the euid, mode 700) is
// pythonInstallPrepScript() below.
function pythonRuntimeDirUsable(runtimeDir) {
  var dir = String(runtimeDir || "")
  if (dir === "" || dir.substring(0, 1) !== "/") return false
  var parts = dir.split("/")
  for (var i = 0; i < parts.length; i++) {
    if (parts[i] === "..") return false
  }
  return true
}

// Prep step for `sh -c`, run before the terminal launches. CONSTANT source
// that only ever sees data through positional parameters:
//   $1  the runtime dir        $2  failure marker       $3  complete marker
// Exit 5 = runtime dir refused. `-- ` and `2>/dev/null` keep odd values or
// an absent stat from leaking into the verdict.
function pythonInstallPrepScript() {
  return "case \"$1\" in /*) ;; *) exit 5;; esac"
    + "; [ -d \"$1\" ] || exit 5"
    + "; [ -O \"$1\" ] || exit 5"
    + "; [ \"$(/usr/bin/stat -c %a -- \"$1\" 2>/dev/null)\" = \"700\" ] || exit 5"
    + "; /usr/bin/rm -f -- \"$2\" \"$3\""
}

// argv for the prep Process: script + positional data only. The runtime
// dir rides as argv, never inside the shell source. argv[0] and the "$0"
// label are absolute /usr/bin/sh so nothing resolves through PATH.
function pythonInstallPrepArgv(runtimeDir) {
  var marker = pythonMarkerPaths(runtimeDir)
  return ["/usr/bin/sh", "-c", pythonInstallPrepScript(), "/usr/bin/sh",
          String(runtimeDir || ""), marker.failure, marker.complete]
}

// Probe/poll body, meant for `sh -c`. Exit codes double as state:
//   0  python3 present          1  missing
//   2  install failed (stdout: the captured exit status; "130" = canceled)
//   3  installing and still waiting (markers say the terminal is at work)
function pythonProbeScript() {
  return "if test \"$3\" = \"1\"; then"
    + " if test -f \"$1\"; then cat \"$1\"; exit 2; fi"
    + "; if ! test -f \"$2\"; then exit 3; fi; fi"
    + "; if command -v python3 >/dev/null 2>&1; then exit 0; fi"
    + "; exit 1"
}

// Full argv for the panel's probe Process; installing flips the
// marker-aware poll mode. Absolute /usr/bin/sh, same fixed-path rule.
function pythonProbeArgv(failurePath, completePath, installing) {
  return ["/usr/bin/sh", "-c", pythonProbeScript(), "/usr/bin/sh",
          String(failurePath || ""), String(completePath || ""),
          installing === true ? "1" : "0"]
}

// Pure mapping of a finished probe onto one of "present", "missing",
// "waiting", "canceled" or "failed"; kept out of QML so the harness can pin
// the semantics.
function pythonProbeOutcome(exitCode, output, installing) {
  if (exitCode === 0) return "present"
  if (exitCode === 2) {
    return String(output || "").trim() === "130" ? "canceled" : "failed"
  }
  if (exitCode === 3 && installing === true) return "waiting"
  return "missing"
}

// ---- derived counts --------------------------------------------------------

function installedCount() {
  return state.items.length
}

function runningCount() {
  var n = 0
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].running) n++
  }
  return n
}

function updateCount() {
  // Only installable updates count: a badge must never promise an install
  // the backend would refuse for want of a publisher digest.
  var n = 0
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].hasUpdate && state.items[i].installable !== false) n++
  }
  return n
}

// The ids of the apps a batch update would touch: every app with a pending
// release the backend would accept. It follows the badge's own rule, so
// "Update all" can never offer an install the backend would refuse.
function pendingUpdateIds() {
  var ids = []
  for (var i = 0; i < state.items.length; i++) {
    var item = state.items[i]
    if (item.hasUpdate !== true) continue
    if (item.installable === false) continue
    ids.push(item.id)
  }
  return ids
}

function findItem(id) {
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].id === id) return state.items[i]
  }
  return null
}

// ---- backend output normalization ------------------------------------------
//
// Shapes are fixed by backend/CONTRACT.md (schema_version 1):
//   --list-installed --json  → {"schema_version":1, "installed":[<app>,…]}
//   --integrate/--remove     → {"schema_version":1, "result":…, "app":…}
//   --preview-integrate      → {"schema_version":1, "result":"preview", …}
//   --list-updates/--fetch-updates → {"schema_version":1, "updates":[<app>,…]}
//   --settings                     → {"schema_version":1, "settings":{…}}
//   --pick-file                    → {"schema_version":1, "result":"picked" |
//                                     "cancelled" | "unavailable" | "error"}
//   error (exit != 0)        → {"schema_version":1, "result":"error", …}
// A bare array is also accepted for list responses so old captures keep
// parsing; older captures missing newer keys normalize to empty values.

function dirname(path) {
  var text = String(path || "")
  var slash = text.lastIndexOf("/")
  return slash > 0 ? text.substring(0, slash) : "."
}

function basenameNoExt(path) {
  var text = String(path || "")
  var slash = text.lastIndexOf("/")
  var base = slash >= 0 ? text.substring(slash + 1) : text
  var dot = base.lastIndexOf(".")
  return dot > 0 ? base.substring(0, dot) : base
}

// The managed icon lives next to the AppImage under .icons/<base>.<ext>
// (CONTRACT.md filesystem layout). The extension is not in the JSON, so the
// view tries png/svg in turn, each gated on its own per-candidate verdict.
function iconBaseFor(app) {
  var path = String(app.path || "")
  if (path === "") return ""
  return dirname(path) + "/.icons/" + basenameNoExt(path)
}

// The desktop entry's Name carries a ` (<version>)` suffix so the LAUNCHER
// can show the version, but the row already prints the version, so keeping
// it would echo it twice. GearLever also invents a version when the
// AppImage ships none — the first six hex characters of the file's md5
// ("Terax (77efdf)"). Drop a trailing parenthetical that IS the app's
// version or that hash shape; any other parenthetical is part of the name.
var HASH_VERSION_RE = /^[0-9a-f]{6}$/

function displayName(name, version) {
  var text = String(name || "")
  var match = /^([\s\S]*) \(([^()]*)\)$/.exec(text)
  if (!match) return text
  var inner = match[2]
  // The hash shape is stripped unconditionally: it is GearLever's md5
  // fallback baked into an old install's desktop Name, garbage whatever
  // version the backend reports now.
  var strip = inner !== "" && (inner === String(version || "")
    || HASH_VERSION_RE.test(inner))
  return strip ? match[1].replace(/\s+$/, "") : text
}

// The listing's pending triple: the backend's durable record owns the
// verdict and every listing re-reads it —
//   pending_update      → hasUpdate
//   pending_version     → updateVersion ("" when no version is named)
//   pending_installable → installable (true / false / null)
// An older backend reports none: updateVersion falls back to
// available_version, installable to the listing's installable key, and
// hasUpdate stays false until a check says otherwise.
function pendingVersionOf(entry) {
  if (entry.pending_version !== undefined) {
    return entry.pending_version === null ? "" : String(entry.pending_version)
  }
  return String(entry.available_version || entry.update_version || "")
}

function pendingInstallableOf(entry) {
  if (entry.pending_installable !== undefined) {
    return entry.pending_installable === true ? true
      : entry.pending_installable === false ? false : null
  }
  return entry.installable === true ? true : entry.installable === false ? false : null
}

function normalizeItem(entry) {
  if (!entry || typeof entry !== "object") return null
  // desktop_id is the stable id (same-named apps coexist); when the backend
  // reports null, the absolute path is the fallback.
  var id = entry.desktop_id || entry.desktopId || entry.path || entry.id || ""
  if (String(id) === "") return null
  var item = {
    id: String(id),
    name: displayName(entry.name || id, entry.current_version || entry.version),
    // X-AppImage-Version only; empty when the AppImage carries none, never
    // the backend's md5 fallback.
    version: String(entry.current_version || entry.version || ""),
    path: String(entry.path || ""),
    // The app's own Exec arguments, e.g. "--no-sandbox %U". The panel
    // forwards the real flags on launch and drops the desktop field codes
    // it cannot fill.
    execArguments: String(entry.exec_arguments || entry.execArguments || ""),
    // The launch command as argv, built by the backend's ONE launch builder
    // so QML never assembles `env DESKTOPINTEGRATION=1 ...`. An older
    // backend means null and the panel falls back to the bare path.
    launchArgv: Array.isArray(entry.launch_argv)
      ? entry.launch_argv.map(String) : null,
    // The argv tail for the backend's `--exec-with-scale` entry point: the
    // image plus its own arguments, field codes dropped. The panel spawns
    // [backendCommand..., "--exec-with-scale", *launchScaleArgv] so its
    // launch shares the launcher's scale mechanism. Older backend: null.
    launchScaleArgv: Array.isArray(entry.launch_scale_argv)
      ? entry.launch_scale_argv.map(String) : null,
    running: entry.running === true,
    // 0 when not running, when the count is not determinable, and on an
    // older backend: the force-quit card then falls back to its count-free
    // sentence.
    runningPids: Math.max(0, toInt(
      entry.running_pids !== undefined ? entry.running_pids : entry.runningPids, 0)),
    iconBase: "",
    // Display gate: the listing is the only proof that
    // `.icons/<base>.{png,svg}` passed the CURRENT sanitiser. `icon_safe` is
    // a per-candidate verdict dict; AppRow may load only candidates whose
    // own verdict is true. Anything else is null: no proof, glyph, fail
    // closed.
    iconSafe: (entry.icon_safe && typeof entry.icon_safe === 'object')
      ? entry.icon_safe : null,
    updateVersion: pendingVersionOf(entry),
    // The listing's pending_update is the verdict, so the marker survives
    // the 10 s poll instead of flapping. An older backend reports no pending
    // triple: the marker stays false and only applyUpdates can set it.
    hasUpdate: entry.pending_update === true,
    // manager names the configured custom source ("" when none),
    // embeddedSource marks an AppImage that ships its own updater,
    // downloadSize is bytes (0 = unknown).
    manager: String(entry.manager || ""),
    // true = a fetch under the required verification policy would be
    // attempted, false = the source would refuse the install (no usable
    // publisher digest), null = not determinable. A missing key from an
    // older backend means null.
    installable: pendingInstallableOf(entry),
    verification: typeof entry.verification === "string" ? entry.verification : "",
    embeddedSource: entry.embedded_source === true,
    downloadSize: typeof entry.download_size === "number" && entry.download_size > 0
      ? entry.download_size : 0
  }
  item.iconBase = iconBaseFor(item)
  return item
}

function parseListJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return parsed
  var list = Array.isArray(parsed.doc) ? parsed.doc : parsed.doc.installed
  if (!Array.isArray(list)) {
    return { ok: false, items: [], runtime: parseRuntime(parsed.doc),
             error: "Backend response had no installed list" }
  }
  var items = []
  for (var i = 0; i < list.length; i++) {
    var item = normalizeItem(list[i])
    if (item) items.push(item)
  }
  return { ok: true, items: items, runtime: parseRuntime(parsed.doc), error: "" }
}

// Normalize the installed-list envelope's additive `runtime` block onto
// { fuse2: "present" | "missing" | "unknown" }. Absent, null, non-object
// and array shapes, and any value outside the three states, normalize to
// "unknown" = no claim (the fuse2 hint may fire only on exactly "missing").
// The result is a fresh object, never a subtree of the parsed document.
function parseRuntime(doc) {
  var block = doc && typeof doc === "object" && !Array.isArray(doc)
    ? doc.runtime : null
  var fuse2 = block && typeof block === "object" && !Array.isArray(block)
    ? String(block.fuse2 || "") : ""
  return {
    fuse2: fuse2 === "present" || fuse2 === "missing" ? fuse2 : "unknown"
  }
}

// Banner text for a failed launch: the base line, plus the fuse2 hint ONLY
// when the same listing reports exactly "missing" — "present" and "unknown"
// are no claim. Text only by the repo wording rule: the hint names the
// library, never a command, never a button.
function launchFailureBanner(appName, fuse2State) {
  var base = formatCount(String(appName || ""), strings.launchFailed)
  if (fuse2State !== "missing") return base
  return base + "\n" + strings.launchFuse2Hint
}

// Parse a --list-updates / --fetch-updates response: the same normalized
// items as the installed list. offline:true means the sweep never reached
// the network, so callers must not read its empty list as "up to date".
function parseUpdatesJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) {
    return { ok: false, updates: [], offline: false, notified: false, error: parsed.error }
  }
  var list = Array.isArray(parsed.doc) ? parsed.doc : parsed.doc.updates
  if (!Array.isArray(list)) {
    return { ok: false, updates: [], offline: false, notified: false,
             error: "Backend response had no updates list" }
  }
  var updates = []
  for (var i = 0; i < list.length; i++) {
    var item = normalizeItem(list[i])
    if (item) updates.push(item)
  }
  return {
    ok: true,
    updates: updates,
    offline: parsed.doc.offline === true,
    notified: parsed.doc.notified === true,
    error: ""
  }
}

// Parse a --settings / --set-setting response into the store's shape. The
// 15-minute floor matches --set-setting's own minimum so a bad document
// cannot tighten the loop.
function parseSettingsJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return { ok: false, settings: null, error: parsed.error }
  var doc = parsed.doc.settings && typeof parsed.doc.settings === "object"
    ? parsed.doc.settings : parsed.doc
  return {
    ok: true,
    settings: {
      enabled: doc.update_check_enabled !== false,
      intervalMinutes: Math.max(15, toInt(doc.update_check_interval_minutes, 360)),
      delayMinutes: Math.max(0, toInt(doc.update_check_delay_minutes, 5))
    },
    error: ""
  }
}

// JSON numbers arrive as ints, floats or strings depending on how careful
// the producer was; null/absent fall back instead of coercing to 0.
function toInt(value, fallback) {
  if (value === null || value === undefined || value === "") return fallback
  var n = Number(value)
  return isFinite(n) ? Math.round(n) : fallback
}

// Parse an action response (--integrate / --remove / --update): returns
// { ok, result, message, error, app }. Any non-error result passes through
// as ok, so a newer backend can add outcomes without breaking the panel.
function parseActionJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return { ok: false, result: "error", message: "", error: parsed.error, app: null }
  var doc = parsed.doc
  var result = String(doc.result || "")
  if (result === "") {
    return { ok: false, result: "error", message: "", error: "Backend response had no result field", app: null }
  }
  return {
    ok: result !== "error",
    result: result,
    message: String(doc.message || ""),
    error: String(doc.error || ""),
    app: normalizeItem(doc.app)
  }
}

// Parse a --pick-file response: { ok, result, path, backend, reason,
// message, error }. An unknown result passes through like parseActionJson's
// (ok unless "error"), and `path` is only surfaced on a real "picked".
function parsePickJson(text) {
  var empty = { ok: false, result: "error", path: "", backend: "",
                reason: "", message: "", error: "" }
  var parsed = parseDocument(text)
  if (!parsed.ok) {
    empty.error = parsed.error
    return empty
  }
  var doc = parsed.doc
  var result = String(doc.result || "")
  if (result === "") {
    empty.error = "Backend response had no result field"
    return empty
  }
  return {
    ok: result !== "error",
    result: result,
    path: result === "picked" ? String(doc.path || "") : "",
    backend: String(doc.backend || ""),
    reason: String(doc.reason || ""),
    message: String(doc.message || ""),
    error: String(doc.error || "")
  }
}

// Parse a --preview-integrate document into the shape the preview card
// renders. The document is advisory and its keys are additive: every
// missing/null sub-key normalizes to a safe default, never undefined. A
// document that is not a preview (an error document, an action result,
// garbage) is rejected so the panel degrades to a status message with the
// picker still usable — it must never render a made-up preview.
function parsePreviewJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return { ok: false, preview: null, error: parsed.error }
  var doc = parsed.doc
  if (String(doc.result || "") !== "preview") {
    return { ok: false, preview: null, error: "Backend response was not a preview document" }
  }
  // Sub-objects may be absent or not objects; treat each as empty.
  function sub(value) {
    return value && typeof value === "object" ? value : {}
  }
  var source = sub(doc.source)
  var verification = sub(doc.verification)
  var destination = sub(doc.destination)
  var app = doc.app && typeof doc.app === "object" ? doc.app : null
  // filename/path are null for URL sources (the final name comes from the
  // artifact at integrate time) — mapped to "" so QML string bindings never
  // see null.
  function opt(value) {
    return value === null || value === undefined ? "" : String(value)
  }
  return {
    ok: true,
    preview: {
      sourceKind: String(source.kind || ""),
      sourceInput: String(source.input || ""),
      verificationMethod: String(verification.method || ""),
      // Strictly === true: an absent/garbage flag must not scare the user
      // with the reduced-assurance warning.
      reducedAssurance: verification.reduced_assurance === true,
      verificationNote: String(verification.note || ""),
      destinationFolder: String(destination.folder || ""),
      destinationFilename: opt(destination.filename),
      destinationPath: opt(destination.path),
      // app is null for URL sources and for local files whose embedded
      // desktop entry cannot be read in place; both render "unknown".
      appName: app ? String(app.name || "") : "",
      appVersion: app ? String(app.version || "") : "",
      alreadyIntegrated: doc.already_integrated === true,
      nameConflict: doc.name_conflict === true,
      advisory: doc.advisory !== false
    },
    error: ""
  }
}

// Tolerant document parse: stdout may hold NDJSON progress documents ahead
// of the result. Strict whole-text first, then per line where the LAST
// valid document wins (a stray trailing line cannot erase an earlier
// result), then recover the outermost {…}/[…]. A document with a non-empty
// `event` key is never a result, so a progress-only stream is a failure.
function parseDocument(text) {
  var raw = String(text || "").trim()
  if (raw === "") return { ok: false, doc: null, error: "Empty backend response" }
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { doc = null }
  if (doc !== null && typeof doc === "object" && !Array.isArray(doc)
      && typeof doc.event === "string" && doc.event !== "") {
    // A progress event that parsed whole: never a result; the line pass and
    // recovery below still get their chance.
    doc = null
  }
  if (doc === null) {
    var lines = raw.split("\n")
    if (lines.length > 1) {
      // Once any complete line parsed as JSON (even one skipped as a
      // progress event), the whole-text recovery below must not resurrect
      // a skipped event as the result.
      var sawLineDocument = false
      for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim()
        if (line === "") continue
        var candidate = null
        try { candidate = JSON.parse(line) } catch (e2) { /* progress or stray line */ }
        if (candidate !== null) sawLineDocument = true
        // An event document is skipped so a progress-only stream stays a
        // parse failure; a failed line never overwrites the accumulator
        // ("last valid document wins", not "last line wins").
        if (candidate && typeof candidate === "object" && !Array.isArray(candidate)
            && typeof candidate.event === "string" && candidate.event !== "") continue
        if (candidate !== null) doc = candidate
      }
      if (doc === null && sawLineDocument) {
        return { ok: false, doc: null, error: "Backend response was not JSON" }
      }
    }
  }
  if (doc === null) {
    var start = raw.indexOf("{")
    var startArray = raw.indexOf("[")
    if (startArray !== -1 && (start === -1 || startArray < start)) start = startArray
    var end = raw.lastIndexOf("}")
    var endArray = raw.lastIndexOf("]")
    if (endArray !== -1 && endArray > end) end = endArray
    if (start >= 0 && end > start) {
      try { doc = JSON.parse(raw.substring(start, end + 1)) } catch (e2) { doc = null }
      // Legacy recovery cannot resurrect a progress event: with no other
      // valid document anywhere in the stream, the verdict stays a failure.
      if (doc !== null && typeof doc === "object" && !Array.isArray(doc)
          && typeof doc.event === "string" && doc.event !== "") doc = null
    }
  }
  if (doc === null || typeof doc !== "object") {
    return { ok: false, doc: null, error: "Backend response was not JSON" }
  }
  return { ok: true, doc: doc, error: "" }
}

// ---- drop-to-integrate -------------------------------------------------------
//
// Pure URI→path conversion for the drop targets. Only file:// URIs with an
// empty authority are usable; a multi-URI selection picks the FIRST
// .AppImage (any case) and reports the rest as ignored. Typed result,
// pinned by the parse harness:
//   { ok: true,  path: "<decoded local path>", ignored: <others skipped> }
//   { ok: false, path: "", ignored: <entries seen>,
//     reason: "empty" | "not-file" | "no-appimage" }
// The decoded path is data for the picker field; this helper can never
// install anything.
function dropUriToPath(uris) {
  var list = Array.isArray(uris) ? uris : [uris]
  var path = ""
  var sawFile = false
  var total = 0
  for (var i = 0; i < list.length; i++) {
    var entry = list[i]
    var raw = String(entry === null || entry === undefined ? "" : entry).trim()
    if (raw === "") continue
    total++
    if (raw.indexOf("file://") !== 0) continue
    var rest = raw.substring("file://".length)
    if (rest.indexOf("/") !== 0) continue
    var decoded = ""
    try {
      decoded = rest.split("/").map(decodeURIComponent).join("/")
    } catch (e) { continue }
    sawFile = true
    if (path === "" && /\.appimage$/i.test(decoded)) path = decoded
  }
  if (total === 0) return { ok: false, path: "", ignored: 0, reason: "empty" }
  if (path !== "") return { ok: true, path: path, ignored: total - 1, reason: "" }
  if (!sawFile) return { ok: false, path: "", ignored: total, reason: "not-file" }
  return { ok: false, path: "", ignored: total, reason: "no-appimage" }
}

// ---- mutators (each announces through changed()) ---------------------------

function setItems(items, runtime, startedMs) {
  state.items = Array.isArray(items) ? items : []
  // A document without the runtime block (older backend) resets the store to
  // "unknown" so a stale "missing" can never survive into the fuse2 hint.
  state.runtime = runtime && typeof runtime === "object" && !Array.isArray(runtime)
    && typeof runtime.fuse2 === "string"
    ? { fuse2: runtime.fuse2 }
    : { fuse2: "unknown" }
  state.lastSyncMs = Date.now()
  pruneJobStates()
  // Only a listing that STARTED after the click may judge the launch: any
  // snapshot already in flight (or an earlier one) cannot see the new
  // process and would blame a healthy launch.
  var settled = settleLaunchVerdict(startedMs)
  changed()
  // True when this listing just raised the launch-failure banner: the
  // caller must not clear the error it settled.
  return settled
}

// Arm the verdict for `id`; the next listing that began after this moment
// decides it (see settleLaunchVerdict). Re-arming replaces the previous one.
// Only a panel that stays open may arm: its settle timer is what asks for
// that listing, so an armed verdict left behind by a closing panel would be
// judged by a much later listing, after the app had already quit normally.
function armLaunchVerdict(id, nowMs) {
  var key = typeof id === "string" ? id : ""
  if (key === "") return false
  state.launchVerdict = { id: key,
                          atMs: typeof nowMs === "number" ? nowMs : Date.now() }
  changed()
  return true
}

// The visit that armed the verdict ended before any listing could judge it:
// nothing observed the launch, so no verdict may be claimed.
function dropLaunchVerdict() {
  if (!state.launchVerdict) return false
  state.launchVerdict = null
  changed()
  return true
}

// A running app, or an app that vanished from the list, is silent — a
// missing row is no claim. A present, non-running row is the launch
// failure, and its fuse2 hint comes from the same listing's runtime block.
function settleLaunchVerdict(startedMs) {
  var verdict = state.launchVerdict
  if (!verdict) return false
  if (typeof startedMs !== "number" || startedMs < verdict.atMs) return false
  state.launchVerdict = null
  var item = findItem(verdict.id)
  if (!item || item.running === true) return false
  setError(launchFailureBanner(item.name, state.runtime.fuse2), verdict.id)
  return true
}

function setBusy(key, value) {
  state.busy[key] = value === true
  changed()
}

function isBusy() {
  for (var key in state.busy) {
    if (state.busy[key]) return true
  }
  return false
}

// Name the app in a banner: an error about one app must say which app.
function appErrorBanner(appName, detail) {
  var name = String(appName || "")
  return name === "" ? String(detail) : name + ": " + String(detail)
}

function setError(message, subject) {
  state.lastError = String(message || "")
  state.lastErrorSubject = typeof subject === "string" ? subject : ""
  if (state.lastError !== "") state.status = ""
  changed()
}

// A new outcome for `subject` clears the error that subject left behind. An
// outcome for any other app leaves it alone: a user reading why *this* update
// failed must not lose it because *that* app finished a check.
function noteOutcome(subject) {
  var id = typeof subject === "string" ? subject : ""
  if (id === "" || state.lastError === "") return
  if (state.lastErrorSubject !== id) return
  state.lastError = ""
  state.lastErrorSubject = ""
  changed()
}

function clearError() {
  if (state.lastError === "") return
  state.lastError = ""
  state.lastErrorSubject = ""
  changed()
}

function setStatus(message) {
  state.status = String(message || "")
  changed()
}

function clearStatus() {
  if (state.status === "") return
  state.status = ""
  changed()
}

// Report a second operation that had to wait. It must never become an error
// and must never wipe the in-flight status line, so it only speaks when
// nothing else is showing.
function setTransientStatus(message) {
  var text = String(message || "")
  if (text === "" || state.status !== "") return
  state.status = text
  changed()
}

// ---- integrate progress -----------------------------------------------------
//
// A URL integrate streams `event: progress` documents on stdout; the panel
// republishes each one as the status line. integrateProgressText is the
// pure derivation over one document; noteIntegrateProgress is the live
// wire: parse one line, ignore anything that is not a progress document
// (never an error — the same stream also carries the final result), publish
// the text, and memoise it so releaseOrphanedRuns() can clear it.

// Human byte form: MiB with one decimal below 10 MiB, integer from 10 MiB
// up; a GiB-scale transfer keeps counting in MiB.
function integrateProgressText(event, table) {
  var s = table || strings
  var doc = event && typeof event === "object" ? event : {}
  if (doc.phase === "verifying") return s.integrateVerifying
  if (doc.phase === "installing") return s.integrateInstalling
  if (doc.phase === "downloading") {
    var bytes = Number(doc.bytes)
    var total = Number(doc.total)
    if (!isFinite(bytes) || bytes < 0) return s.integrateWorking
    if (isFinite(total) && total > 0) {
      var pct = Math.min(100, Math.floor(bytes * 100 / total))
      return formatCount(pct, s.integrateDownloadingPct)
    }
    var mib = bytes / (1024 * 1024)
    var shown = mib < 10 ? Math.round(mib * 10) / 10 : Math.round(mib)
    return formatCount(shown, s.integrateDownloadingBytes)
  }
  return s.integrateWorking
}

function noteIntegrateProgress(line) {
  var doc = null
  try { doc = JSON.parse(String(line || "")) } catch (e) { return }
  if (!doc || typeof doc !== "object" || doc.event !== "progress") return
  var text = integrateProgressText(doc, strings)
  state.integrateProgressStatus = text
  setStatus(text)
}

function markRunning(id, running) {
  var item = findItem(id)
  if (!item || item.running === (running === true)) return
  item.running = running === true
  changed()
}

// Merge a finished update sweep into the installed list without a rescan.
// The sweep result is authoritative for the apps it REPORTS: each is marked
// hasUpdate (even a versionless static source) and gets its fresher source
// metadata. It never CLEARS an app it does not report — the response lists
// only apps with a pending release, so a failed check is simply absent and
// clearing here would make the marker flap; the listing clears it.
// Only update metadata moves: names, paths and running state still belong
// to --list-installed.
function applyUpdates(updates) {
  if (!Array.isArray(updates)) updates = []
  var byId = {}
  for (var u = 0; u < updates.length; u++) byId[updates[u].id] = updates[u]
  var touched = false
  for (var i = 0; i < state.items.length; i++) {
    var item = state.items[i]
    var upd = byId[item.id]
    if (!upd) continue
    if (item.hasUpdate !== true) {
      item.hasUpdate = true
      touched = true
    }
    if (item.updateVersion !== upd.updateVersion) {
      item.updateVersion = upd.updateVersion
      touched = true
    }
    if (item.manager !== upd.manager) {
      item.manager = upd.manager
      touched = true
    }
    if (item.embeddedSource !== upd.embeddedSource) {
      item.embeddedSource = upd.embeddedSource
      touched = true
    }
    if (item.downloadSize !== upd.downloadSize) {
      item.downloadSize = upd.downloadSize
      touched = true
    }
    if (item.installable !== upd.installable) {
      item.installable = upd.installable
      touched = true
    }
    if (item.verification !== upd.verification) {
      item.verification = upd.verification
      touched = true
    }
  }
  state.lastUpdatesCheckMs = Date.now()
  if (touched) changed()
}

// A finished update retires that app's pending verdict at once instead of
// at the next listing. The backend's record is the owner; this is only the
// mirror, and it lives in the shared store because the shell unloads the
// panel QML on close.
function retirePendingUpdate(id) {
  var item = findItem(String(id || ""))
  if (!item) return false
  if (item.hasUpdate !== true && item.updateVersion === "") return false
  item.hasUpdate = false
  item.updateVersion = ""
  changed()
  return true
}

// Store parsed settings and bump settingsRevision so the service's store
// subscription reschedules its check timer.
function applySettings(settings) {
  if (!settings || typeof settings !== "object") return
  state.settings.enabled = settings.enabled !== false
  state.settings.intervalMinutes = Math.max(15, toInt(settings.intervalMinutes, 360))
  state.settings.delayMinutes = Math.max(0, toInt(settings.delayMinutes, 5))
  state.settingsRevision++
  changed()
}

// ---- background update jobs (per-row, disk-derived) --------------------------
//
// A row's updating state is not an engine-wide flag: it is the adopted view
// of the app's on-disk job document (lib/JobDocs.js; CONTRACT.md "Job
// documents"). Rows render state.jobStates and nothing else, so after a
// reload mid-download the next open re-derives every row from disk and can
// never spin without a document behind it.
//
// entry shape: { decision, consumed }. consumed is the sign-of-life already
// surfaced to the user, so a settled update toasts once per job instead of
// on every poll tick or reopen; it lives in this engine-wide singleton and
// dies with the shell.

function jobEntry(id) {
  return state.jobStates[id] || null
}

// Compact identity of the user-visible decision shape, for poll de-dup:
// re-adopting an unchanged document must not churn the subscribers.
function jobDecisionKey(d) {
  return [d.state, d.phase, d.progress, d.message, d.newVersion, d.status,
          d.errorText, d.stale, d.pidDead].join("|")
}

// Store an adopted decision for `id`; changed() fires only when the
// user-visible shape actually moved.
function adoptJobDecision(id, decision) {
  var key = String(id || "")
  if (key === "" || !decision || typeof decision !== "object") return
  var prev = state.jobStates[key]
  if (prev && jobDecisionKey(prev.decision) === jobDecisionKey(decision)) return
  state.jobStates[key] = {
    decision: decision,
    consumed: prev ? prev.consumed : 0
  }
  changed()
  // Both terminal truths retire the pending verdict: the update was applied,
  // or the availability check proved there is nothing newer (the backend
  // drops the record entry in both cases). A failed or skipped-running job
  // keeps it — nothing was proven about the release.
  if (decision.state === "done"
      && (decision.status === "updated" || decision.status === "up-to-date")) {
    retirePendingUpdate(key)
  }
}

function jobDecision(id) {
  var e = state.jobStates[id]
  return e ? e.decision : null
}

// The per-app single flight: true only while THIS app's document says
// active. Other apps are never blocked — parallel updates are the point.
function jobActive(id) {
  var d = jobDecision(id)
  return !!d && d.state === "active"
}

// Done-banner text for a terminal job decision: the version line when the
// document carries one, else the backend's own message (the caller supplies
// the up-to-date fallback), plus the closed-app clause ONLY when the
// document recorded a force_quit.
function jobDoneBanner(decision, upToDateFallback) {
  var d = decision && typeof decision === "object" ? decision : {}
  var base
  if (String(d.status || "") === "up-to-date") {
    base = String(d.message || "") !== ""
      ? String(d.message) : String(upToDateFallback || "")
    // SPEC 4.4: an up-to-date document applied nothing, so it can never
    // claim the app was closed for it, whatever force_quit it carries
    return base
  }
  base = String(d.newVersion || "") !== ""
    ? formatCount(d.newVersion, strings.updated)
    : String(d.message || "")
  return base + forceQuitClause(d.forceQuit, false)
}

// The closed-app clause for a terminal banner. `quit` is
// JobDocs.normalizeForceQuit's output (null for absent/malformed values); a
// count of 0 cannot honestly claim a close, since the writer only records
// processes it actually signalled. `failed` picks the error wording, where
// "to apply the update" would be a lie. SIGKILL reads differently because
// the app had to be forced.
function forceQuitClause(quit, failed) {
  if (!quit || !(quit.count > 0)) return ""
  if (failed) {
    return quit.sigkill
      ? strings.forceQuitFailedSigkill : strings.forceQuitFailed
  }
  return quit.sigkill
    ? strings.forceQuitAppliedSigkill : strings.forceQuitApplied
}

// Error-banner text for a failed job decision: the backend's error, else
// its message, else the caller's fallback — plus the closed-app clause when
// the job closed the app and THEN failed.
function jobErrorBanner(decision, fallback) {
  var d = decision && typeof decision === "object" ? decision : {}
  var base = String(d.errorText || "") !== ""
    ? String(d.errorText)
    : (String(d.message || "") !== ""
      ? String(d.message) : String(fallback || ""))
  return base + forceQuitClause(d.forceQuit, true)
}

// Any row busy — gates the panel's job poll and its re-list poll.
function anyJobActive() {
  for (var id in state.jobStates) {
    if (state.jobStates[id].decision.state === "active") return true
  }
  return false
}

// Record that `id`'s outcome for `signOfLife` has been surfaced. -1 marks
// a corrupt document's once-per-session surfacing so the poll cannot
// re-toast it forever.
function markJobConsumed(id, signOfLife) {
  var e = state.jobStates[id]
  var mark = Number(signOfLife)
  if (!e || !isFinite(mark) || mark === 0 || e.consumed === mark) return
  e.consumed = mark
  changed()
}

// Drop job views for apps that left the installed list, so no ghost holds
// the poll open.
function pruneJobStates() {
  var keep = {}
  for (var i = 0; i < state.items.length; i++) {
    var id = state.items[i].id
    if (state.jobStates[id]) keep[id] = state.jobStates[id]
  }
  state.jobStates = keep
}

// ---- update-source managers (settings UI metadata) --------------------------
//
// Mirror of the managers --set-update-source accepts. labelKey resolves
// through `strings` for i18n, and `fields` drives the source editor: type
// "string" renders a text field, "bool" a switch. Keep in sync with the
// backend's manager registry.

var MANAGERS = [
  { name: "StaticFileUpdater", labelKey: "managerStatic",
    fields: [{ key: "url", type: "string" }] },
  { name: "GithubUpdater", labelKey: "managerGithub",
    fields: [{ key: "repo", type: "string" },
             { key: "repo_filename", type: "string" },
             { key: "allow_prereleases", type: "bool" }] },
  { name: "GitlabUpdater", labelKey: "managerGitlab",
    fields: [{ key: "repo_url", type: "string" },
             { key: "repo_filename", type: "string" }] },
  { name: "CodebergUpdater", labelKey: "managerCodeberg",
    fields: [{ key: "repo", type: "string" },
             { key: "repo_filename", type: "string" },
             { key: "allow_prereleases", type: "bool" }] },
  { name: "ForgejoUpdater", labelKey: "managerForgejo",
    fields: [{ key: "repo_url", type: "string" },
             { key: "repo_filename", type: "string" },
             { key: "allow_prereleases", type: "bool" }] }
]

function managersByName() {
  var map = {}
  for (var i = 0; i < MANAGERS.length; i++) map[MANAGERS[i].name] = MANAGERS[i]
  return map
}

// Label for a manager config key; unknown keys fall back to the raw key so
// a newer backend never blanks the form.
function fieldLabel(key) {
  return strings.sourceFieldLabels[key] || String(key)
}

function removeById(id) {
  var next = []
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].id !== id) next.push(state.items[i])
  }
  if (next.length === state.items.length) return
  state.items = next
  delete state.jobStates[id]
  changed()
}

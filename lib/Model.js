.pragma library

// Shared state store for the AppImage plugin.
//
// `.pragma library` makes this a single engine-wide instance, so the bar
// widget (badge counts) and every panel instance read and write the same
// state no matter which QML document imported the file. Plain QML/JS has
// no property-change notifications for library scripts, so mutations go
// through the functions below and are announced to `subscribe()`d
// listeners, which mirror the values into their own reactive properties.
//
// Real backend (M1b): items come from `backend/main.py --list-installed
// --json` (see backend/CONTRACT.md, schema_version 1). The store keeps
// the last response as the cache — panels render it instantly and call
// refresh() for a background update (PRD §8: never block the UI).

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
  // Launch failure (F4): the panel can only know a launch failed when the
  // post-launch settle listing says the app never came up (execDetached
  // spawn success says nothing about the AppImage runtime). launchFuse2Hint
  // is appended ONLY when the same listing's runtime block reports fuse2
  // "missing" (Model.launchFailureBanner decides) — text only, naming the
  // library: no install command, no button (repo wording rule; the
  // security gates scan install-command sources).
  launchFailed: "%1 did not start.",
  launchFuse2Hint: "The most common cause is a missing fuse2 runtime (the libfuse.so.2 library).",
  // Spawn failures only now: "never ran at all" (python3 missing or the
  // exec itself failed). Watchdog timeouts get strings.timeout instead —
  // blaming python3 for a slow update sent users hunting the wrong fix.
  // The copy points at the panel's Install Python card because this panel
  // owns the install now — the old hand-run `omarchy pkg add python` line
  // is stale. Hedged "may be missing" on purpose: this string also shows
  // when the backend command itself is empty, not only for missing python3.
  backendMissing: "Backend did not run — python3 may be missing. Reopen the panel and use the Install Python card.",
  // Python dependency card: python3 is the backend's only runtime dep and
  // Omarchy does not guarantee it. Installing a system package is the
  // user's call, so the panel never runs the install itself — the button
  // opens a floating terminal presenting `/usr/bin/omarchy pkg add python`
  // (sudo prompt included) and polls the result. Same pattern as
  // hyprmoncfg.
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
  // exist. Honest by design: nothing was installed and nothing changed.
  pythonInstallPrepFailed: "Could not open the installer terminal — nothing was installed or changed. Try again.",
  // Covers both "unset/empty" and "present but not a private absolute
  // dir" — the same wording the prep script's exit 5 maps to.
  pythonNoRuntimeDir: "The runtime directory is missing or not a private absolute path (owner-only 0700) — install refused.",
  integrateWorking: "Integrating…",
  // Shown on the next open when the panel went away (closed, reloaded)
  // while an integrate was still running: the destroyed panel takes its
  // backend child with it, so the download vanishes mid-transfer.
  integrateInterrupted: "Integration was interrupted — the panel closed before it finished. Nothing was installed; try again.",
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
  // --pick-file outcomes: cancelled stays subtle (the user did it on
  // purpose); unavailable keeps today's path-field UX alive, so it is a
  // status, never an error. Typed errors (timeout / backend-failed /
  // not-local) surface through the backend's own error message and only
  // fall back to pickerPickFailed when a document arrives without one.
  pickCanceled: "No file chosen",
  pickerUnavailable: "No file chooser is available — type or paste the path instead.",
  pickerPickFailed: "The file dialog failed",
  // Confirm-before-integrate preview (#18): the picker first shows what
  // --integrate would do (--preview-integrate, offline + advisory) and
  // only the explicit confirm runs the real integrate command. Esc/back
  // dismisses with zero side effects. The verification.note from the
  // backend is rendered verbatim; the flags drive the prominent
  // reduced-assurance line and the already-integrated notice.
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
  // Drop-to-integrate (#21): a drop only seeds the picker and runs the same
  // preview flow as every other route — it never installs. dropBusy is the
  // ignore-with-status line for a drop racing an in-flight call (nothing is
  // queued); the other two cover the typed rejections of
  // Model.dropUriToPath. Text only: no commands, no buttons.
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
  // Installability (ticket #39): a source that cannot prove its
  // artifact reports "cannot verify" instead of a pending release —
  // the row never offers an update the backend would refuse.
  cannotVerify: "cannot verify",
  // Run state (ticket #42): a second operation is a wait, never a
  // backend failure; it only shows when no other status is up.
  operationBusy: "One operation at a time",
  updateAction: "Update",
  updating: "%1 updating…",
  updated: "Updated to %1",
  upToDate: "Up to date",
  skippedRunning: "Skipped — the app is running",
  // "Update all" (issue #52): one action for every pending app. The click
  // only OPENS the consent card — the card names the apps it would update
  // and the processes it would close, and nothing is spawned until the
  // user accepts. The process line is count-free when a running app's
  // count is not determinable, exactly like the per-app force-quit card:
  // the card never invents a number.
  updateAllTooltip: "Update all",
  updateAllTitle: "Update all pending apps",
  updateAllApps: "Apps: %1",
  updateAllProcessesNone: "No running app will be closed.",
  updateAllProcessesOne: "1 process will be closed to apply the updates.",
  updateAllProcessesMany: "%1 processes will be closed to apply the updates.",
  updateAllProcessesUnknown: "Running apps will be closed to apply the updates.",
  updateAllConfirm: "Update all",
  updateAllKeysHint: "Enter confirms · Esc cancels",
  // Force-quit before update (ticket #27): Update on a running row opens a
  // confirm card first — nothing is spawned until Confirm, and the card
  // names the app and the number of processes the confirm will close
  // (`running_pids` from the listing; the fallback sentence is count-free
  // for a stale flag or an older backend that never reported one).
  // forceQuitApplied is the done-banner's closed-app clause, appended only
  // when the finished document carries a usable force_quit record. SIGKILL
  // is a different sentence: the app had to be forced. forceQuitFailed is
  // the same clause on an `error` document, where the update did NOT apply.
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
  // so this copy owns the banner; the Update button is usable again by
  // definition (the row is not active) and a retry starts a fresh job.
  jobInterrupted: "Update interrupted — it did not finish. Press Update to try again.",
  // Panel chrome (pin button tooltips).
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
  // Per-app update source (F9, --set-update-source). sourceFieldLabels is
  // keyed by manager config key; manager display names resolve through
  // MANAGERS' labelKey so both stay central for i18n.
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

// Escape the three characters a StyledText line would otherwise read as
// markup. The row meta line is assembled as HTML so that ONE bounded,
// elided Text carries it: a Row of separate Texts overflowed its column
// whenever the fragments got long (a version, a pending release and a
// byte-pair progress label together) and painted over the row's action
// buttons.
function escapeHtml(text) {
  return String(text === null || text === undefined ? "" : text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
}

// The row's IDENTITY line: the installed version and the running dot.
// `accent` = draw in the accent colour, otherwise quiet text.
//
// The row's meta used to be one segment list (version · running dot ·
// pending release · job progress) rendered as one StyledText. The update
// status read as part of the version and grew into the row's action
// buttons, so it now lives on its own line (issue #51): two segment
// lists, each rendered as ONE bounded, elided Text.
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
// the panel knows while the row's job is active ("" otherwise) — only the
// panel knows the job state, and an empty string keeps this pure.
//
// A source that cannot prove its artifact (ticket #39) is reported as a
// capability, not an offer: the row says "cannot verify" and the pending
// release text never appears, because the backend would refuse that
// install. Only a row the sweep flagged carries it, so an unswept row
// stays quiet. A row with no pending release and no live job produces no
// segments at all — the status line stays hidden.
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

// Force-quit card sentence (ticket #27): names the app and how many
// processes the confirmed update will close. `runningPids` is the row's
// normalized count: 1 reads as a singular sentence, 2+ as a plural one,
// and 0 (not running by the flag, count not determinable, or an older
// backend that never reported one) keeps the count-free fallback — the
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

// The "Update all" consent card's wording (issue #52). `apps` is the
// pinned list of the apps the batch would update — { id, name, running,
// runningPids } — captured when the card opened, so a refresh cannot
// rewrite what the user is consenting to.
//
// The card names the apps and the total number of processes the accepted
// batch would close. A running app whose count is not determinable (a
// stale flag, or an older backend that never reported one) keeps the
// count-free sentence: the card never invents a total it cannot know.
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
  // Normalized items: { id, name, version, path, running, runningPids,
  //   iconBase, updateVersion, hasUpdate, manager, embeddedSource,
  //   downloadSize }
  // `id` is the desktop_id (the stable identifier CONTRACT.md mandates for
  // --remove); `path` feeds F4's detached launch.
  items: [],
  lastSyncMs: 0,
  lastError: "",
  lastErrorSubject: "",
  // Ticket #47: an armed launch verdict lives HERE, not on the panel
  // object. The shell unloads the panel QML on close (no keepLoaded), which
  // destroys its timers, so a verdict held by the panel could never be
  // settled after the launch click closed it. Any surface's listing can
  // settle it, at any time later: { id, atMs } or null.
  launchVerdict: null,
  status: "",
  // The installed-list envelope's `runtime` verdict (CONTRACT.md, fuse2
  // probe): { fuse2: "present" | "missing" | "unknown" }, mapped by
  // parseRuntime and refreshed by every listing surface (panel refresh,
  // bar startup sync, service heal). "unknown" is the no-claim default —
  // the launch-failure fuse2 hint fires only on exactly "missing".
  runtime: { fuse2: "unknown" },
  // busy.* stay engine-wide booleans for the whole-surface operations
  // (list/remove/integrate, the F2 picker browse and the F6/F8 sweep).
  // Per-app update jobs deliberately have NO flag here: a row's updating
  // state is derived from the app's on-disk job document — see
  // state.jobStates below (ticket #11), so no reload can leave a spinner
  // without a document behind it.
  busy: { list: false, launch: false, remove: false, integrate: false,
          updates: false, pick: false },
  // argv array (e.g. ["python3", "<pluginDir>/backend/main.py"]); null until
  // configured or derived from the manifest source dir.
  backendCommand: null,
  mockMode: false,
  startupSyncClaimed: false,
  // M2 updates: `settings` mirrors the backend's --settings document and
  // `settingsRevision` is the service's reschedule signal (bumped by
  // applySettings — a JS library has no property notifications, so the
  // revision IS the notification). lastUpdatesCheckMs timestamps the last
  // finished sweep; updatesChecking mirrors busy.updates for callers that
  // read the flag by name.
  settings: { enabled: true, intervalMinutes: 360, delayMinutes: 5 },
  settingsRevision: 0,
  lastUpdatesCheckMs: 0,
  updatesChecking: false,
  // Who holds the update-check claim ("panel" | "service" | ""): the
  // claim outlives the caller's QML object, so a panel open may only
  // release a claim the PANEL took — Service.qml runs in a surface that is
  // never unloaded, and stealing its claim would let the panel run a
  // second sweep beside it (see releaseOrphanedRuns).
  updateCheckOwner: "",
  updateCheckToken: 0,
  // Live claims (ticket #42): name -> { owner, token, deadlineMs }.
  claims: {},
  // Background update jobs (ticket #11): desktop_id →
  // { decision, consumed } — decision is a JobDocs.adoptJob-shaped view of
  // the app's CURRENT on-disk job document, consumed is the sign-of-life
  // (document heartbeat) already surfaced to the user. The panel's readers
  // fill this; rows render it and nothing else.
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
// per monitor, but a single backend probe at login is enough (PRD §2). The
// first caller wins; later callers get false.
function claimStartupSync() {
  if (state.startupSyncClaimed) return false
  state.startupSyncClaimed = true
  return true
}

// ---- operations and claims (ticket #42) -----------------------------------
//
// One claim table for the whole engine. A claim is { owner, token,
// deadlineMs }: the owner is the surface that took it ("panel", "service"),
// the token identifies this particular taking (so a stale release cannot
// free a newer claim), and the deadline is the point after which anyone may
// take over. That is what makes a claim survive a surface that dies without
// releasing it — the shell unloads the panel object on close, so its
// Process objects are killed and their onDone callbacks never run.
//
// A failed claim is a WAIT, not an error: the caller reports it as a quiet
// status and never as a backend failure.

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
  // Only the owner may release: a claim that expired was taken over, and
  // the loser's onDone must not free the winner's claim (ticket #42).
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
// exists. The shell's panel Loader unloads the whole panel QML object when
// the panel is closed (no `keepLoaded` in the manifest, shell.qml binds the
// Loader's `active:` to the open set), which destroys the Process objects
// and — Quickshell's kill is `running: false` — SIGTERMs the backend child.
// Every clear site for these flags sits INSIDE the run's onDone callback,
// which a killed run never reaches, and this store is `.pragma library`
// (one engine-wide instance, outliving every panel instance), so the flags
// would survive the reload as a permanent claim: "Integrating…" forever,
// every retry refused by the busy guard, and the update-check claim dead
// for the shell's lifetime. A freshly loaded panel cannot have a run of its
// own in flight, so releasing on open is always correct. `lastError` is a
// real outcome and is never cleared here; only the in-flight status line —
// the same claim as the flags — goes with them.
function releaseOrphanedRuns() {
  var runStatuses = [strings.integrateWorking, strings.checkingUpdates]
  state.busy.list = false
  state.busy.remove = false
  state.busy.integrate = false
  state.busy.pick = false
  // The update-check claim is released only when the PANEL took it:
  // Service.qml's sweep runs in a surface that is never unloaded, so its
  // claim is not an orphan and stealing it would let the panel run a
  // second sweep beside the service's own.
  // Every claim a panel surface took dies with it (ticket #42): the claim
  // table carries the owner, so a reload can free exactly those and nothing
  // the never-unloaded service holds.
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
// panel never spawns the install silently: the Install button opens a
// floating terminal via `omarchy launch floating terminal with presentation`,
// which RUNS the exact command — the visible terminal (and its sudo prompt)
// IS the authorization, the hyprmoncfg-plugin way. The panel itself shows
// the exact command it will run; the terminal does NOT echo it (it prints
// the Omarchy logo and the install output), so no copy may claim the
// terminal "shows" the command.
//
// The terminal-side command and the panel-side probe coordinate through two
// marker files under $XDG_RUNTIME_DIR: ".complete" is touched on success,
// the exit status (a number, "130" = Ctrl-C) is written into ".failed". The
// panel polls the probe every second while the terminal is running. All of
// these are pure string/argv builders and a pure exit-code mapper so the
// node harness can pin them without a process.
//
// Shell-source invariant (security round 3): `omarchy launch floating
// terminal with presentation <cmd>` turns <cmd> into bash SOURCE
// (presentation_script="…; $cmd; …" → bash -c), so pythonInstallCommand()
// is ZERO-ARGUMENT and returns a CONSTANT string. Environment-derived
// values never appear as interpolated source — the terminal shell reads
// XDG_RUNTIME_DIR itself via "${XDG_RUNTIME_DIR:?}" expansions (the `:?`
// fails the command visibly if unset), and the runtime dir the panel does
// pass travels as argv DATA to the prep step (`sh -c script sh "$1" …`),
// never inside a shell string. The prep script re-checks what the panel's
// cheap pythonRuntimeDirUsable() gate cannot see: the dir exists, is owned
// by the euid and is mode 700. scripts/ci/security-gates.sh rule
// shell-source-constancy enforces the invariant mechanically.

var PYTHON_MARKER_BASE = "appimage-plugin-python-install."

function pythonMarkerPaths(runtimeDir) {
  var dir = String(runtimeDir || "")
  return {
    failure: dir + "/" + PYTHON_MARKER_BASE + "failed",
    complete: dir + "/" + PYTHON_MARKER_BASE + "complete"
  }
}

// The exact shell line the floating terminal presents and runs. CONSTANT
// SOURCE, by the invariant above: this function takes no argument, and the
// only variable-shaped tokens in the result are the terminal shell's own
// "${XDG_RUNTIME_DIR:?}" expansions — data inside double quotes, never an
// interpolated runtime value (the presentation wrapper feeds the whole
// string to bash as source). Absolute executable paths because the
// presented line must not depend on PATH either. POSIX sh only
// ([ ... ] not (( ... ))) — the presentation terminal is whatever the
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

// argv for Process.startDetached: /usr/bin/omarchy opens its floating
// terminal with the presentation wrapper around the (constant) command —
// visible, cancellable, and gated by sudo. Absolute argv[0] keeps the one
// hop execvp could have resolved from PATH on the fixed-path invariant.
// Nothing here runs without the user having pressed the panel's Install
// button.
function pythonInstallArgv() {
  return ["/usr/bin/omarchy", "launch", "floating", "terminal", "with",
          "presentation", pythonInstallCommand()]
}

// Panel-side cheap gate on XDG_RUNTIME_DIR, run before anything spawns:
// false for empty, for anything not starting with "/", and for any
// "/"-split component equal to ".." (traversal). Purely syntactic — the
// authoritative filesystem check (is a directory, owned by the euid,
// mode 700) is pythonInstallPrepScript() below.
function pythonRuntimeDirUsable(runtimeDir) {
  var dir = String(runtimeDir || "")
  if (dir === "" || dir.substring(0, 1) !== "/") return false
  var parts = dir.split("/")
  for (var i = 0; i < parts.length; i++) {
    if (parts[i] === "..") return false
  }
  return true
}

// Prep step for `sh -c`, run by the panel before the terminal launches.
// CONSTANT source that only ever sees data through positional parameters:
//   $1  the runtime dir        $2  failure marker       $3  complete marker
// Exit 5 = runtime dir refused (not absolute, not a directory, not owned
// by the euid, or not mode 700) — the authoritative half of
// pythonRuntimeDirUsable(); any marker cleanup then happens only for an
// accepted dir. `-- ` and `2>/dev/null` keep odd values/absent stat from
// leaking into the verdict.
function pythonInstallPrepScript() {
  return "case \"$1\" in /*) ;; *) exit 5;; esac"
    + "; [ -d \"$1\" ] || exit 5"
    + "; [ -O \"$1\" ] || exit 5"
    + "; [ \"$(/usr/bin/stat -c %a -- \"$1\" 2>/dev/null)\" = \"700\" ] || exit 5"
    + "; /usr/bin/rm -f -- \"$2\" \"$3\""
}

// argv for the prep Process: script + positional data only. The runtime
// dir is argv here (never inside the shell source), and the marker paths
// ride along as ordinary arguments the script reads as "$2"/"$3". argv[0]
// and the "$0" label are the absolute /usr/bin/sh — the same fixed-path
// rule as /usr/bin/omarchy, /usr/bin/rm and /usr/bin/stat; nothing in the
// exec path resolves through the caller's PATH.
function pythonInstallPrepArgv(runtimeDir) {
  var marker = pythonMarkerPaths(runtimeDir)
  return ["/usr/bin/sh", "-c", pythonInstallPrepScript(), "/usr/bin/sh",
          String(runtimeDir || ""), marker.failure, marker.complete]
}

// Probe/poll body, meant for `sh -c`. Exit codes double as state:
//   0  python3 present          1  missing
//   2  install failed (stdout: the captured exit status; "130" = canceled)
//   3  installing and still waiting (markers say the terminal is at work)
// While installing ("$3" = "1") the marker check runs first: a ".failed"
// marker short-circuits to 2, no markers yet means keep waiting.
function pythonProbeScript() {
  return "if test \"$3\" = \"1\"; then"
    + " if test -f \"$1\"; then cat \"$1\"; exit 2; fi"
    + "; if ! test -f \"$2\"; then exit 3; fi; fi"
    + "; if command -v python3 >/dev/null 2>&1; then exit 0; fi"
    + "; exit 1"
}

// Full argv for the panel's probe Process. failurePath/completePath come
// from pythonMarkerPaths(); installing flips the marker-aware poll mode.
// argv[0] and the "$0" label are the absolute /usr/bin/sh, matching the
// fixed-path invariant of the prep/install argv builders above.
function pythonProbeArgv(failurePath, completePath, installing) {
  return ["/usr/bin/sh", "-c", pythonProbeScript(), "/usr/bin/sh",
          String(failurePath || ""), String(completePath || ""),
          installing === true ? "1" : "0"]
}

// Pure mapping of a finished probe onto panel state — one of "present",
// "missing", "waiting", "canceled" or "failed". Kept out of the QML so the
// harness pins the semantics (especially the "130" → canceled rule).
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
  // Only installable updates count (ticket #39): a badge must never promise
  // an install the backend would refuse for want of a publisher digest.
  var n = 0
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].hasUpdate && state.items[i].installable !== false) n++
  }
  return n
}

// The ids of the apps a batch update would touch (issue #52): every app
// with a pending release the backend would accept. The rule is the
// badge's own rule — a pending release whose verdict is not false — so the
// "Update all" action can never offer an install the backend would
// refuse (ticket #39), and never names an app without a pending release.
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
//   --preview-integrate      → {"schema_version":1, "result":"preview",
//                               "source":{…}, "verification":{…},
//                               "destination":{…}, "app":…|null, flags}
//   --list-updates/--fetch-updates → {"schema_version":1, "updates":[<app>,…]}
//   --settings                     → {"schema_version":1, "settings":{…}}
//   --pick-file                    → {"schema_version":1, "result":"picked",
//                                     "path":…, "backend":…} | "cancelled" |
//                                     "unavailable" | "error" (+reason)
//   error (exit != 0)        → {"schema_version":1, "result":"error",
//                               "error":"<message>"}
// A bare array is also accepted for list responses so old captures keep
// parsing. Older captures missing the update keys (manager, download_size,
// …) normalize to their empty values.

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
// (CONTRACT.md filesystem layout); the extension is not in the JSON, so we
// keep the extension-less base and let the view try png/svg/xpm in turn.
function iconBaseFor(app) {
  var path = String(app.path || "")
  if (path === "") return ""
  return dirname(path) + "/.icons/" + basenameNoExt(path)
}

// The installed desktop entry's Name carries a ` (<version>)` suffix
// (provider.install_file's KEEP branch) so the LAUNCHER can show the
// version — but the panel row already prints the version in its meta
// line, so rendering the desktop Name verbatim echoed the version twice
// ("Escrcpy (3.3.1)" above "3.3.1 • running"). Worse, GearLever's
// version fallback invents a version when the AppImage ships none: it is
// the first six hex characters of the file's md5
// (provider._get_app_version(return_hash=True)), which is why a freshly
// integrated app rendered as "Terax (77efdf)" — a hash, not a version.
// Drop a trailing parenthetical that IS the app's version, and one that
// has that hash shape when the backend reports no version at all; any
// other parenthetical belongs to the app's own name and stays.
var HASH_VERSION_RE = /^[0-9a-f]{6}$/

function displayName(name, version) {
  var text = String(name || "")
  var match = /^([\s\S]*) \(([^()]*)\)$/.exec(text)
  if (!match) return text
  var inner = match[2]
  // Strip the suffix when it merely repeats the version, and strip a
  // 6-hex-hash suffix unconditionally: that shape is GearLever's md5
  // fallback baked into an old install's desktop Name, so it is garbage
  // whichever version the backend now reports.
  var strip = inner !== "" && (inner === String(version || "")
    || HASH_VERSION_RE.test(inner))
  return strip ? match[1].replace(/\s+$/, "") : text
}

// The listing's pending triple (issue #50): the backend's durable record
// owns the verdict, and every listing re-reads it —
//   pending_update      → hasUpdate
//   pending_version     → updateVersion ("" when the record names no
//                         version, e.g. a static source)
//   pending_installable → installable (true / false / null)
// An older backend reports none of them: updateVersion then falls back to
// available_version, installable to the listing's own installable key, and
// hasUpdate stays false — nothing is pending until a check says so.
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
  // desktop_id is the stable id (same-named apps coexist per CONTRACT.md);
  // when the backend reports null, the absolute path is the fallback —
  // --remove resolves exact paths before desktop ids anyway.
  var id = entry.desktop_id || entry.desktopId || entry.path || entry.id || ""
  if (String(id) === "") return null
  var item = {
    id: String(id),
    name: displayName(entry.name || id, entry.current_version || entry.version),
    // The version the backend reports (X-AppImage-Version only) — empty
    // when the AppImage carries none, never the md5 fallback that the
    // backend's own display name used to leak (see displayName).
    version: String(entry.current_version || entry.version || ""),
    path: String(entry.path || ""),
    // The app's own Exec arguments as the backend reports them
    // (`exec_arguments`), e.g. "--no-sandbox %U". The panel forwards the
    // real flags on launch and drops the desktop field codes it has
    // nothing to fill. Absent (older captures) means no extra arguments.
    execArguments: String(entry.exec_arguments || entry.execArguments || ""),
    // Ticket #32: the launch command as argv, built by the backend's ONE
    // launch builder (the listing's `launch_argv` key) — the panel spawns
    // it instead of assembling `env DESKTOPINTEGRATION=1 ...` in QML.
    // Absent (an older backend or capture) means null and the panel
    // falls back to the bare path.
    launchArgv: Array.isArray(entry.launch_argv)
      ? entry.launch_argv.map(String) : null,
    // Ticket #35: the argv tail for the backend's `--exec-with-scale`
    // entry point (the listing's additive `launch_scale_argv` key) — the
    // image plus its own arguments, field codes dropped. The panel spawns
    // [backendCommand..., "--exec-with-scale", *launchScaleArgv] so the
    // panel launch shares the launcher's scale mechanism. Absent (an
    // older backend or capture) means null and the panel falls back to
    // launchArgv, then the bare path.
    launchScaleArgv: Array.isArray(entry.launch_scale_argv)
      ? entry.launch_scale_argv.map(String) : null,
    running: entry.running === true,
    // How many processes belong to this AppImage (the backend's additive
    // `running_pids`, ticket #27) — the force-quit card names it. 0 when
    // the app is not running, when the count is not determinable, and on
    // an older backend that never reported one: the card then falls back
    // to its count-free sentence.
    runningPids: Math.max(0, toInt(
      entry.running_pids !== undefined ? entry.running_pids : entry.runningPids, 0)),
    iconBase: "",
    updateVersion: pendingVersionOf(entry),
    // hasUpdate marks "a newer release is pending for this app" and is
    // what updateCount() badges on — a static-source update carries no
    // version string, so the marker alone cannot tint the badge.
    // The listing's pending_update is the verdict (issue #50): the
    // backend's durable record is the owner, and every listing re-reads
    // it, so the marker survives the 10 s poll instead of flapping. An
    // older backend reports no pending triple: the marker stays false
    // and only a check's own response (applyUpdates) can set it.
    hasUpdate: entry.pending_update === true,
    // Update plumbing (M2): manager names the configured custom source
    // ("" when none), embeddedSource marks an AppImage that ships its own
    // updater, downloadSize is bytes (0 = unknown/null).
    manager: String(entry.manager || ""),
    // Installability (ticket #39, additive keys): true = a fetch under the
    // required verification policy would be attempted, false = the source
    // would refuse the install (no usable publisher digest), null = not
    // determinable (no sweep yet, no source, offline). A missing key from
    // an older backend means null, which keeps today's behaviour.
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

// Normalize the installed-list envelope's additive `runtime` block
// (CONTRACT.md, fuse2 probe) onto { fuse2: "present" | "missing" |
// "unknown" }. Defensive by design: the block is promised on every
// successful listing, but absent, null, non-object and array shapes all
// normalize to "unknown", as does any fuse2 value outside the contract's
// three states. Unknown keys anywhere are tolerated and ignored, and the
// result is a fresh object (callers may hand it on without sharing the
// parsed document's subtree). "unknown" means "no claim": the panel's
// fuse2 hint may fire only on exactly "missing".
function parseRuntime(doc) {
  var block = doc && typeof doc === "object" && !Array.isArray(doc)
    ? doc.runtime : null
  var fuse2 = block && typeof block === "object" && !Array.isArray(block)
    ? String(block.fuse2 || "") : ""
  return {
    fuse2: fuse2 === "present" || fuse2 === "missing" ? fuse2 : "unknown"
  }
}

// Banner text for a failed launch (the settle listing says the app never
// came up): the factual base line, plus the fuse2 hint line ONLY when the
// same listing's runtime block reports exactly "missing" — "present" and
// "unknown" (and any foreign state) keep the banner about the failure
// alone, per CONTRACT.md ("unknown" is no claim). The panel calls this
// only on a failed listing, so a successful launch can never produce the
// hint. Text only by the repo wording rule: the hint names the library,
// never a command, never a button.
function launchFailureBanner(appName, fuse2State) {
  var base = formatCount(String(appName || ""), strings.launchFailed)
  if (fuse2State !== "missing") return base
  return base + "\n" + strings.launchFuse2Hint
}

// Parse an --list-updates / --fetch-updates response: the same normalized
// items as the installed list. notified/offline only ride on
// --fetch-updates (--list-updates leaves them false); offline:true means
// the sweep never reached the network, so callers must not treat its empty
// list as "everything is up to date".
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

// Parse a --settings / --set-setting response into the store's shape.
// Tolerant defaults mirror the backend contract (360-minute cadence, first
// check 5 minutes after login, checks on); the 15-minute floor matches
// --set-setting's own minimum so a bad document cannot tighten the loop.
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
// { ok, result, message, error, app } with result one of "integrated",
// "already-integrated", "removed" ("updated", "up-to-date",
// "skipped-running" for --update) or "error" per CONTRACT.md — any
// non-error result passes through as ok.
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

// Parse a --pick-file response (CONTRACT.md "### --pick-file [--json]"):
// returns { ok, result, path, backend, reason, message, error } with result
// one of "picked" / "cancelled" / "unavailable" (all exit 0) or "error"
// (exit 1, reason one of "timeout" / "backend-failed" / "not-local").
// Defensive by contract: unknown envelope keys are ignored, an unknown
// result passes through like parseActionJson's (ok unless "error") so a
// newer backend can add outcomes without breaking the panel, and `path` is
// only surfaced on a real "picked".
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

// Parse a --preview-integrate document (CONTRACT.md, schema_version 1)
// into the shape the picker's preview card renders. Defensive by contract:
// the preview document is advisory and its keys are additive, so unknown
// keys are ignored and every missing/null sub-key normalizes to a safe
// default — never undefined, never a crash. A document that is not a
// preview (the error document a bad path returns, an action result,
// garbage) is rejected with an error so the panel can degrade to a status
// message with the picker still usable; it must never render a made-up
// preview.
function parsePreviewJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return { ok: false, preview: null, error: parsed.error }
  var doc = parsed.doc
  if (String(doc.result || "") !== "preview") {
    return { ok: false, preview: null, error: "Backend response was not a preview document" }
  }
  // Sub-objects may be absent or not objects in a sloppy future document;
  // treat each as empty rather than trusting it.
  function sub(value) {
    return value && typeof value === "object" ? value : {}
  }
  var source = sub(doc.source)
  var verification = sub(doc.verification)
  var destination = sub(doc.destination)
  var app = doc.app && typeof doc.app === "object" ? doc.app : null
  // filename/path are null for URL sources (the final name comes from the
  // artifact's embedded desktop entry at integrate time) — mapped to ""
  // so QML string bindings never see null.
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

// Tolerant document parse (shared with lib/Backend.js's strategy): strict
// first, then recover the outermost {…}/[…] if a stray line sneaks in.
function parseDocument(text) {
  var raw = String(text || "").trim()
  if (raw === "") return { ok: false, doc: null, error: "Empty backend response" }
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { doc = null }
  if (doc === null) {
    var start = raw.indexOf("{")
    var startArray = raw.indexOf("[")
    if (startArray !== -1 && (start === -1 || startArray < start)) start = startArray
    var end = raw.lastIndexOf("}")
    var endArray = raw.lastIndexOf("]")
    if (endArray !== -1 && endArray > end) end = endArray
    if (start >= 0 && end > start) {
      try { doc = JSON.parse(raw.substring(start, end + 1)) } catch (e2) { doc = null }
    }
  }
  if (doc === null || typeof doc !== "object") {
    return { ok: false, doc: null, error: "Backend response was not JSON" }
  }
  return { ok: true, doc: doc, error: "" }
}

// ---- drop-to-integrate (#21) -------------------------------------------------
//
// Pure URI→path conversion for the drop targets (panel window + bar widget).
// A DropArea hands over a list of URL strings; only file:// URIs are usable
// (URL-text drops are out of scope by the spec — the integrate field still
// takes pasted URLs by hand). Typed result, pinned by the parse harness:
//   { ok: true,  path: "<decoded local path>", ignored: <others skipped>,
//     reason: "" }
//   { ok: false, path: "", ignored: <entries seen>,
//     reason: "empty" | "not-file" | "no-appimage" }
//   - "empty":      nothing usable was dropped at all (empty list / blanks)
//   - "not-file":   entries exist but none is a plain file:// URI with an
//                   empty authority — http(s) links, plain text garbage,
//                   file://host/… shares, malformed %-sequences
//   - "no-appimage": file URIs exist but none ends in .AppImage (any case)
// A multi-URI selection picks the FIRST AppImage and reports every other
// entry as ignored. The decoded path is data for the picker field — the
// preview/confirm flow owns whatever happens next; this helper can never
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
  // Every listing is the fresher verdict: a document without the runtime
  // block (older backend) resets the store to "unknown" so a stale
  // "missing" can never survive into the launch-failure hint.
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
  // caller must not clear the error it settled (ticket #47).
  return settled
}

// Arm the verdict for `id`; the next listing that began after this moment
// decides it (see settleLaunchVerdict). Re-arming replaces the previous one.
function armLaunchVerdict(id, nowMs) {
  var key = typeof id === "string" ? id : ""
  if (key === "") return false
  state.launchVerdict = { id: key,
                          atMs: typeof nowMs === "number" ? nowMs : Date.now() }
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

// An error is an outcome, a status is a progress line (ticket #48): the two
// no longer displace each other. `subject` is the app the error concerns
// ("" for a call that concerns none), which is what lets a later outcome for
// that same app — and nothing else — replace it.
// Ticket #48: an error about one app must say which app it is about.
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

// Report a second operation that had to wait (ticket #42). It must never
// become an error and must never wipe the in-flight status line, so it only
// speaks when nothing else is showing; the panel's status timer clears it
// like any other notice.
function setTransientStatus(message) {
  var text = String(message || "")
  if (text === "" || state.status !== "") return
  state.status = text
  changed()
}

function markRunning(id, running) {
  var item = findItem(id)
  if (!item || item.running === (running === true)) return
  item.running = running === true
  changed()
}

// Merge a finished update sweep into the installed list without a rescan.
// The sweep result is authoritative for the apps it REPORTS: every
// reported id is marked hasUpdate (even static sources whose
// available_version is null — their update is real but versionless) and
// refreshes its source metadata (manager/embeddedSource/downloadSize —
// sweep data is fresher than the list cache).
//
// The sweep never CLEARS an app it does not report (issue #50): the
// response lists only the apps that have a pending release, so an app
// whose check failed is simply absent while the backend keeps that app's
// last known verdict in its durable record. Clearing it here would make
// the marker flap — gone on the sweep, back on the next listing. The
// listing is what clears a verdict: it reports pending_update false for
// an app the last check found up to date.
//
// Only update metadata moves — names, paths and running state still
// belong to --list-installed.
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

// A finished update retires that app's pending verdict (issue #50): the
// row's marker and the badge drop it at once instead of at the next
// listing. The backend's record is the owner — it deletes the app's entry
// after a successful update — so this is only the mirror.
//
// The clearing lives HERE, in the shared store, because the shell
// unloads the panel QML on close: a verdict held on the panel object
// could never be settled once the panel is gone.
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

// ---- background update jobs (per-row, disk-derived; ticket #11) -------------
//
// A row's updating state is NOT an engine-wide flag: it is the adopted view
// of the app's on-disk job document (lib/JobDocs.js; CONTRACT.md "Job
// documents"). The panel reads the documents and hands the decisions here;
// list rows render state.jobStates and nothing else — which is the
// structural fossil-spinner fix: after a shell/plugin reload mid-download,
// the next panel open re-derives every row from disk and can never spin
// without a document behind it.
//
// entry shape: { decision, consumed } — decision is a JobDocs.adoptJob
// decision (state idle|active|interrupted|done|error); consumed is the
// sign-of-life (document heartbeat, or -1 for a corrupt document) whose
// terminal/interrupted outcome has already been surfaced, so a settled
// update toasts once per job instead of on every poll tick or reopen. The
// marker lives in this engine-wide singleton: it survives panel open/close
// and dies with the shell — where surfacing the outcome once more is
// correct (the user may have missed it).

function jobEntry(id) {
  return state.jobStates[id] || null
}

// Compact identity of the user-visible decision shape, for poll de-dup:
// re-adopting an unchanged document must not churn the subscribers.
function jobDecisionKey(d) {
  return [d.state, d.phase, d.progress, d.message, d.newVersion, d.status,
          d.errorText, d.stale, d.pidDead].join("|")
}

// Store an adopted decision for `id`. changed() fires only when the
// user-visible shape actually moved.
//
// A job that ended in "updated" retires that app's pending verdict
// (issue #50): the badge and the row drop it at once instead of at the
// next listing. The clearing is here, in the shared store, because the
// shell unloads the panel QML on close — a verdict settled on the panel
// object would never be settled at all.
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
  // Both terminal truths retire the verdict: the update was applied, or
  // the update pipeline's own availability check proved there is nothing
  // newer to install (the backend drops the record entry in both cases).
  // A job that failed, or a 'skipped-running' one, keeps it — nothing was
  // proven about the release.
  if (decision.state === "done"
      && (decision.status === "updated" || decision.status === "up-to-date")) {
    retirePendingUpdate(key)
  }
}

function jobDecision(id) {
  var e = state.jobStates[id]
  return e ? e.decision : null
}

// The per-app single flight (ticket #11): true only while THIS app's
// adopted document says active. Other apps are never blocked — parallel
// updates are the point; the backend refuses a second job per app anyway.
function jobActive(id) {
  var d = jobDecision(id)
  return !!d && d.state === "active"
}

// Done-banner text for a terminal job decision (the panel's `updated` and
// `up-to-date` branches, ticket #27): the pre-#27 base line — the version
// line when the document carries one, else the backend's own message (the
// caller supplies the up-to-date fallback for an empty message) — plus the
// closed-app clause ONLY when the document recorded a force_quit.
// `decision.forceQuit` is JobDocs.normalizeForceQuit's output: null for
// absent or malformed values, {count, sigkill} for a real record. A record
// whose count normalized to 0 cannot honestly claim a close (the writer
// only records processes it actually signalled), so it reads as no close.
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
// JobDocs.normalizeForceQuit's output (null for absent/malformed values),
// and a record whose count normalized to 0 cannot honestly claim a close
// (the writer only records processes it actually signalled), so it reads
// as no close. `failed` picks the error wording: the update did not apply,
// so "to apply the update" would be a lie. SIGKILL reads differently from
// a polite TERM — the app had to be forced, which is worth telling.
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
// its message, else the caller's fallback — plus the closed-app clause
// when the job closed the app and THEN failed (the app is gone, so the
// error cannot be read without it).
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

// Record that `id`'s outcome for `signOfLife` has been surfaced. A
// corrupt/truncated document has no usable heartbeat; -1 marks its
// once-per-session surfacing so the poll cannot re-toast it forever.
function markJobConsumed(id, signOfLife) {
  var e = state.jobStates[id]
  var mark = Number(signOfLife)
  if (!e || !isFinite(mark) || mark === 0 || e.consumed === mark) return
  e.consumed = mark
  changed()
}

// Drop job views for apps that left the installed list — a removed app
// keeps no state that could hold the poll open for a ghost.
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
// Mirror of the managers --set-update-source accepts (CONTRACT.md): labelKey
// resolves through `strings` so display names stay central for i18n, and
// `fields` drives the per-app source editor — type "string" renders a text
// field, type "bool" a switch (serialized as true/false). Keep in sync with
// the backend's manager registry.

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

// Label for a manager config key in the source editor; unknown keys fall
// back to the raw key so a newer backend never blanks the form.
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

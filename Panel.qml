import QtQuick
import Qt.labs.folderlistmodel
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend
import "lib/JobDocs.js" as JobDocs

// Main AppImage manager surface.
//
// The plugin declares both "bar-widget" and "panel" kinds, so this panel
// is mounted by the shell's panel loader (summon path) as a standalone
// layer-shell surface — the same shape omarchy.menu and the dev gallery
// use — while BarWidget.qml is just the launcher button in the bar.
//
// Lifecycle: the qs.Ui.Panel base supplies `opened`, open()/close()/
// toggle(), and the popout-switch pair; `open(payloadJson)` below extends
// open() for the loader's summon payload. Escape closes through
// PanelKeyCatcher, exactly like the first-party panels.
//
// M1b wiring: every action runs backend/main.py (see backend/CONTRACT.md)
// through dedicated Process/Timer pairs in this file — a shared short-call
// pair for refresh-style calls, a dedicated pair for the --update --async
// spawn (so refresh traffic can never fail it synthetically) and the
// python probe pair —
// F1 list (cache-first, the
// store renders instantly and refreshes in the background), F2 integrate
// (inline picker: Quickshell ships no file dialog, so a path field, a
// Browse button, Downloads quick-picks and drop-to-integrate stand in;
// every action previews first through the offline --preview-integrate
// verb and installs only on explicit confirm — #18, #19, #21), F3 remove
// (two-click inline confirm, keyed on desktop_id), F4 launch (detached
// exec of the AppImage path), F5 bar badge (observer over the same Model
// store).
//
// M2 wiring: F6 check updates (--list-updates with a long watchdog; the
// offline error document surfaces its own message), F7 one-click update
  // per row as a DETACHED BACKGROUND JOB (--update --async: the spawn
  // answers within milliseconds, the transfer runs outside this panel's
  // process tree, and every row renders the app's on-disk job document —
  // reader lib/JobDocs.js — so no in-memory flag can outlive a reload and
  // no spinner can exist without a document behind it), F9 per-row
  // update-source editors plus the F8 cadence knobs in a settings card
  // toggled by the header gear. The F8 background service itself lives in
  // Service.qml — this panel only edits what it runs on.
Panel {
  id: root
  moduleName: "io.github.glasschan.appimage"
  ipcTarget: "io.github.glasschan.appimage"
  // The panel owns its IPC handler so extra verbs (refresh, list) can be
  // exposed alongside the standard lifecycle set; the base would install
  // a second handler on the same target if left enabled.
  manageIpc: false

  // Injected by the shell's panel loader when summoned.
  property var shell: null
  property var manifest: null
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  readonly property string homeDir: Quickshell.env("HOME") || ""

  // Cache policy: opening the panel shows the cached list immediately and
  // only rescans when the cache is older than this (PRD §2: no rescan on
  // every summon). The Refresh button always forces one.
  readonly property int staleAfterMs: 60000
  // Integrating means squashfs extraction + a file copy; give it room.
  readonly property int integrateTimeoutMs: 120000
  // --integrate from a release URL downloads the whole AppImage inside
  // this panel's process — the watchdog must outlast the transfer, not
  // the UI. (--update no longer needs a long budget here: it runs as a
  // detached background job and the spawn answers in milliseconds.)
  readonly property int integrateUrlTimeoutMs: 600000
  // Update sweeps walk every configured release source over the network.
  readonly property int checkTimeoutMs: 120000
  // --pick-file: the backend bounds probe + portal dialog + zenity under
  // one 600 s whole-operation deadline (CONTRACT.md); the watchdog matches
  // it instead of undercutting it, so a slow portal is never reaped while
  // the backend still considers the dialog alive.
  readonly property int pickTimeoutMs: 600000
  // How long the two-click remove confirmation stays armed.
  readonly property int confirmResetMs: 4000
  // Grace before the post-launch refresh so `ps` can see the new process.
  readonly property int launchSettleMs: 1500
  // Launch-failure banner (F4): the id of the app awaiting its post-launch
  // settle verdict ("" when none) and the moment the launch was clicked.
  // execDetached cannot report the AppImage runtime's own death (a type-2
  // AppImage without fuse2 exits right after spawn), so the verdict comes
  // from the first completed listing that STARTED after the click — a
  // listing already in flight at click time has a pre-spawn ps snapshot
  // and must never judge the launch. On a failed verdict the banner gets
  // the fuse2 hint line only when that same listing's runtime block says
  // "missing" (Model.launchFailureBanner decides).
  // Status notices fade on their own after this long (matching the
  // backend's desktop-notification expire time); errors never auto-hide.
  readonly property int statusTimeoutMs: 5000

  // ---- background update jobs (F7 reworked, ticket #11) ----------------------
  // Row update state is derived from the backend's on-disk job documents
  // (CONTRACT.md "Job documents", reader lib/JobDocs.js): every open and
  // every poll tick re-reads the documents, so a reload mid-download
  // re-derives everything from disk and no spinner can outlive its doc.
  readonly property int jobPollMs: 3000
  // $XDG_STATE_HOME/io.github.glasschan.appimage/jobs (jobs.py's path rule;
  // XDG_STATE_HOME defaults to ~/.local/state like XDG_DATA_HOME).
  readonly property string jobsDir: (Quickshell.env("XDG_STATE_HOME")
    || (homeDir + "/.local/state")) + "/io.github.glasschan.appimage/jobs"
  // desktop_id → adopted decision (JobDocs shape), mirrored from the store
  // so row bindings stay reactive; jobsBusy gates both polls below.
  property var jobStates: ({})
  property bool jobsBusy: false
  // Sequential single-document read state for jobFileView: at most one
  // job document is being read at any time (a FileView repointed mid-read
  // aborts the old load, so the queue never races on one instance).
  property var jobReadQueue: []
  property string jobReadingId: ""
  // Click grace (per id → Date.now()): a "no document" read landing
  // within jobStartGraceMs of a job-started reply is a race with the
  // grandchild's FIRST write, not evidence that nothing runs — during
  // the grace it must not overwrite the provisional active decision
  // (that would kill the spinner AND the poll before the document ever
  // appears). Past the grace an absent document honestly means the job
  // died before writing anything, and idle wins.
  readonly property int jobStartGraceMs: 10000
  property var jobStartGraces: ({})

  // Force-quit confirm card (ticket #27): Update on a row whose last
  // --list-installed poll said the app runs opens this card first. The id,
  // name and process count are pinned at click time (the row's model data
  // may be replaced by the refresh that follows); nothing is spawned until
  // confirmForceQuit, and cancel clears the trio with zero side effects.
  // The count comes from the backend's additive `running_pids`; 0 means
  // "not known" (a stale flag, or an older backend) and the card falls
  // back to its count-free sentence.
  property string forceQuitId: ""
  property string forceQuitName: ""
  property int forceQuitPids: 0

  // "Update all" consent card (issue #52): one action for every pending
  // app. The click only OPENS this card — the batch is spawned after the
  // user accepts it, and dismissing the card starts nothing. The apps are
  // pinned at open time (id, name, running, running_pids), so a refresh
  // cannot rewrite what the user is consenting to.
  property bool updateAllOpen: false
  property var updateAllApps: []
  // The card's wording, derived from the pinned app list (the apps it
  // names and the processes it would close).
  readonly property var updateAllSummary: Model.updateAllSummary(root.updateAllApps)
  // The ids whose background job still has to be spawned. The spawn pair
  // is one-at-a-time, so the queue advances when each spawn answers.
  property var updateAllQueue: []

  // Current Backend.run context (null while idle); the Process and Timer
  // signal handlers feed it back into lib/Backend.js.
  property var runCtx: null

  // Backend.run context for the --update --async spawn (null while idle).
  // The spawn owns a dedicated Process/Timer pair (updateSpawnProc below):
  // routing it through the shared short-call pair instead would let any
  // in-flight re-list or update check synthetically fail the click via
  // callBackend's single flight.
  property var updateSpawnCtx: null

  // ---- python3 dependency (authorized install) ------------------------------
  // The probe settles python presence once per open (and every second while
  // an authorized install runs). Until it answers the panel assumes nothing;
  // a confirmed-missing python3 swaps the body for the install card, whose
  // button opens a visible floating terminal presenting `/usr/bin/omarchy
  // pkg add python` — the terminal (and its sudo prompt) is the
  // authorization, the marker files under XDG_RUNTIME_DIR are its report
  // channel. The presented terminal command is a CONSTANT shell source (the
  // omarchy wrapper would run an interpolated string as bash source): it
  // reads XDG_RUNTIME_DIR itself via "${XDG_RUNTIME_DIR:?}" expansion, and
  // the runtime dir the panel holds is validated — syntactically by
  // Model.pythonRuntimeDirUsable(), then authoritatively by the prep step
  // (absolute, a directory, owned by the euid, mode 700) — and travels
  // only as argv data, never inside a shell string.
  property bool pythonStateKnown: false
  property bool pythonMissing: false
  property bool installingPython: false
  // Install-button verify gate (verify-then-start): set when the user
  // presses Install and consumed by the next probe result, which alone
  // decides whether the prep step + terminal may start. The verify probe
  // runs with installingPython=false — a plain presence check, so stale
  // marker files from an old attempt cannot trigger a new terminal.
  property bool installVerifyPending: false
  // Backend.run context for the in-flight probe (null while idle).
  property var probeCtx: null
  readonly property string runtimeDir: Quickshell.env("XDG_RUNTIME_DIR") || ""
  readonly property string pythonFailureMarker: Model.pythonMarkerPaths(runtimeDir).failure
  readonly property string pythonCompleteMarker: Model.pythonMarkerPaths(runtimeDir).complete

  // Mirrored from the shared Model store (no property notifications in a
  // JS library, so a subscription copies state into reactive properties).
  property var items: []
  property bool busyList: false
  property bool busyIntegrate: false
  property bool busyPick: false
  property bool busyRemove: false
  property bool busyUpdates: false
  property string lastError: ""
  property string statusText: ""
  property var unsubscribe: null
  // The ids of the apps a batch update would touch (mirrored from the
  // store: the "Update all" action is enabled exactly while at least one
  // app has a pending release the backend would accept).
  property var pendingIds: []

  // Integrate picker (F2): Quickshell has no FileDialog type (checked
  // against /usr/lib/qt6/qml/Quickshell — no dialog types ship with it),
  // so the picker is a path field, a Browse button backed by the backend's
  // crash-isolated --pick-file dialog (portal first, zenity fallback), and
  // AppImages found in ~/Downloads and ~/downloads.
  property bool pickerOpen: false
  property string pickerPath: ""
  property var pickerFiles: []

  // Confirm-before-integrate preview (#18): pressing the integrate action
  // (field Enter, Go button, Downloads row) first runs --preview-integrate
  // — offline and fast, so its watchdog is a short budget — and renders the
  // preview card; Confirm then runs the existing integrate command and
  // Esc/Cancel dismisses back to the picker with zero side effects.
  // previewInput pins the exact input the shown document was computed from:
  // editing the path dismisses the card, so Confirm can only ever install
  // what the user actually saw previewed.
  readonly property int previewTimeoutMs: 10000
  property bool previewOpen: false
  property bool busyPreview: false
  property string previewInput: ""
  property var previewDoc: null

  // Single-source of the picker-owning composite — a browse dialog, a
  // preview call or an integrate run in flight. Browse, Go and the
  // Downloads quick-picks all disable on exactly this conjunction
  // (#18/#19 guards), so it is combined once here instead of
  // hand-assembled per control. Sites whose guard intentionally differs
  // keep their extra terms separate: Go also requires a non-empty path,
  // the preview card's Confirm/Cancel omit the pick term by design, and
  // dropBusy()/confirmPreview() read the store flags directly.
  readonly property bool pickerBusy: busyPick || busyIntegrate || busyPreview

  // Drop-to-integrate (#21): the bar widget converts its dropped URIs
  // through Model.dropUriToPath and hands the usable path over in the
  // summon payload; open() parks it here and tryConsumePendingDrop runs it
  // once the store is idle (the panel's own DropArea path checks the same
  // guard live and reports a busy drop with a status line instead). The
  // panel window DropArea never queues — this pending pair is transport
  // for a gesture that happened before the panel existed. Closing the
  // panel cancels a still-pending drop (onOpenedChanged).
  property string pendingDropPath: ""
  property int pendingDropIgnored: 0

  // desktop_id whose Remove button is armed for the second click (F3).
  property string confirmRemoveId: ""

  // Settings card (F8): opened by the header gear, mutually exclusive
  // with the integrate picker. The edit* fields are working copies the
  // card mutates; Save pushes them key by key through --set-setting and
  // the store is re-read afterwards (reloadSettings).
  property bool settingsOpen: false
  // Session-only window mode: pinned turns the full-screen overlay into a
  // compact floating window sized to the card (never persisted).
  property bool pinned: false
  property bool settingsBusy: false
  property bool editEnabled: true
  property int editInterval: 360
  property int editDelay: 5
  property var pendingSettingKeys: []

  // Per-app source editor (F9): one editor at a time, keyed by desktop_id.
  // Field values live as strings ("true"/"false" for bools) so the Save
  // argv can pass them straight through as key=value tokens.
  property string sourceEditId: ""
  property string sourceEditName: ""
  property string sourceEditManager: ""
  property var sourceEditValues: ({})
  property bool sourceBusy: false
  // Focus count over EVERY editable text field in the panel (the picker's
  // path field, the source editor's dynamic fields). PanelKeyCatcher must
  // hand raw keys to whichever field is typing (see formEditing), and it
  // has to be blocked for as long as one holds focus: its Keys handler
  // CONSUMES every key it is handed — a handler that only returns still
  // accepts — so an unblocked catcher silently eats typed characters (the
  // force-quit card exposed exactly that: it early-returns on single-letter
  // text keys while it asks, and `blocked` was formEditing only).
  property int textFieldFocusCount: 0
  // Mirrors the per-row manager Dropdown's popupOpen so formEditing can
  // keep blocking the key catcher while the popup (outside this item
  // tree) owns keys — see onPopupOpenChanged on the dropdown.
  property bool sourceDropdownOpen: false

  // Manager choices for the source editor's dropdown, resolved through the
  // strings table so display names stay central for i18n.
  readonly property var managerOptions: {
    var opts = []
    for (var i = 0; i < Model.MANAGERS.length; i++) {
      var mgr = Model.MANAGERS[i]
      opts.push({ value: mgr.name, label: Model.strings[mgr.labelKey] || mgr.name })
    }
    return opts
  }

  // Emitted by the [Integrate] button and the "i" shortcut; opens the
  // inline picker below.
  signal integrateRequested()

  readonly property var strings: Model.strings
  readonly property color foreground: Color.popups.text
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color urgent: Color.urgent
  readonly property string fontFamily: Style.font.family
  readonly property real controlHeight: Style.font.icon
    + Style.spacing.controlPaddingY * 2 + Style.normalBorderWidth * 2

  // argv for the backend CLI. Priority: an explicit setting handed to
  // Model.configureBackend(), then python3 + <pluginDir>/backend/main.py
  // derived from the manifest's source dir, then the canonical install
  // location (the shell strips __sourceDir from third-party manifests —
  // shell.qml publicPluginManifest() — so the manifest hint is usually
  // absent and the PluginRegistry path is the real source of truth).
  readonly property var backendCommand: {
    var configured = Model.state.backendCommand
    if (configured && configured.length > 0) return configured
    var sourceDir = manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : ""
    if (sourceDir === "") sourceDir = homeDir + "/.config/omarchy/plugins/" + moduleName
    if (sourceDir !== "") return ["python3", sourceDir + "/backend/main.py"]
    return []
  }

  readonly property string subtitleText: {
    var parts = []
    var count = items.length
    parts.push(count === 1
      ? strings.itemsOne
      : Model.formatCount(count, strings.itemsMany))
    var running = Model.runningCount()
    if (running > 0) parts.push(Model.formatCount(running, strings.runningCount))
    return parts.join(" · ")
  }

  function shellHost() {
    return shell || null
  }

  // Assemble one row line's segments into a single StyledText: every
  // dynamic fragment is escaped (Model.escapeHtml) and the accent
  // segments carry the accent colour. One bounded, elided Text per line
  // — a Row of separate Texts has no width bound and painted over the
  // row's action buttons (v0.9.1).
  function metaText(segs) {
    var parts = []
    for (var i = 0; i < segs.length; i++) {
      parts.push('<span style="color:'
        + (segs[i].accent ? Color.accent : root.dim)
        + '">'
        + Model.escapeHtml(segs[i].text)
        + "</span>")
    }
    return parts.join(" ")
  }

  function syncFromModel() {
    items = Model.state.items.slice()
    // One snapshot of the operation state (ticket #42): the panel and the
    // service read the same shape, so a second surface can never disagree
    // about who owns a run.
    var ops = Model.snapshot()
    busyList = ops.busy.list
    busyIntegrate = ops.busy.integrate
    busyPick = ops.busy.pick
    busyRemove = ops.busy.remove
    busyUpdates = ops.busy.updates
    // Per-row job views (ticket #11): mirror the adopted decisions into a
    // reactive map — rows read jobStates[id] and nothing else.
    var jobs = {}
    for (var id in Model.state.jobStates) {
      jobs[id] = Model.state.jobStates[id].decision
    }
    jobStates = jobs
    jobsBusy = Model.anyJobActive()
    lastError = Model.state.lastError
    statusText = Model.state.status
    // The pending apps a batch update would touch (issue #52): the header
    // action is enabled exactly while this list is not empty.
    pendingIds = Model.pendingUpdateIds()
    // A bar-widget drop waits here for an idle store (see
    // tryConsumePendingDrop): every busy flag flip announces itself
    // through changed(), so the sync after the last in-flight call lands
    // is the moment the pending preview can start.
    tryConsumePendingDrop()
  }

  // True while any editor inside the panel should own raw keys instead of
  // the key catcher's shortcuts (see the keyCatcher binding below). The
  // picker and settings fields are Loader-created (see the field templates
  // at the bottom of this file), so guard on the loaded items.
  readonly property bool formEditing: (pickerFieldLoader.item
      ? pickerFieldLoader.item.activeFocus : false)
    || textFieldFocusCount > 0
    || root.sourceDropdownOpen
    || (settingsNumbersLoader.item
        ? settingsNumbersLoader.item.anyFieldFocused : false)

  // ---- lifecycle -----------------------------------------------------------

  function open(payloadJson) {
    Model.setMockMode(false)
    if (backendCommand.length > 0) Model.configureBackend(backendCommand)
    // Bar-widget drop handoff (#21): the payload carries the file the user
    // dropped on the bar icon (already converted and validated there).
    // Parked, not run: the cache-first refresh below may occupy the shared
    // backend process, and tryConsumePendingDrop waits that out.
    readDropPayload(payloadJson)
    // Mirror the store. Run flags left by a DESTROYED instance were already
    // released in Component.onCompleted — never here, because the shell
    // re-delivers open() to a live panel.
    syncFromModel()
    // Settle the python3 state first: with it missing, refresh() below is
    // a no-op and the install card takes over the body instead.
    probePython()
    // Cache-first: whatever the store holds renders now; a backend rescan
    // only fires when the cache is empty or stale (never blocks the UI —
    // the Process is asynchronous and the watchdog bounds it).
    if (Model.state.items.length === 0
        || Date.now() - Model.state.lastSyncMs > staleAfterMs) {
      refresh()
    }
    // Adopt whatever the on-disk job documents say right now — this is
    // the fossil-spinner fix: state comes from disk on EVERY open, so a
    // reload mid-download re-renders the running job (or its
    // interruption) instead of resurrecting an in-memory flag.
    refreshJobStates()
    controller.show()
    Qt.callLater(function() {
      if (root.opened && keyCatcher) keyCatcher.forceActiveFocus()
    })
    // Idle-store backstop: if nothing was in flight and nothing starts,
    // no syncFromModel fires after the one above — consume the pending
    // drop once more from the event loop (a no-op when already taken).
    Qt.callLater(root.tryConsumePendingDrop)
  }

  // User-initiated close (Escape, outside click): tell the shell so its
  // openPanelIds map stays consistent and the next toggle summons again.
  // Host-initiated close goes straight to root.close(), which only flips
  // the controller.
  function requestClose() {
    var host = shellHost()
    if (host && typeof host.hide === "function") host.hide(moduleName)
    else close()
  }

  // ---- backend bridge ------------------------------------------------------

  function callBackend(args, options, onDone) {
    // Single flight on the shared Process: a second call started while one
    // is in flight would overwrite runCtx, misroute the first call's
    // output to this callback and wedge its busy flag. Fail the newcomer
    // synthetically instead (same result shape as Backend.handleExit's
    // timeout report) so the caller's normal failure path runs.
    if (runCtx && runCtx.active === true) {
      // A second call is a WAIT, never a failure (ticket #42): the caller's
      // normal failure path would otherwise invent "Backend call failed
      // (list)" and wipe the status line of the run that is still healthy.
      onDone({ ok: false, exitCode: -1, stdout: "", stderr: "", json: null,
               timedOut: false, spawnFailed: false, error: "busy",
               wait: true })
      return
    }
    var ctx = Backend.run(backendProc, backendTimer, backendCommand, args, options || {}, onDone)
    runCtx = ctx && ctx.active ? ctx : null
  }

  // Map a finished Backend.run result onto the store's error/status state.
  // Covers: watchdog timeout, spawn failure (python3 missing), non-JSON
  // stdout, and the backend's own {"result":"error"} documents.
  //
  // `app` is the row this call concerned ({ id, name }) or null: an error
  // about one app must name it (ticket #48), and only that same app's next
  // outcome may replace it.
  function reportFailure(result, what, app) {
    var subject = app ? app.id : ""
    var detail = function(message) {
      return app ? Model.appErrorBanner(app.name, message) : message
    }
    // Another operation owns the shared Process (ticket #42): say so quietly
    // and leave whatever status the running operation set exactly as it is —
    // this is a wait, not an outcome.
    if (result.wait === true) {
      Model.setTransientStatus(strings.operationBusy)
      return
    }
    // A watchdog timeout means the backend ran but overran its budget —
    // a different problem from "never ran at all", and blaming python3
    // here sent users hunting for the wrong fix during slow updates.
    if (result.timedOut) {
      Model.setError(detail(strings.timeout), subject)
      return
    }
    if (result.spawnFailed || result.error === "no-backend-command") {
      // A probe-confirmed missing python3 is the install card's job — the
      // banner would only duplicate it.
      if (result.spawnFailed && root.pythonStateKnown && root.pythonMissing) {
        Model.clearError()
        return
      }
      Model.setError(strings.backendMissing)
      return
    }
    // A genuine error document (exit != 0) beats stderr for copy quality;
    // garbage stdout parses to null and falls through to the stderr line.
    var doc = Backend.parseJson(result.stdout)
    if (doc && doc.result === "error" && doc.error) {
      // The backend's own message already names the target where it can.
      Model.setError(String(doc.error), subject)
      return
    }
    if (result.stderr !== "" || result.stdout !== "") {
      Model.setError(detail(strings.backendFailed + ": "
        + Backend.elide(result.stderr || result.stdout)), subject)
      return
    }
    Model.setError(detail(strings.backendFailed + " (" + what + ")"), subject)
  }

  // ---- python3 dependency (authorized install) ------------------------------

  // One probe run through the same Backend.run plumbing as every backend
  // call: settles python presence, and while an authorized install runs,
  // reads the terminal's marker files (Model.pythonProbeArgv). Cheap enough
  // to re-run on every panel open and every poll tick.
  function probePython() {
    if (root.probeCtx && root.probeCtx.active) return
    var ctx = Backend.run(pythonProbeProc, pythonProbeTimer,
      Model.pythonProbeArgv(root.pythonFailureMarker, root.pythonCompleteMarker,
        root.installingPython),
      {}, {}, function(result) {
        root.probeCtx = null
        handleProbe(result.exitCode, result.stdout)
      })
    probeCtx = ctx && ctx.active ? ctx : null
  }

  // Route a finished probe. "waiting" keeps the poll running; "present"
  // tears the install state down and refreshes the now-working backend;
  // "canceled"/"failed" surface the terminal's verdict. While python is
  // missing, refresh() early-returns and the install card owns the screen,
  // so a racing spawn failure's banner is cleared here rather than stacked.
  function handleProbe(exitCode, output) {
    var outcome = Model.pythonProbeOutcome(exitCode, output, root.installingPython)
    // Verify-then-start: a pending Install press is answered by this probe
    // alone. Only a confirmed-missing verdict backed by a REAL exit may
    // start the install; any other outcome stands down — "present" falls
    // through to the present branch below (python is already there, the
    // refresh IS the answer), and canceled/failed can only arrive from a
    // stale install-mode poll the click rode on, none of which may open a
    // terminal. The exitCode >= 0 gate matters: a probe that never
    // answered arrives from Backend.handleTimeout as exitCode -1, which
    // the outcome mapper's fall-through default also labels "missing" —
    // trusting that would open the sudo terminal with no presence check
    // at all.
    if (root.installVerifyPending) {
      root.installVerifyPending = false
      if (outcome === "missing" && exitCode >= 0) {
        startPythonInstall()
        return
      }
      if (outcome !== "present") {
        syncFromModel()
        return
      }
    }
    if (outcome === "waiting") return
    root.pythonStateKnown = true
    if (outcome === "missing") {
      // Wipe a stale banner only on the false→true transition: the slow
      // post-timeout poll re-reports "missing" every 15 s, and an
      // unconditional clear would keep erasing the pythonInstallWaiting
      // message the timeout just posted.
      var wasMissing = root.pythonMissing
      root.pythonMissing = true
      if (!wasMissing) Model.clearError()
      syncFromModel()
      return
    }
    if (outcome === "present") {
      var recovered = root.pythonMissing || root.installingPython
      root.pythonMissing = false
      root.installingPython = false
      pythonPoll.stop()
      pythonInstallTimeout.stop()
      pythonSlowPoll.stop()
      if (recovered) {
        Model.setStatus(strings.pythonReady)
        syncFromModel()
        refresh()
      }
      return
    }
    root.installingPython = false
    pythonPoll.stop()
    pythonInstallTimeout.stop()
    // Defensive: a canceled/failed verdict also retires the slow poll so a
    // dead attempt cannot keep probing behind the error banner.
    pythonSlowPoll.stop()
    Model.setError(outcome === "canceled"
      ? strings.pythonInstallCanceled : strings.pythonInstallFailed)
    syncFromModel()
  }

  // The Install button. Nothing runs until this is pressed — and what then
  // runs is a visible floating terminal presenting the exact (constant)
  // command (sudo prompt included), never a hidden process. The user can
  // Ctrl-C it. The runtime dir gets its cheap syntactic gate here; the
  // prep step re-checks the filesystem before the terminal launches.
  //
  // Verify-then-start: the click does NOT spawn anything directly. It arms
  // installVerifyPending and probes; only a confirmed-missing answer
  // (handleProbe → startPythonInstall) opens the terminal. The guarantee
  // is bounded but real: a terminal only ever opens behind a fresh
  // absence verdict, and a completed install is never followed by a
  // redundant one. It does NOT dedupe against an earlier terminal still
  // sitting at its sudo prompt — a post-timeout re-press probes missing
  // again and can still open a second `pkg add` (the old
  // concurrent-pacman-lock race survives in that corner). Single-flight
  // nuance: if a probe is already in flight, probePython() early-returns
  // and the pending flag rides on THAT probe's result; only in the rare
  // case that probe already delivered does the click look inert —
  // pressing again re-arms.
  function installPython() {
    if (root.installingPython) return
    if (!Model.pythonRuntimeDirUsable(root.runtimeDir)) {
      Model.setError(strings.pythonNoRuntimeDir)
      syncFromModel()
      return
    }
    // A slow poll from a timed-out attempt may still be running; this new
    // attempt owns the flow from here.
    pythonSlowPoll.stop()
    root.installVerifyPending = true
    probePython()
  }

  // The verified install start, reached only through handleProbe once the
  // verify probe confirmed python3 is still missing. Enters install mode,
  // then arms the prep step: it re-validates the runtime dir against the
  // filesystem (absolute, directory, euid-owned, mode 700 — exit 5 =
  // refused) and clears stale markers so the poll can only see THIS
  // attempt's verdict; the terminal command clears them again defensively.
  function startPythonInstall() {
    root.installingPython = true
    Model.clearError()
    syncFromModel()
    pythonInstallPrepProc.command = Model.pythonInstallPrepArgv(root.runtimeDir)
    pythonInstallPrepProc.running = true
    // Prep watchdog: nothing else bounds the prep step (the install
    // timeout only arms after prep success), so a hung prep would leave
    // installingPython stuck true forever without this.
    pythonPrepTimer.restart()
  }

  function refresh() {
    if (Model.state.busy.list) return
    if (Model.state.mockMode) return
    // Probe-confirmed missing python3: every call would just spawn-fail;
    // the install card owns the screen until the user authorizes Python.
    // The card's success path clears pythonMissing before refreshing.
    if (root.pythonStateKnown && root.pythonMissing) return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    var refreshStartedMs = Date.now()
    Model.setBusy("list", true)
    syncFromModel()
    callBackend(["--list-installed", "--json"], null, function(result) {
      Model.setBusy("list", false)
      if (result.ok) {
        var parsed = Model.parseListJson(result.stdout)
        if (parsed.ok) {
          // A listing that settled an armed launch verdict just raised the
          // failure banner; clearing here would erase it before it paints.
          var settled = Model.setItems(
                parsed.items, parsed.runtime, refreshStartedMs)
          if (settled !== true) Model.clearError()
        } else {
          Model.setError(parsed.error)
        }
      } else {
        reportFailure(result, "list")
      }
      syncFromModel()
      // The row set may have changed (first-ever open, post-install):
      // adopt the job documents for whatever is installed now.
      refreshJobStates()
    })
  }

  // F4: launch the AppImage binary detached. The argv form keeps the path
  // (which came from the backend's JSON) a single literal argument — no
  // shell ever sees it. DESKTOPINTEGRATION=1 matches the generated desktop
  // entry's Exec line so AppImages that bundle their own integration
  // prompt stay quiet.
  //
  // The app's own Exec arguments (the backend's exec_arguments, e.g.
  // "--no-sandbox %U") ride along as extra argv tokens: real flags are
  // kept, desktop field codes (%f %F %u %U %i %c %k) are dropped because
  // the panel has no file or URL to hand over. No arguments at all (the
  // common case) produce exactly the argv of before.
  function launchItem(item) {
    if (!item || item.path === "") return
    var args = (item.execArguments || "").trim().split(/\s+/)
      .filter(function(a) { return a && !/^%[fFuUicdk]$/.test(a) })
    Quickshell.execDetached(["env", "DESKTOPINTEGRATION=1", item.path].concat(args))
    Model.markRunning(item.id, true)
    // Arm the launch verdict in the STORE (ticket #47): spawn success says
    // nothing about the AppImage runtime, and this click closes the panel,
    // so the verdict has to outlive this object. Any later listing settles
    // it — the timer below only asks for one soon.
    Model.armLaunchVerdict(item.id)
    launchSettleTimer.restart()
    // The panel is a full-screen overlay with exclusive keyboard focus, so
    // a freshly launched app would sit behind it and the user would have
    // to press Escape first. Close through the existing close path
    // (requestClose) so the shell's openPanelIds bookkeeping stays
    // consistent. A pinned panel is a compact floating window that is
    // meant to stay open.
    if (!root.pinned) root.requestClose()
  }

  // F3: first click arms the inline confirmation, second click runs
  // --remove keyed on the desktop_id (the stable identifier — names can
  // repeat when two versions coexist, CONTRACT.md).
  function requestRemove(item) {
    if (!item) return
    // A row whose detached update job is still downloading/replacing is
    // not removable: the job installs where the AppImage sits, so a
    // removal now would land the new release in a target that is already
    // in the trash. Fail closed here as well as on the buttons (same
    // shape as updateItem's per-app single-flight guard).
    if (root.jobActive(item.id)) return
    if (confirmRemoveId !== item.id) {
      confirmRemoveId = item.id
      confirmResetTimer.restart()
      return
    }
    confirmRemoveId = ""
    confirmResetTimer.stop()
    if (Model.state.busy.remove) return
    Model.setBusy("remove", true)
    syncFromModel()
    callBackend(["--remove", item.id, "--yes", "--json"], null, function(result) {
      Model.setBusy("remove", false)
      if (result.ok) {
        var action = Model.parseActionJson(result.stdout)
        if (action.result === "removed") {
          Model.removeById(item.id)
          Model.clearError()
        } else {
          reportFailure(result, "remove", item)
        }
      } else {
        reportFailure(result, "remove", item)
      }
      syncFromModel()
      refresh()
    })
  }

  // F2: run --integrate on the chosen path. Outcome handling per
  // CONTRACT.md: "integrated" (success), "already-integrated" (exit 0,
  // shown as success with different copy) and {"result":"error"}.
  function integratePath(path) {
    var trimmed = String(path || "").trim()
    if (trimmed === "") return
    if (Model.state.busy.integrate) return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    Model.clearError()
    Model.setBusy("integrate", true)
    Model.setStatus(strings.integrateWorking)
    syncFromModel()
    // A GitHub release URL install downloads the whole AppImage first;
    // give it the long transfer budget so the watchdog doesn't reap a
    // big download. Local paths keep the fast extract budget.
    var timeout = /^https?:\/\//i.test(trimmed)
      ? integrateUrlTimeoutMs : integrateTimeoutMs
    callBackend(["--integrate", trimmed, "--yes", "--json"],
        { timeoutMs: timeout }, function(result) {
      Model.setBusy("integrate", false)
      if (result.ok) {
        var action = Model.parseActionJson(result.stdout)
        if (action.result === "integrated") {
          Model.setStatus(Model.formatCount(action.app && action.app.name
            ? action.app.name : trimmed, strings.statusIntegrated))
        } else if (action.result === "already-integrated") {
          Model.setStatus(strings.statusAlreadyIntegrated)
        } else {
          Model.setError(action.error !== "" ? action.error : strings.backendFailed)
        }
      } else {
        reportFailure(result, "integrate")
      }
      syncFromModel()
      pickerOpen = false
      pickerPath = ""
      refresh()
    })
  }

  // F6: sweep every configured source. The offline error document arrives
  // as exit 1 + {"result":"error"} and reportFailure below surfaces its
  // own message ("Internet connection not available" and friends).
  function checkUpdates() {
    if (Model.state.mockMode) return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    // Single flight with the background service — a sweep that started in
    // Service.qml wins and this click is a no-op.
    if (!Model.claimUpdateCheck("panel")) return
    Model.clearError()
    Model.setStatus(strings.checkingUpdates)
    syncFromModel()
    callBackend(["--list-updates", "--json"], { timeoutMs: checkTimeoutMs }, function(result) {
      Model.releaseUpdateCheck("panel")
      if (result.ok) {
        var parsed = Model.parseUpdatesJson(result.stdout)
        if (parsed.ok) {
          Model.applyUpdates(parsed.updates)
          Model.setStatus(parsed.updates.length > 0
            ? Model.formatCount(parsed.updates.length, strings.updatesAvailable)
            : strings.updatesNone)
        } else {
          Model.setError(parsed.error)
        }
      } else {
        reportFailure(result, "list-updates")
      }
      syncFromModel()
    })
  }

  // F7: one-click update as a DETACHED BACKGROUND JOB. The click only
  // spawns: `--update --async` answers job-started (exit 0) or an error
  // document (exit 1 — e.g. the active-job refusal) within milliseconds.
  // A running app is the one exception: updateItem is reached through
  // requestUpdate, which shows the #27 confirm card instead of spawning
  // (a stale `running` flag is harmless in both directions — see there).
  // The spawn runs on its OWN Process/Timer pair (updateSpawnProc below)
  // with the default 15 s watchdog — never through callBackend, whose
  // single flight would synthetically fail the spawn whenever the shared
  // short-call pair is busy (a 10 s re-list poll, a 120 s --list-updates
  // check). The transfer itself runs outside this panel's process tree
  // and reaches the row through its job document. No confirm — the
  // backend replaces the AppImage where it sits; the running-app guard
  // and "up-to-date" now surface as terminal job documents, not
  // spawn-time results.
  // The row's Update action (ticket #27): a running app asks first. The
  // `running` flag comes from the last --list-installed poll, so it can be
  // stale — deliberately not probed at click time. Stale-but-running (the
  // flag says true, nothing runs any more) only grants a close the backend
  // finds nothing to do with; stale-but-not-running (the flag says false,
  // the app actually runs) is caught by the backend: without the grant a
  // non-TTY caller gets `skipped-running`, whose banner asks for another
  // Update press — by then the poll has caught up and the card appears.
  function requestUpdate(item) {
    if (!item || item.id === "") return
    if (root.jobActive(item.id)) return
    if (item.running === true) {
      // Only one consent card at a time: the row's force-quit card
      // replaces the batch card (and the other way round), so the
      // keyboard and the eye never face two asks at once.
      root.dismissUpdateAll()
      root.forceQuitId = item.id
      root.forceQuitName = item.name !== "" ? item.name : item.id
      root.forceQuitPids = item.runningPids > 0 ? item.runningPids : 0
      return
    }
    root.updateItem(item, false)
  }

  function dismissForceQuit() {
    root.forceQuitId = ""
    root.forceQuitName = ""
    root.forceQuitPids = 0
  }

  // Confirm: run exactly the async job, now with the --kill-running grant
  // (the backend closes the app at job start and again right before the
  // atomic replace; its force_quit record feeds the done banner).
  function confirmForceQuit() {
    var id = root.forceQuitId
    var name = root.forceQuitName
    if (id === "") return
    root.dismissForceQuit()
    root.updateItem({ id: id, name: name }, true)
  }

  // ---- "Update all" (issue #52) --------------------------------------------
  //
  // One action for every pending app. The click OPENS the consent card
  // and starts nothing: the card names the apps it would update and the
  // processes it would close, and only the explicit accept spawns the
  // jobs. An app with no pending release is never named and never
  // closed; an app whose verdict is false (the backend would refuse the
  // install) is never offered either — Model.pendingUpdateIds applies
  // the badge's own rule.
  function requestUpdateAll() {
    var ids = Model.pendingUpdateIds()
    if (ids.length === 0) return
    // Only one consent card at a time (see requestUpdate).
    root.dismissForceQuit()
    var apps = []
    for (var i = 0; i < ids.length; i++) {
      var item = Model.findItem(ids[i])
      if (!item) continue
      apps.push({ id: item.id, name: item.name,
                  running: item.running === true,
                  runningPids: item.runningPids > 0 ? item.runningPids : 0 })
    }
    if (apps.length === 0) return
    root.updateAllApps = apps
    root.updateAllOpen = true
  }

  // Dismiss: nothing is spawned, nothing is closed.
  function dismissUpdateAll() {
    root.updateAllOpen = false
    root.updateAllApps = []
    root.updateAllQueue = []
  }

  // Accept: one background job per app, in the same single-flight spawn
  // the row's Update button uses (--update --async). The consent card is
  // the --kill-running grant: the user accepted that running apps are
  // closed. Each app gets its OWN job, so the backend's per-app claim
  // still refuses a second update of the same app.
  function confirmUpdateAll() {
    if (!root.updateAllOpen) return
    var apps = root.updateAllApps
    root.updateAllOpen = false
    root.updateAllApps = []
    var queue = []
    for (var i = 0; i < apps.length; i++) queue.push(apps[i].id)
    root.updateAllQueue = queue
    root.pumpUpdateAll()
  }

  // The spawn pair is one-at-a-time, so the queue advances when a spawn
  // finishes (updateItem pumps the queue on every exit path). An app that
  // left the store in the meantime is skipped — the backend refuses an
  // update that does not exist anyway.
  function pumpUpdateAll() {
    if (root.updateAllQueue.length === 0) return
    if (root.updateSpawnCtx && root.updateSpawnCtx.active === true) return
    var id = String(root.updateAllQueue.shift())
    var item = Model.findItem(id)
    if (!item) {
      root.pumpUpdateAll()
      return
    }
    root.updateItem(item, true)
  }

  function updateItem(item, killRunning) {
    // Every exit path pumps the "Update all" queue: the batch advances
    // one app per finished spawn, and a refused spawn must not stall the
    // apps behind it.
    if (!item || item.id === "") {
      root.pumpUpdateAll()
      return
    }
    // Per-app single flight: a row with an active job refuses here (the
    // button is disabled too; the backend refuses a second job for the
    // same app regardless). Other rows are never blocked — different
    // apps update in parallel.
    if (root.jobActive(item.id)) {
      root.pumpUpdateAll()
      return
    }
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      root.pumpUpdateAll()
      return
    }
    // Single flight on the spawn pair itself: a second --update --async
    // while one spawn is still answering would overwrite updateSpawnCtx
    // and misroute the first reply (same guard as callBackend's).
    if (root.updateSpawnCtx && root.updateSpawnCtx.active === true) {
      root.pumpUpdateAll()
      return
    }
    Model.clearError()
    Model.setStatus(Model.formatCount(item.name, strings.updating))
    syncFromModel()
    var args = ["--update", item.id, "--yes", "--json", "--async"]
    if (killRunning === true) args.push("--kill-running")
    // Default watchdog on purpose: this call is over in milliseconds.
    var ctx = Backend.run(updateSpawnProc, updateSpawnTimer, backendCommand,
        args, {},
        function(result) {
      root.updateSpawnCtx = null
      if (result.ok) {
        var doc = Backend.parseJson(result.stdout)
        if (doc && doc.result === "job-started") {
          // The row may spin before the job's first document write
          // lands: adopt a provisional active decision and read the
          // real document right away. Every later state — including
          // "the job died before writing anything" — arrives as a
          // document decision and replaces this, so the spinner can
          // never outlive its document.
          root.jobStartGraces[item.id] = Date.now()
          Model.adoptJobDecision(item.id,
            JobDocs.provisionalActiveDecision(item.id))
        } else {
          // job-started is the only success shape --async reports.
          Model.setError(Model.appErrorBanner(item.name,
            strings.backendFailed + " (update)"), item.id)
        }
      } else {
        // Refusals arrive as error documents — the backend's own message
        // (the active-job refusal names the pid) surfaces verbatim.
        reportFailure(result, "update", item)
      }
      syncFromModel()
      // Converge the row with whatever its job document says now — the
      // refusal case too, where the already-running job's document is
      // exactly what the row should show.
      root.queueJobRead(item.id)
      root.pumpJobReads()
      // The spawn pair is free again: the next pending app of an
      // "Update all" batch may start.
      root.pumpUpdateAll()
    })
    // A synchronous onDone (no-backend-command) has already finished by
    // the time this line runs; ctx.active is false then, the ctx stays
    // null — same convention as probePython.
    updateSpawnCtx = ctx && ctx.active ? ctx : null
  }

  // ---- background update jobs: document reads + adoption (ticket #11) -------

  // True while THIS row renders an active job (spinner + progress). The
  // only source is the adopted document view — never a click flag.
  function jobActive(id) {
    var d = root.jobStates[id]
    return !!d && d.state === "active"
  }

  // Queue every installed app's job document for adoption. Called on every
  // panel open, whenever the installed list lands, and by the job poll;
  // the queue drains one document at a time through jobFileView.
  function refreshJobStates() {
    for (var i = 0; i < root.items.length; i++) {
      root.queueJobRead(root.items[i].id)
    }
    root.pumpJobReads()
  }

  function queueJobRead(id) {
    if (id === "" || root.jobReadingId === id) return
    if (root.jobReadQueue.indexOf(id) !== -1) return
    root.jobReadQueue.push(id)
  }

  function pumpJobReads() {
    if (root.jobReadingId !== "" || root.jobReadQueue.length === 0) return
    var id = String(root.jobReadQueue.shift())
    var path = JobDocs.jobDocumentPath(root.jobsDir, id)
    if (path === "") {
      // Unusable desktop id: jobs.py's stem rule means no document can
      // exist for it — adopt the idle decision and move on.
      root.adoptJob(id, root.idleParsed())
      root.pumpJobReads()
      return
    }
    root.jobReadingId = id
    jobReadWatchdog.restart()
    // Clear-then-set (the Service.qml jobView idiom): an equal-value
    // property write fires no change signal, so re-pointing the view at
    // the SAME document — a single-app install re-queues its one id every
    // poll tick — would never reload and hang the read until the watchdog
    // retired it without adopting anything.
    jobFileView.path = ""       // force a re-read even when the previous
    jobFileView.path = path     // read pointed at this same document
  }

  // The "no document" parse result (file missing, or an id that cannot
  // name one): JobDocs.adoptJob(null) is the idle decision — idle is ONLY
  // ever adopted from an absent document, never from a corrupt one.
  function idleParsed() {
    return { parseError: "", heartbeat: 0,
             decision: JobDocs.adoptJob(null, true, Date.now()) }
  }

  // Store one adopted decision and surface any non-active outcome. The
  // desktopId check keeps a document that names another app from ever
  // dressing this row (paranoia: the read path was built from the row's
  // own id, but the check is JobDocs's stated adoption contract).
  function adoptJob(id, parsed) {
    if (parsed.decision.desktopId !== "" && parsed.decision.desktopId !== id) {
      parsed = root.idleParsed()
    }
    // Click grace: do not let an early "no document" read overwrite a
    // provisional active decision — see jobStartGraces above.
    if (parsed.decision.state === "idle" && root.jobActive(id)
        && Date.now() - (root.jobStartGraces[id] || 0) < root.jobStartGraceMs) {
      return
    }
    root.jobStartGraces[id] = 0
    Model.adoptJobDecision(id, parsed.decision)
    syncFromModel()
    var entry = Model.jobEntry(id)
    root.surfaceJobOutcome(id, parsed, entry ? entry.consumed : 0)
  }

  // Terminal and interrupted outcomes surface ONCE per sign-of-life (the
  // document's heartbeat; a corrupt document has none, so -1 marks its
  // once-per-session banner). Active needs no surfacing — the row shows
  // it; idle has nothing to say.
  function surfaceJobOutcome(id, parsed, consumed) {
    var d = parsed.decision
    if (d.state === "active" || d.state === "idle") return
    var seen = parsed.heartbeat > 0
      ? consumed === parsed.heartbeat : consumed === -1
    if (seen) return
    Model.markJobConsumed(id, parsed.heartbeat > 0 ? parsed.heartbeat : -1)
    if (d.state === "done") {
      // This app reached an outcome, so an older error about it is spent.
      Model.noteOutcome(id)
      // "updated" changed the AppImage (and its desktop entry's version):
      // re-list so names, versions and manager columns stay truthful.
      if (d.status === "updated") {
        Model.setStatus(Model.jobDoneBanner(d, ""))
        refresh()
      } else if (d.status === "up-to-date") {
        Model.setStatus(Model.jobDoneBanner(d, strings.upToDate))
      } else if (d.status === "skipped-running") {
        Model.setStatus(d.message !== "" ? d.message : strings.skippedRunning)
      } else if (d.message !== "") {
        Model.setStatus(d.message)
      }
    } else if (d.state === "error") {
      // G: the error line also carries the closed-app clause when the job
      // closed the app and THEN failed (force_quit on an error document)
      Model.setError(Model.jobErrorBanner(d, strings.backendFailed), id)
    } else { // interrupted: dead updater or a heartbeat stale past 120 s
      Model.setError(strings.jobInterrupted, id)
    }
    syncFromModel()
  }

  // Per-row progress label: percent from the document's progress fraction
  // (the trustworthy live signal), byte pair only when the declared total
  // is plausibly the payload (JobDocs.bytesSane — a zsync source declares
  // its control file's size, and a kilobyte "total" under a truthful
  // percent reads as a lie).
  function jobProgressLabel(id) {
    var d = root.jobStates[id]
    if (!d || d.state !== "active") return ""
    var label = Math.round((d.progress || 0) * 100) + "%"
    if (JobDocs.bytesSane(d.totalBytes)) {
      label += " · " + JobDocs.formatBytes(d.downloadedBytes)
        + " / " + JobDocs.formatBytes(d.totalBytes)
    }
    return label
  }

  // ---- settings card (F8 cadence) -------------------------------------------

  function toggleSettings() {
    if (settingsOpen) {
      settingsOpen = false
      closeSourceEditor()
      return
    }
    // A browse in flight keeps the picker card (closePicker guards it);
    // opening settings under it would recreate the width/focus fight the
    // cards' mutual exclusion exists to prevent.
    if (Model.state.busy.pick) return
    closePicker()
    settingsOpen = true
    mirrorEditFields()
    loadSettingsCard()
  }

  // Pour the store's settings into the card's working copies.
  function mirrorEditFields() {
    editEnabled = Model.state.settings.enabled
    editInterval = Model.state.settings.intervalMinutes
    editDelay = Model.state.settings.delayMinutes
  }

  // Re-read --settings from the backend so the card shows truth (opening
  // the card, and the tail of saveSettings, both land here).
  function loadSettingsCard() {
    if (backendCommand.length === 0 || settingsBusy) return
    settingsBusy = true
    syncFromModel()
    callBackend(["--settings", "--json"], null, function(result) {
      settingsBusy = false
      if (result.ok) {
        var parsed = Model.parseSettingsJson(result.stdout)
        if (parsed.ok) {
          Model.applySettings(parsed.settings)
        } else {
          Model.setError(parsed.error)
        }
      } else {
        reportFailure(result, "settings")
      }
      mirrorEditFields()
      syncFromModel()
    })
  }

  // Save the cadence: one --set-setting per changed key, then re-read the
  // document so the card (and the service, via the settingsRevision bump)
  // converges on what the backend actually stored.
  function saveSettings() {
    if (settingsBusy) return
    var queue = []
    if (editEnabled !== Model.state.settings.enabled)
      queue.push("update_check_enabled=" + (editEnabled ? "true" : "false"))
    if (Math.max(15, editInterval) !== Model.state.settings.intervalMinutes)
      queue.push("update_check_interval_minutes=" + Math.max(15, editInterval))
    if (Math.max(0, editDelay) !== Model.state.settings.delayMinutes)
      queue.push("update_check_delay_minutes=" + Math.max(0, editDelay))
    if (queue.length === 0) {
      Model.setStatus(strings.settingsSaved)
      syncFromModel()
      return
    }
    settingsBusy = true
    pendingSettingKeys = queue
    runNextSettingSave()
  }

  function runNextSettingSave() {
    if (!settingsBusy) return
    if (pendingSettingKeys.length === 0) {
      pendingSettingKeys = []
      settingsBusy = false
      loadSettingsCard()
      Model.setStatus(strings.settingsSaved)
      syncFromModel()
      return
    }
    var kv = pendingSettingKeys.shift()
    callBackend(["--set-setting", kv, "--json"], null, function(result) {
      if (!result.ok) {
        // Abort the chain on failure; the reload keeps the card honest
        // about what actually stuck.
        pendingSettingKeys = []
        settingsBusy = false
        reportFailure(result, "set-setting")
        loadSettingsCard()
        return
      }
      runNextSettingSave()
    })
  }

  // F9 editor: seed the manager dropdown (empty = no source configured —
  // the user must pick before Save) and blank field values, then pre-fill
  // the stored source asynchronously via --get-update-source (--list-installed
  // carries the manager name only, never its config). Clicking the same
  // row's gear again toggles the editor closed — the unsaved working values
  // die with it (closeSourceEditor drops them); a different row's gear
  // keeps the switch-over behaviour below.
  function openSourceEditor(item) {
    if (!item) return
    if (root.sourceEditId === item.id) {
      closeSourceEditor()
      return
    }
    // Name/manager first: the per-row editor's Dropdown `value` binding
    // derives from root.sourceEditManager, and its visible binding from
    // sourceEditId — so managers and values must be live before the row's
    // editor becomes visible, and sourceEditId lands last.
    sourceEditName = item.name
    sourceEditManager = item.manager
    rebuildSourceEditValues()
    sourceEditId = item.id
    // Switched rows empty the previous row's Repeater model, which
    // Loader-destroys its fields (see closeSourceEditor for why the count
    // must be zeroed by hand).
    textFieldFocusCount = 0
    // Coarse reveal: the editor may sit below the fold of a long list.
    Qt.callLater(function() {
      if (root.sourceEditId !== "" && listFlick) {
        listFlick.contentY = Math.max(0, listFlick.contentHeight - listFlick.height)
      }
    })
    // Async pre-fill: the editor rendered blank instantly; fill it from the
    // stored config once the backend answers. Bail out when the user closed
    // the editor or switched rows while the call was in flight (a stale
    // answer must never touch the current editor's state) or when the call
    // failed; "no-source" and unparseable answers keep the blanks.
    callBackend(["--get-update-source", item.id, "--json"], null,
        function(result) {
      if (root.sourceEditId !== item.id || !result.ok) return
      var doc = Backend.parseJson(result.stdout)
      if (!doc || doc.result !== "ok") return
      // Same ordering as the instant path above: manager live before the
      // values rebuild so the dropdown never binds a half-set state.
      if (doc.manager) root.sourceEditManager = doc.manager
      root.rebuildSourceEditValues()
      var config = doc.config || {}
      for (var key in config) {
        // Plain key writes only — the map object is never swapped under a
        // live field binding (see setSourceValue).
        root.sourceEditValues[key] = String(config[key])
      }
      // Key writes carry no change signal by design, so the fields would
      // keep showing the blanks the rebuild just published; republish the
      // filled map as one replacement to make every binding re-read.
      var filled = {}
      for (var k in root.sourceEditValues) filled[k] = root.sourceEditValues[k]
      root.sourceEditValues = filled
    })
  }

  function closeSourceEditor() {
    sourceEditId = ""
    sourceEditName = ""
    sourceEditManager = ""
    sourceEditValues = {}
    // Loader-destroyed fields may never deliver their final
    // activeFocusChanged(false); zero the count so the key catcher cannot
    // stay blocked forever.
    textFieldFocusCount = 0
  }

  function rebuildSourceEditValues() {
    var mgr = Model.managersByName()[sourceEditManager]
    var values = {}
    if (mgr) {
      for (var i = 0; i < mgr.fields.length; i++) {
        values[mgr.fields[i].key] = mgr.fields[i].type === "bool" ? "false" : ""
      }
    }
    // Reset before the model swap re-creates the Loader-held fields, for
    // the same skipped-leave reason as closeSourceEditor above.
    textFieldFocusCount = 0
    sourceEditValues = values
  }

  function setSourceValue(key, value) {
    // Storage only: mutate without reassigning so a field's text binding
    // never re-evaluates mid-typing. rebuildSourceEditValues and the async
    // pre-fill in openSourceEditor are the only writers that replace the
    // object (both outside a typing session).
    sourceEditValues[key] = value
  }

  // Persist the edited source. argv keeps every token a single literal —
  // ids, keys and values never meet a shell. Empty string fields are
  // skipped; the backend validates what its manager needs and answers
  // exit 2 with a clean {"result":"error"} document (--json) otherwise.
  function saveSource() {
    if (sourceBusy || sourceEditId === "" || sourceEditManager === "") return
    var args = ["--set-update-source", sourceEditId, "--manager",
                sourceEditManager, "--json"]
    var mgr = Model.managersByName()[sourceEditManager]
    if (mgr) {
      for (var i = 0; i < mgr.fields.length; i++) {
        var field = mgr.fields[i]
        var value = sourceEditValues[field.key]
        if (value === undefined) continue
        var text = field.type === "bool"
          ? (value === "true" ? "true" : "false")
          : String(value).trim()
        if (text === "") continue
        args.push(field.key + "=" + text)
      }
    }
    sourceBusy = true
    Model.clearError()
    syncFromModel()
    callBackend(args, null, function(result) {
      sourceBusy = false
      if (result.ok) {
        Model.setStatus(strings.sourceSaved)
        Model.clearError()
        closeSourceEditor()
      } else {
        reportFailure(result, "set-update-source")
      }
      syncFromModel()
      // Manager names ride on --list-installed; refresh the rows.
      refresh()
    })
  }

  function unsetSource() {
    if (sourceBusy || sourceEditId === "") return
    sourceBusy = true
    Model.clearError()
    syncFromModel()
    callBackend(["--set-update-source", sourceEditId, "--unset", "--json"], null,
        function(result) {
      sourceBusy = false
      if (result.ok) {
        Model.setStatus(strings.sourceUnsetDone)
        Model.clearError()
        closeSourceEditor()
      } else {
        reportFailure(result, "set-update-source")
      }
      syncFromModel()
      refresh()
    })
  }

  // ---- integrate picker (F2 fallback: no Quickshell file dialog) -----------

  function openPicker() {
    // The two inline cards would fight for width and keyboard focus; the
    // settings gear and Integrate close each other.
    settingsOpen = false
    closeSourceEditor()
    // A preview computed for a previous session is stale: re-entering the
    // picker starts at the plain path field.
    clearPreview()
    pickerOpen = true
    rebuildPickerFiles()
  }

  function closePicker() {
    // A browse in flight owns the card: its answer lands in pickerPath, so
    // the card must not be dismissed out from under the pending dialog.
    // Idempotent-open paths (openPicker, settings toggling back) are
    // unaffected; the dialog itself is bounded by the pick watchdog.
    if (Model.state.busy.pick) return
    pickerOpen = false
    clearPreview()
  }

  // Preview lifecycle (#18). clearPreview resets all card state; it never
  // calls the backend or touches the store — dismissing has zero side
  // effects, by contract.
  function clearPreview() {
    previewOpen = false
    busyPreview = false
    previewDoc = null
    previewInput = ""
  }

  // Step 1 of the flow: ask the backend what --integrate WOULD do. The
  // preview verb is offline (URL classification by shape, local entries
  // read in place) and advisory — nothing here integrates anything. Failure
  // lands in the error banner and the picker stays usable.
  function requestPreview(path) {
    var trimmed = String(path || "").trim()
    if (trimmed === "") return
    if (Model.state.busy.integrate || root.busyPreview) return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    // Same input, card already showing it: nothing to refresh (this is
    // also what a second Enter on the focused field lands on).
    if (root.previewOpen && root.previewInput === trimmed) return
    Model.clearError()
    root.busyPreview = true
    syncFromModel()
    callBackend(["--preview-integrate", trimmed, "--json"],
        { timeoutMs: previewTimeoutMs }, function(result) {
      root.busyPreview = false
      // The path moved on while the call ran (kept typing, quick-pick,
      // picker closed): the answer is stale — drop it silently. Without
      // this, the card could preview one path while Confirm installs
      // another. The field's next Enter re-previews the new text.
      if (String(root.pickerPath).trim() !== trimmed) {
        syncFromModel()
        return
      }
      if (result.ok) {
        var parsed = Model.parsePreviewJson(result.stdout)
        if (parsed.ok) {
          root.previewDoc = parsed.preview
          root.previewInput = trimmed
          root.previewOpen = true
          Model.clearError()
        } else {
          clearPreview()
          Model.setError(parsed.error)
        }
      } else {
        clearPreview()
        reportFailure(result, "preview")
      }
      syncFromModel()
      // The confirm step owns the keyboard (Enter confirms, Escape
      // dismisses): pull focus back to the panel-level key catcher so the
      // SafeTextField stops blocking it.
      Qt.callLater(function() {
        if (root.opened && keyCatcher) keyCatcher.forceActiveFocus()
      })
    })
  }

  // Step 2: the explicit confirmation. Runs the existing integrate command
  // unchanged (integratePath re-classifies and re-verifies from scratch —
  // the preview document was advisory). The card is dropped first so a
  // failure leaves the plain picker with the banner, not a stale preview.
  function confirmPreview() {
    if (!root.previewOpen || root.busyPreview) return
    // A browse in flight owns the backend the integrate command would
    // share (#19's pick-busy invariant, applied to the keyboard's confirm
    // path too — the Go button is already disabled during a pick).
    if (Model.state.busy.integrate || Model.state.busy.pick) return
    clearPreview()
    integratePath(root.pickerPath)
  }

  // Esc / Cancel: back to the picker, nothing was run, nothing changed.
  function dismissPreview() {
    if (!root.previewOpen) return
    clearPreview()
    // Hand focus back to the path field so the user can edit what they
    // typed (formEditing re-blocks the key catcher while it holds focus).
    Qt.callLater(function() {
      if (root.pickerOpen && pickerFieldLoader.item) {
        pickerFieldLoader.item.forceActiveFocus()
      }
    })
  }

  // Preview card display lines. An empty appName means the backend could
  // not know yet (URL sources download nothing; a local file without an
  // embedded desktop entry stays unreadable in place) — the copy says so
  // instead of showing blank, per CONTRACT.md's "the preview stays valid".
  function previewAppLine() {
    if (!previewDoc) return ""
    if (previewDoc.appName !== "") return previewDoc.appName
    return previewDoc.sourceKind === "local"
      ? strings.previewAppUnknown : strings.previewAppUnknownUrl
  }

  function previewDestinationLine() {
    if (!previewDoc) return ""
    if (previewDoc.destinationPath !== "") return previewDoc.destinationPath
    return previewDoc.destinationFolder !== ""
      ? previewDoc.destinationFolder + " — " + strings.previewDestinationLater
      : strings.previewDestinationLater
  }

  // FolderListModel.filePath comes back as a plain path on some Qt builds
  // and as a file:// URL on others; normalize to a local path.
  function pickerUrlToPath(value) {
    var s = String(value || "")
    if (s.indexOf("file://") === 0) {
      s = s.substring("file://".length)
      s = s.split("/").map(decodeURIComponent).join("/")
    }
    return s
  }

  function rebuildPickerFiles() {
    var picks = []
    var models = [downloadsUpper, downloadsLower]
    for (var m = 0; m < models.length; m++) {
      var count = models[m].count
      for (var i = 0; i < count; i++) {
        try {
          var path = pickerUrlToPath(models[m].get(i, "filePath"))
          var name = String(models[m].get(i, "fileName"))
          if (path !== "") picks.push({ name: name, path: path })
        } catch (e) { /* unreadable entry — skip it */ }
      }
    }
    pickerFiles = picks
  }

  // F2 browse: run --pick-file through the shared watchdog plumbing. The
  // backend orchestrates a crash-isolated native dialog (portal first,
  // zenity fallback) under its own 600 s whole-operation deadline; result
  // mapping per CONTRACT.md — picked (exit 0 + path) fills the field,
  // cancelled/unavailable are exit-0 statuses, typed errors (reason key)
  // and a watchdog overrun surface through reportFailure like every other
  // backend failure. A busy browse is a no-op (and the Go/Downloads paths
  // are disabled meanwhile), so the dialog can never stack.
  function browseForFile() {
    if (Model.state.busy.pick) return
    if (Model.state.busy.integrate) return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    Model.clearError()
    Model.setBusy("pick", true)
    Model.setStatus(strings.pickerBrowsing)
    syncFromModel()
    callBackend(["--pick-file", "--json"], { timeoutMs: pickTimeoutMs },
        function(result) {
      Model.setBusy("pick", false)
      if (result.ok) {
        var pick = Model.parsePickJson(result.stdout)
        if (pick.result === "picked" && pick.path !== "") {
          Model.clearStatus()
          pickerPath = pick.path
          // A preview computed for the previous path is stale the moment
          // the fill lands (#18 confirm-before-integrate, browse flavor):
          // drop it so Confirm can never install anything other than what
          // is on screen. The picked path then rides the same preview flow
          // as a drop or a Downloads quick-pick — seedDropPreview's
          // mechanics minus openPicker (the card is already open): fill
          // the field, request the offline preview, Confirm stays the only
          // install path (ticket #19 acceptance). Same-trimmed fills keep
          // the shown card via requestPreview's own guard, matching the
          // field's onEdited rule.
          if (root.previewOpen && pick.path.trim() !== root.previewInput) {
            root.dismissPreview()
          }
          syncFromModel()
          focusPickerField()
          root.requestPreview(pick.path)
          return
        }
        if (pick.result === "cancelled") {
          Model.setStatus(strings.pickCanceled)
        } else if (pick.result === "unavailable") {
          // The backend's own copy wins; the local string is the fallback.
          // Status, not error: the path field keeps working exactly as
          // before (this is today's UX, not a failure).
          Model.setStatus(pick.message !== "" ? pick.message
            : strings.pickerUnavailable)
        } else {
          Model.setError(pick.result === "error" && pick.error !== ""
            ? pick.error : strings.pickerPickFailed)
        }
      } else {
        reportFailure(result, "pick-file")
      }
      syncFromModel()
    })
  }

  // Put focus into the picker's path field (Loader-created — it exists
  // only while the card is open) so a picked path can be integrated with
  // Enter right away. Defensive: a browse that outlived the card is a
  // quiet store update.
  function focusPickerField() {
    var field = pickerFieldLoader.item
    if (field) field.forceActiveFocus()
  }

  // ---- drop-to-integrate (#21) ------------------------------------------------
  //
  // Two entries, one flow. The panel window's DropArea (live drop) and the
  // bar widget's summon-payload handoff both end in seedDropPreview, which
  // is exactly the Downloads quick-pick mechanics plus openPicker(): seed
  // the field, request the offline preview — a drop NEVER integrates
  // directly, Confirm stays the only install path.

  // True while the shared backend process or a picker-owning flow is busy —
  // the same states during which Go/Browse/Downloads are disabled (#18/#19
  // guards). A drop racing any of them is IGNORED with a status line,
  // never queued: a field fill mid-call could only produce an answer the
  // stale-drop logic would have to throw away (the same reasoning that
  // disables Browse during a preview), and every flag here is
  // watchdog-bounded, so "drop again" is always short-lived advice.
  function dropBusy() {
    return Model.isBusy() || root.busyPreview || root.settingsBusy || root.sourceBusy
  }

  // The shared handoff. openPicker() closes the settings/source cards and
  // drops a stale preview first, so a drop works from any panel state and
  // the card that appears is always computed from the dropped file. An
  // ignored-extras report is a status (accent banner, 5 s auto-clear) set
  // before the preview request so both notices coexist.
  function seedDropPreview(path, ignored) {
    openPicker()
    root.pickerPath = path
    if (ignored > 0) {
      Model.setStatus(ignored === 1 ? strings.dropIgnoredOne
        : Model.formatCount(ignored, strings.dropIgnoredMany))
    }
    root.requestPreview(path)
  }

  // Panel DropArea entry: convert the dropped URI list (typed result —
  // see Model.dropUriToPath), report rejections as a status with the
  // picker untouched, refuse busy races, otherwise run the flow.
  function handleDropUris(uris) {
    var conv = Model.dropUriToPath(uris)
    if (!conv.ok) {
      // reason "empty" means nothing usable was dropped at all — silent.
      if (conv.reason === "not-file") Model.setStatus(strings.dropNotFile)
      else if (conv.reason === "no-appimage") Model.setStatus(strings.dropNoAppImage)
      syncFromModel()
      return
    }
    if (dropBusy()) {
      Model.setStatus(strings.dropBusy)
      syncFromModel()
      return
    }
    seedDropPreview(conv.path, conv.ignored)
  }

  // Summon-payload parser (bar-widget handoff). Only BarWidget's own drop
  // handler writes this shape ({drop_path, drop_ignored}); every other
  // summon payload ("{}", garbage) leaves the pending pair empty. The path
  // is picker-field DATA exactly like a typed path — it reaches nothing
  // but the preview flow, never a shell.
  function readDropPayload(payloadJson) {
    root.pendingDropPath = ""
    root.pendingDropIgnored = 0
    if (!payloadJson || payloadJson === "{}") return
    var payload = null
    try { payload = JSON.parse(String(payloadJson)) } catch (e) { payload = null }
    if (!payload || typeof payload !== "object") return
    var dropped = String(payload.drop_path || "").trim()
    if (dropped === "") return
    root.pendingDropPath = dropped
    var extras = payload.drop_ignored
    root.pendingDropIgnored = typeof extras === "number" && extras > 0
      ? Math.round(extras) : 0
  }

  // Consume the parked bar-widget drop. Runs on every store sync (and once
  // more from open() via callLater as the idle-store backstop): while any
  // backend call is in flight the drop stays pending — a preview request
  // started now would only die on the shared process's single-flight
  // guard — and the sync after the last busy flag clears runs it. The
  // wait is bounded (every busy flag is watchdog-released) and closing
  // the panel cancels the drop outright (onOpenedChanged). Deferral here
  // is transport, not queueing: the gesture happened before the panel
  // existed, so ignoring it would kill the only reason the panel opened.
  function tryConsumePendingDrop() {
    if (root.pendingDropPath === "" || !root.opened) return
    if (dropBusy()) return
    var dropped = root.pendingDropPath
    var ignored = root.pendingDropIgnored
    root.pendingDropPath = ""
    root.pendingDropIgnored = 0
    seedDropPreview(dropped, ignored)
  }

  Component.onCompleted: {
    // Release flags a DESTROYED panel instance left behind. Only a fresh
    // instance can run this: closing the panel unloads this whole QML
    // object, which SIGTERMs its backend child (the shell's kill is
    // `running: false`) and so never runs the onDone callback that would
    // have cleared the flag. `lib/Model.js` is engine-wide, so without this
    // the "Integrating…" of a run that died with the last instance is
    // permanent and the busy guard refuses every retry. It must NOT live in
    // open(): the shell re-delivers open() to the live panel (summon, bar
    // drop), and a live panel releasing its own claim lets the service
    // start a second sweep beside it.
    Model.releaseOrphanedRuns()
    syncFromModel()
    unsubscribe = Model.subscribe(syncFromModel)
    probePython()
  }

  Component.onDestruction: {
    // This instance is being destroyed and takes its backend child with it
    // (panel close unloads the object → Process destroyed → python gets
    // SIGTERM). A killed integrate can never write its own outcome, so say
    // so: the next open explains the vanished download instead of showing
    // a stuck spinner. The flags themselves are released by open()'s
    // releaseOrphanedRuns().
    if (Model.state.busy.integrate) {
      Model.setError(strings.integrateInterrupted)
    }
    if (unsubscribe) unsubscribe()
  }

  // Closing the panel (every path — Escape, outside click, host hide, IPC
  // close — ends in `opened` going false) stops the post-timeout recovery
  // poll, and the install timeout itself only starts that poll while the
  // panel is open — so no 15 s probe survives a closed panel for the
  // shell's lifetime. open() re-probes on every open, and a fresh Install
  // press re-arms recovery.
  onOpenedChanged: {
    if (!root.opened) {
      pythonSlowPoll.stop()
      // A bar-widget drop not yet consumed dies with the panel: a later
      // open must never surface a stale gesture as a fresh preview card.
      root.pendingDropPath = ""
      root.pendingDropIgnored = 0
      // Same rule for the #27 confirm card: a force-quit decision belongs
      // to the visit that asked for it, never to a later open.
      root.dismissForceQuit()
    }
  }

  Process {
    id: backendProc
    command: []
    stdout: StdioCollector {
      id: backendStdout
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.runCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: backendStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.runCtx, "stderr", data) }
    }
    // Proof of spawn: separates "python3 missing" (never started) from
    // "ran and failed" in the failure reporting above.
    onStarted: Backend.markStarted(root.runCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.runCtx, exitCode, backendStdout.text, backendStderr.text)
    }
  }

  Timer {
    id: backendTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.runCtx)
  }

  // Dedicated spawn pair for --update --async (F7): the click must never
  // queue behind the shared backendProc's single flight — a 10 s re-list
  // poll or a 120 s --list-updates check in flight would synthetically
  // fail the spawn (a spurious error banner and a lost click). Same
  // declarative wiring as backendProc; the default 15 s watchdog bounds
  // only the spawn itself, never the detached transfer.
  Process {
    id: updateSpawnProc
    command: []
    stdout: StdioCollector {
      id: updateSpawnStdout
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.updateSpawnCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: updateSpawnStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.updateSpawnCtx, "stderr", data) }
    }
    // Proof of spawn: separates "python3 missing" (never started) from
    // "ran and failed" in the failure reporting above.
    onStarted: Backend.markStarted(root.updateSpawnCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.updateSpawnCtx, exitCode, updateSpawnStdout.text, updateSpawnStderr.text)
    }
  }

  Timer {
    id: updateSpawnTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.updateSpawnCtx)
  }

  // ---- python3 dependency plumbing -------------------------------------------
  // A dedicated probe Process (it must answer even while a backend call is
  // failing or in flight), the install pair — a short prep that re-validates
  // the runtime dir and clears stale markers, then the detached floating
  // terminal — and the poll/watchdog timers. The timers are driven
  // imperatively (restart on install start, stop on every terminal outcome)
  // so no leftover binding can fire a stray probe.
  Process {
    id: pythonProbeProc
    command: []
    stdout: StdioCollector {
      id: pythonProbeOutput
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.probeCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: pythonProbeStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.probeCtx, "stderr", data) }
    }
    onStarted: Backend.markStarted(root.probeCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.probeCtx, exitCode, pythonProbeOutput.text, pythonProbeStderr.text)
    }
  }

  Timer {
    id: pythonProbeTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.probeCtx)
  }

  Process {
    id: pythonInstallPrepProc
    onExited: function(exitCode) {
      // Any real exit retires the one-shot watchdog first: the timer must
      // never fire after the prep answered.
      pythonPrepTimer.stop()
      if (!root.installingPython) return
      // 5 = the prep script refused the runtime dir (not an absolute
      // directory owned by the euid with mode 700): the cheap JS gate
      // passed but the filesystem disagreed, so surface the same message
      // and stand down without ever opening the terminal.
      if (exitCode === 5) {
        root.installingPython = false
        Model.setError(strings.pythonNoRuntimeDir)
        syncFromModel()
        return
      }
      if (exitCode !== 0) {
        // The terminal never opened here, so the terminal-focused install
        // copy would be a lie — this message says nothing was changed.
        root.installingPython = false
        Model.setError(strings.pythonInstallPrepFailed)
        syncFromModel()
        return
      }
      pythonInstallerProc.command = Model.pythonInstallArgv()
      pythonInstallerProc.startDetached()
      pythonPoll.restart()
      pythonInstallTimeout.restart()
    }
  }

  // Prep watchdog: bounds the prep Process (10 s for local stat/rm work is
  // generous — the install watchdog only arms after prep success, so this
  // is the only bound a hung prep can ever have). Kill it, stand down and
  // show the honest prep-failure copy; the eventual onExited is a no-op
  // because installingPython is already false here.
  Timer {
    id: pythonPrepTimer
    interval: 10000
    repeat: false
    onTriggered: {
      if (!pythonInstallPrepProc.running) return
      pythonInstallPrepProc.running = false
      root.installingPython = false
      Model.setError(strings.pythonInstallPrepFailed)
      root.syncFromModel()
    }
  }

  // Detached on purpose: the terminal outlives probe polls and reports
  // through the marker files, not through this process object.
  Process { id: pythonInstallerProc }

  Timer {
    id: pythonPoll
    interval: 1000
    repeat: true
    onTriggered: root.probePython()
  }

  Timer {
    id: pythonInstallTimeout
    interval: 300000
    onTriggered: {
      if (!root.installingPython) return
      root.installingPython = false
      pythonPoll.stop()
      Model.setError(strings.pythonInstallWaiting)
      syncFromModel()
      // The terminal can outlive the panel's patience: hand recovery to a
      // slow probe — but only while the panel is open. A timeout firing on
      // a closed panel (Install, then close mid-install) must not start a
      // 15 s probe that outlives the session; open() re-probes on every
      // open and a fresh Install press re-arms recovery instead. The probe
      // re-checks python presence from the SYSTEM every 15 s —
      // installingPython is false now, so it ignores the markers and plain
      // `command -v python3` decides; a leftover marker cannot
      // false-positive it. When the user finishes the install in the
      // still-open terminal, the next tick flips the open panel to
      // pythonReady through handleProbe's recovered path — no reopen
      // needed.
      if (root.opened) pythonSlowPoll.restart()
    }
  }

  // Post-timeout recovery poll: 15 s probes that keep an OPEN panel
  // learning after the 5-minute install watchdog gave up. Only ever
  // started while the panel is open (the timeout gates its restart on
  // root.opened), and stopped on any terminal verdict
  // (present/canceled/failed), when a new install attempt starts, and
  // when the panel closes; open() re-probes on every open, so reopening
  // resumes recovery from a settled state.
  Timer {
    id: pythonSlowPoll
    interval: 15000
    repeat: true
    onTriggered: root.probePython()
  }

  // ---- background update jobs: the reader plumbing (ticket #11) --------------
  // One reusable FileView drains the document queue: pumpJobReads points
  // it at one job document at a time (a path change aborts any in-flight
  // read, so the sequential queue never races on the instance), and the
  // handlers adopt the result. loadFailed means "no document" — the file
  // is missing, the normal case for an app that never ran a background
  // update — which adopts idle. A present-but-corrupt file READS fine and
  // adopts interrupted instead (JobDocs's no-document/corrupt split).
  // preload keeps every read asynchronous; printErrors stays off because
  // a missing document is idle, not an error worth a log line.
  FileView {
    id: jobFileView
    path: ""
    preload: true
    printErrors: false
    onLoaded: {
      var id = root.jobReadingId
      root.jobReadingId = ""
      jobReadWatchdog.stop()
      // The path guard retires a superseded read: only the document the
      // queue actually pointed at may adopt. `null` is the pid argument
      // this panel can actually stand behind: QML cannot probe a pid
      // (the third JobDocs state, "caller cannot probe"), so the heartbeat
      // rule decides alone — asserting `true` would claim a probe that
      // never happened and make pidDead unreachable.
      if (id !== "" && jobFileView.path === JobDocs.jobDocumentPath(root.jobsDir, id)) {
        root.adoptJob(id, JobDocs.adoptDocumentForPanel(jobFileView.text(), null, Date.now()))
      }
      root.pumpJobReads()
    }
    onLoadFailed: {
      var id = root.jobReadingId
      root.jobReadingId = ""
      jobReadWatchdog.stop()
      // Same superseded-read guard as onLoaded: a failure report for a
      // read the queue has already repointed away from must not adopt
      // idle for the wrong id.
      if (id !== "" && jobFileView.path === JobDocs.jobDocumentPath(root.jobsDir, id)) {
        root.adoptJob(id, root.idleParsed())
      }
      root.pumpJobReads()
    }
  }

  // Read watchdog: a job document is a tiny local JSON file and a failed
  // read reports through loadFailed; if a reader ever wedges anyway,
  // retire it (5 s is generous) so the queue — and the progress UI it
  // feeds — keeps moving. A stuck read must never fossilize.
  Timer {
    id: jobReadWatchdog
    interval: 5000
    repeat: false
    onTriggered: {
      if (root.jobReadingId === "") return
      root.jobReadingId = ""
      root.pumpJobReads()
    }
  }

  // The job poll: only while the panel is open AND at least one row
  // carries a non-idle decision, re-read every job document at jobPollMs
  // (3 s — the writer heartbeats every 2 s, the contract goes stale at
  // 120 s). An idle panel runs no poll and performs no reads at all; the
  // binding drops when the last active job adopts a terminal decision.
  Timer {
    id: jobPoll
    interval: root.jobPollMs
    repeat: true
    running: root.opened && root.jobsBusy
    onTriggered: root.refreshJobStates()
  }

  Timer {
    id: confirmResetTimer
    interval: root.confirmResetMs
    repeat: false
    onTriggered: root.confirmRemoveId = ""
  }

  Timer {
    id: launchSettleTimer
    interval: root.launchSettleMs
    repeat: false
    onTriggered: root.refresh()
  }

  // Periodic re-list while the panel is open and idle: a row's "● running"
  // flag otherwise goes stale forever if the app is closed outside this panel
  // (explicit Refresh / reopen / post-launch settle are the only other
  // refresh paths). The running binding auto-pauses while any backend
  // operation — including a live background update job — is in flight and
  // resumes after; refresh()'s own busy guard makes a racing trigger a
  // no-op anyway.
  Timer {
    id: runningPollTimer
    interval: 10000
    repeat: true
    running: root.opened && !root.busyList && !root.busyIntegrate && !root.busyRemove
      && !root.busyUpdates && !root.busyPick && !root.jobsBusy
      && !root.settingsBusy && !root.sourceBusy
    onTriggered: root.refresh()
  }

  // Status notices only: clears the accent banner after statusTimeoutMs.
  // Errors bypass this timer entirely (see onStatusTextChanged below) and
  // stay until the Dismiss button.
  Timer {
    id: statusTimer
    interval: root.statusTimeoutMs
    repeat: false
    onTriggered: {
      Model.clearStatus()
      root.syncFromModel()
    }
  }

  IpcHandler {
    target: root.moduleName

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): string { root.refresh(); return "ok" }
    function list(): string { return JSON.stringify(Model.state.items) }
  }

  PanelWindow {
    id: window
    visible: root.opened
    anchors {
      top: !root.pinned
      bottom: !root.pinned
      left: !root.pinned
      right: !root.pinned
    }
    // Pinned: drop the screen anchors and size the layer surface to the
    // card plus a margin; unpinned the anchors win and the implicit sizes
    // below are inert (ProxyWindowBase consumes implicit size on
    // layer-shell windows — `width`/`height` are deprecated there).
    implicitWidth: root.pinned ? card.width + Style.gapsOut * 2 : 0
    implicitHeight: root.pinned ? card.height + Style.gapsOut * 2 : 0
    color: "transparent"
    exclusionMode: ExclusionMode.Ignore

    WlrLayershell.namespace: "io.github.glasschan.appimage"
    WlrLayershell.layer: WlrLayer.Overlay
    // Exclusive while the overlay is open so Escape/`r`/`i`/`u` always work;
    // pinned keeps OnDemand so the browser keeps typing until clicked.
    WlrLayershell.keyboardFocus: root.opened
      ? (root.pinned ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.Exclusive)
      : WlrKeyboardFocus.None

    // Theme-derived scrim over the desktop behind the card.
    Rectangle {
      anchors.fill: parent
      visible: !root.pinned
      color: Util.alpha(Color.background, 0.45)
    }

    MouseArea {
      anchors.fill: parent
      visible: !root.pinned
      acceptedButtons: Qt.AllButtons
      onClicked: root.requestClose()
    }

    BorderSurface {
      id: card
      anchors.centerIn: parent
      // Pinned the window sizes to the card, so the cap must fall back to
      // the screen; unpinned the window fills the screen and sizes as before.
      width: Math.min(Style.space(440), root.pinned
        ? (window.screen ? window.screen.width : 1920) - Style.gapsOut * 2
        : window.width - Style.gapsOut * 2)
      height: Math.min(
        contentCol.implicitHeight + card.contentTopInset + card.contentBottomInset,
        root.pinned
          ? (window.screen ? window.screen.height : 1080) - Style.gapsOut * 2
          : window.height - Style.gapsOut * 2)
      color: Color.popups.background
      borderSpec: Border.surfaceSpec("popups", "border", Color.popups.border, Math.max(1, Style.space(2)))
      radius: Style.cornerRadius
      padding: Style.spacing.popupPadding

      // Swallow clicks on the card so they don't reach the dismissal
      // MouseArea behind it.
      MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.AllButtons
        onClicked: {}
      }

      PanelKeyCatcher {
        id: keyCatcher
        anchors.fill: parent
        anchors.margins: card.padding
        // Inline editors need raw keys while they hold focus (same pattern
        // as the weather panel's inline editor): the picker's path field,
        // the source editor's dynamic fields (tracked by focus count —
        // moving between them fires leave/gain out of order), the
        // NumberFields' inner SpinBox editors, and the per-row manager
        // dropdown's popup — root.sourceDropdownOpen mirrors its popupOpen
        // because the popup list is not in this panel's item tree.
        blocked: root.formEditing
        // The confirm step owns Escape first: Esc dismisses the preview
        // back to the picker with zero side effects; a second Esc (no
        // preview showing) closes the panel as before. The force-quit
        // card (ticket #27) outranks both: Esc cancels it, nothing runs.
        // The "Update all" card (issue #52) is modal in the same way:
        // Esc dismisses it and no job is spawned.
        onCloseRequested: {
          if (root.forceQuitId !== "") root.dismissForceQuit()
          else if (root.updateAllOpen) root.dismissUpdateAll()
          else if (root.previewOpen) root.dismissPreview()
          else root.requestClose()
        }
        // Enter on the catcher confirms the shown preview (while the path
        // field holds focus, its own Enter reaches requestPreview through
        // SafeTextField.accepted instead). Confirm on the force-quit card
        // wins over a preview: that card is the later, blocking decision.
        // The "Update all" card is blocking in the same way: Enter starts
        // the batch the card names.
        onActivateRequested: {
          if (root.forceQuitId !== "") root.confirmForceQuit()
          else if (root.updateAllOpen) root.confirmUpdateAll()
          else if (root.previewOpen) root.confirmPreview()
        }
        onTextKey: function(t) {
          // Behind the confirm card the single-letter shortcuts would fire
          // blind actions (refresh/integrate/checks); the preview owns the
          // keyboard from the moment its call starts until it is confirmed
          // or dismissed (a refresh racing the fetch would only earn the
          // shared-process "busy" banner). The force-quit card is modal in
          // the same way while it asks — and so is the "Update all" card.
          if (root.forceQuitId !== "" || root.updateAllOpen
              || root.previewOpen || root.busyPreview) return
          if (t === "r" || t === "R") root.refresh()
          else if (t === "i" || t === "I") root.integrateRequested()
          else if (t === "u" || t === "U") root.checkUpdates()
        }

        Column {
          id: contentCol
          width: parent.width
          spacing: Style.spacing.md

          // ---- Header: title, counts, actions --------------------------
          Item {
            width: parent.width
            implicitHeight: headerRow.implicitHeight

            Row {
              id: headerRow
              anchors.left: parent.left
              anchors.right: parent.right
              spacing: Style.spacing.controlGap

              Column {
                id: headerText
                spacing: Style.spacing.xxs
                anchors.verticalCenter: parent.verticalCenter

                Text {
                  text: root.strings.panelTitle
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.heading
                  font.bold: true
                }

                Text {
                  text: root.subtitleText
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }

              Item { width: 1; height: 1; anchors.verticalCenter: parent.verticalCenter }

              // The shell Button has no image slot (its iconText is a
              // Nerd Font glyph), so the row below supplies the tabler
              // icon while Button keeps owning every state visual (hover,
              // focus, fills); the width override mirrors Button's own
              // label-based sizing math over that row. The caption lives
              // in the tooltip, like the icon-only buttons to the right.
              Button {
                id: integrateButton
                text: ""
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                tooltipText: root.pickerOpen ? root.strings.pickerCancel : root.strings.integrate
                implicitWidth: integrateRow.implicitWidth
                  + integrateButton.horizontalPadding * 2
                  + integrateButton._reservedBorderLeft
                  + integrateButton._reservedBorderRight

                Row {
                  id: integrateRow
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  ThemeIcon {
                    name: "plus"
                    size: Style.font.icon
                    strokeWidth: 1.75
                    color: integrateButton.foreground
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                onClicked: {
                  if (root.pickerOpen) root.closePicker()
                  else root.integrateRequested()
                }
              }

              Button {
                id: refreshButton
                text: ""
                enabled: !root.busyList
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                tooltipText: root.strings.refresh
                implicitWidth: refreshRow.implicitWidth
                  + refreshButton.horizontalPadding * 2
                  + refreshButton._reservedBorderLeft
                  + refreshButton._reservedBorderRight

                Row {
                  id: refreshRow
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  ThemeIcon {
                    name: "refresh"
                    size: Style.font.icon
                    strokeWidth: 1.75
                    color: refreshButton.foreground
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                onClicked: root.refresh()
              }

              // F6 as an icon-only header button (tooltip carries the
              // caption the row buttons would show); same Button+ThemeIcon
              // row pattern as Integrate/Refresh above.
              Button {
                id: checkUpdatesButton
                text: ""
                enabled: !root.busyUpdates
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                tooltipText: root.strings.checkUpdates
                implicitWidth: checkUpdatesRow.implicitWidth
                  + checkUpdatesButton.horizontalPadding * 2
                  + checkUpdatesButton._reservedBorderLeft
                  + checkUpdatesButton._reservedBorderRight

                Row {
                  id: checkUpdatesRow
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  ThemeIcon {
                    name: "cloud-download"
                    size: Style.font.icon
                    strokeWidth: 1.75
                    color: checkUpdatesButton.foreground
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                onClicked: root.checkUpdates()
              }

              // "Update all" (issue #52): one action for every pending
              // app, next to the check-updates action and reusing the
              // row's arrow-up icon. The click only OPENS the consent
              // card — nothing is spawned until the user accepts it. The
              // button is enabled exactly while at least one app has a
              // pending release the backend would accept.
              Button {
                id: updateAllButton
                text: ""
                enabled: root.pendingIds.length > 0
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                tooltipText: root.strings.updateAllTooltip
                implicitWidth: updateAllRow.implicitWidth
                  + updateAllButton.horizontalPadding * 2
                  + updateAllButton._reservedBorderLeft
                  + updateAllButton._reservedBorderRight

                Row {
                  id: updateAllRow
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  ThemeIcon {
                    name: "arrow-up"
                    size: Style.font.icon
                    strokeWidth: 1.75
                    color: updateAllButton.enabled
                      ? updateAllButton.foreground
                      : root.dim
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                onClicked: root.requestUpdateAll()
              }

              // Pin toggle: flips the overlay into a compact floating
              // window (session-only) so other apps stay clickable.
              Button {
                id: pinButton
                text: ""
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                tooltipText: root.pinned ? root.strings.pinTooltipUnpin : root.strings.pinTooltip
                implicitWidth: pinRow.implicitWidth
                  + pinButton.horizontalPadding * 2
                  + pinButton._reservedBorderLeft
                  + pinButton._reservedBorderRight

                Row {
                  id: pinRow
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  ThemeIcon {
                    name: "pin"
                    size: Style.font.icon
                    strokeWidth: 1.75
                    color: pinButton.foreground
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                onClicked: root.pinned = !root.pinned
              }

              // Settings gear: toggles the settings card below.
              Button {
                id: settingsButton
                text: ""
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                tooltipText: root.strings.settingsTitle
                implicitWidth: settingsRow.implicitWidth
                  + settingsButton.horizontalPadding * 2
                  + settingsButton._reservedBorderLeft
                  + settingsButton._reservedBorderRight

                Row {
                  id: settingsRow
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  ThemeIcon {
                    name: "settings"
                    size: Style.font.icon
                    strokeWidth: 1.75
                    color: settingsButton.foreground
                    anchors.verticalCenter: parent.verticalCenter
                  }
                }

                onClicked: root.toggleSettings()
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          // ---- Error / status area -------------------------------------
          BorderSurface {
            id: noticeBanner
            width: parent.width
            visible: root.lastError !== "" || root.statusText !== ""
            implicitHeight: visible ? noticeRow.implicitHeight + Style.spacing.sm * 2 : 0
            color: Util.alpha(root.lastError !== "" ? root.urgent : Color.accent, 0.10)
            borderSpec: Border.flat(Util.alpha(root.lastError !== "" ? root.urgent : Color.accent, 0.55), Math.max(1, Style.normalBorderWidth))
            radius: Style.cornerRadius

            Row {
              id: noticeRow
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.spacing.sm
              anchors.rightMargin: Style.spacing.sm
              spacing: Style.spacing.sm

              Text {
                text: root.lastError !== "" ? root.lastError : root.statusText
                color: root.lastError !== "" ? root.urgent : Color.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - dismissButton.width - Style.spacing.sm
              }

              Button {
                id: dismissButton
                text: root.strings.dismiss
                anchors.verticalCenter: parent.verticalCenter
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                implicitHeight: root.controlHeight
                onClicked: {
                  Model.clearError()
                  Model.clearStatus()
                }
              }
            }
          }

          // ---- Force-quit confirm card (ticket #27) ---------------------
          // Shown instead of spawning when Update lands on a row the last
          // listing said runs. It names the app and how many processes the
          // confirm will close (the listing's `running_pids`; a count of 0
          // — unknown — keeps the count-free sentence). Enter confirms, Esc
          // cancels (PanelKeyCatcher wiring above); Cancel spawns nothing
          // and leaves the row as it was.
          BorderSurface {
            id: forceQuitCard
            width: parent.width
            visible: root.forceQuitId !== ""
            implicitHeight: visible ? forceQuitCol.implicitHeight + Style.spacing.sm * 2 : 0
            color: Util.alpha(root.urgent, 0.09)
            borderSpec: Border.flat(Util.alpha(root.urgent, 0.45), Math.max(1, Style.normalBorderWidth))
            radius: Style.cornerRadius

            Column {
              id: forceQuitCol
              width: parent.width - Style.spacing.sm * 2
              anchors.centerIn: parent
              spacing: Style.spacing.xs

              Text {
                text: root.strings.forceQuitTitle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }

              Text {
                text: Model.forceQuitBody(root.forceQuitName, root.forceQuitPids)
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                width: parent.width
              }

              Row {
                spacing: Style.spacing.xs

                Button {
                  text: root.strings.forceQuitConfirm
                  // The confirm card can outlive its row's job-start reply
                  // (a slow backend): never stack a second job by hand.
                  enabled: !root.jobActive(root.forceQuitId)
                  foreground: root.foreground
                  accent: root.urgent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.confirmForceQuit()
                }

                Button {
                  text: root.strings.pickerCancel
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.dismissForceQuit()
                }
              }

              Text {
                text: root.strings.forceQuitKeysHint
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // ---- "Update all" consent card (issue #52) -------------------
          // Opened by the header's Update-all action; it starts NOTHING
          // by itself. The card names the apps the batch would update and
          // the processes it would close (the listing's `running_pids`;
          // a count that is not determinable keeps the count-free
          // sentence). Enter confirms, Esc cancels (PanelKeyCatcher
          // wiring above); Cancel spawns nothing and closes no app.
          BorderSurface {
            id: updateAllCard
            width: parent.width
            visible: root.updateAllOpen
            implicitHeight: visible ? updateAllCol.implicitHeight + Style.spacing.sm * 2 : 0
            color: Util.alpha(root.urgent, 0.09)
            borderSpec: Border.flat(Util.alpha(root.urgent, 0.45), Math.max(1, Style.normalBorderWidth))
            radius: Style.cornerRadius

            Column {
              id: updateAllCol
              width: parent.width - Style.spacing.sm * 2
              anchors.centerIn: parent
              spacing: Style.spacing.xs

              Text {
                text: root.strings.updateAllTitle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }

              Text {
                text: root.updateAllSummary.appsLine
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                width: parent.width
              }

              Text {
                text: root.updateAllSummary.processLine
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                width: parent.width
              }

              Row {
                spacing: Style.spacing.xs

                Button {
                  text: root.strings.updateAllConfirm
                  // The card can outlive a slow spawn: never stack a
                  // second job for an app that is already updating.
                  enabled: root.pendingIds.length > 0
                  foreground: root.foreground
                  accent: root.urgent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.confirmUpdateAll()
                }

                Button {
                  text: root.strings.pickerCancel
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.dismissUpdateAll()
                }
              }

              Text {
                text: root.strings.updateAllKeysHint
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // ---- Python dependency card (authorized install) --------------
          // Shown only after the probe positively confirmed python3 is
          // absent. The button never installs anything by itself: it opens
          // omarchy's floating terminal presenting `/usr/bin/omarchy pkg
          // add python` so the user watches and authorizes every step
          // (hyprmoncfg's plugin-managed-dependency pattern).
          BorderSurface {
            id: pythonCard
            width: parent.width
            visible: root.pythonStateKnown && root.pythonMissing
            implicitHeight: visible ? pythonCol.implicitHeight + Style.spacing.sm * 2 : 0
            color: Util.alpha(Color.urgent, 0.06)
            borderSpec: Border.flat(Util.alpha(Color.urgent, 0.35), Math.max(1, Style.normalBorderWidth))
            radius: Style.cornerRadius

            Column {
              id: pythonCol
              width: parent.width - Style.spacing.sm * 2
              anchors.centerIn: parent
              spacing: Style.spacing.xs

              Text {
                text: root.strings.pythonMissingTitle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }

              Text {
                text: root.strings.pythonMissingHint
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
                width: parent.width
              }

              Button {
                width: parent.width
                text: root.installingPython
                  ? root.strings.installingPython : root.strings.installPython
                enabled: !root.installingPython
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.bodySmall
                implicitHeight: root.controlHeight
                onClicked: root.installPython()
              }
            }
          }

          // ---- Integrate picker (F2) ------------------------------------
          BorderSurface {
            id: pickerCard
            width: parent.width
            visible: root.pickerOpen
            implicitHeight: visible ? pickerCol.implicitHeight + Style.spacing.sm * 2 : 0
            color: Util.alpha(Color.accent, 0.06)
            borderSpec: Border.flat(Util.alpha(Color.accent, 0.35), Math.max(1, Style.normalBorderWidth))
            radius: Style.cornerRadius

            Column {
              id: pickerCol
              width: parent.width - Style.spacing.sm * 2
              anchors.centerIn: parent
              spacing: Style.spacing.xs

              Text {
                text: root.strings.pickerTitle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }

              Row {
                width: parent.width
                spacing: Style.spacing.xs

                Loader {
                  id: pickerFieldLoader
                  active: root.pickerOpen
                  width: parent.width - pickerGoButton.width
                    - pickerBrowseButton.width - Style.spacing.xs * 2
                  sourceComponent: pickerFieldComp
                }

                Button {
                  id: pickerBrowseButton
                  text: root.busyPick ? root.strings.pickerBrowsing
                    : root.strings.pickerBrowse
                  // Locked with Go/Downloads while a preview runs (#18):
                  // a fill mid-preview-call could only produce an answer
                  // the stale-drop has to throw away.
                  enabled: !root.pickerBusy
                  anchors.verticalCenter: parent.verticalCenter
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.browseForFile()
                }

                // The row's primary action tracks the flow's step (#18):
                // Go previews a fresh path, Confirm confirms the shown
                // preview; both show their in-flight copy while busy.
                Button {
                  id: pickerGoButton
                  text: root.busyIntegrate ? root.strings.integrateWorking
                    : (root.busyPreview ? root.strings.previewChecking
                      : (root.previewOpen ? root.strings.previewConfirm
                        : root.strings.pickerGo))
                  enabled: !root.pickerBusy && root.pickerPath.trim() !== ""
                  anchors.verticalCenter: parent.verticalCenter
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.previewOpen
                    ? root.confirmPreview() : root.requestPreview(root.pickerPath)
                }
              }

              // ---- Preview card (#18): confirm before anything installs --
              // Rendered from Model.parsePreviewJson's defensive mapping
              // (unknown keys tolerated, null app/destination normalize to
              // empty). The reduced-assurance line is deliberately
              // prominent — bold and urgent-colored: informing BEFORE the
              // download is the whole point of the preview. Enter confirms,
              // Escape goes back (PanelKeyCatcher wiring above).
              BorderSurface {
                id: previewCard
                width: parent.width
                visible: root.previewOpen
                implicitHeight: visible ? previewCol.implicitHeight + Style.spacing.sm * 2 : 0
                color: Util.alpha(Color.accent, 0.09)
                borderSpec: Border.flat(Util.alpha(Color.accent, 0.45), Math.max(1, Style.normalBorderWidth))
                radius: Style.cornerRadius

                Column {
                  id: previewCol
                  width: parent.width - Style.spacing.sm * 2
                  anchors.centerIn: parent
                  spacing: Style.spacing.xs

                  Text {
                    text: root.strings.previewTitle
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }

                  Text {
                    text: root.previewAppLine()
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    elide: Text.ElideRight
                    width: parent.width
                  }

                  Text {
                    visible: root.previewDoc && root.previewDoc.appVersion !== ""
                    text: root.previewDoc
                      ? Model.formatCount(root.previewDoc.appVersion,
                          root.strings.previewVersion) : ""
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    text: root.previewDestinationLine()
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                    width: parent.width
                  }

                  Text {
                    visible: root.previewDoc && root.previewDoc.reducedAssurance
                    text: root.strings.previewReducedAssurance
                    color: root.urgent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                    wrapMode: Text.WordWrap
                    width: parent.width
                  }

                  // The backend's own sentence, rendered verbatim (the
                  // contract designates `note` as panel-copy).
                  Text {
                    text: root.previewDoc ? root.previewDoc.verificationNote : ""
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                    width: parent.width
                  }

                  Text {
                    visible: root.previewDoc && root.previewDoc.alreadyIntegrated
                    text: root.strings.previewAlreadyIntegrated
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                    width: parent.width
                  }

                  Text {
                    visible: root.previewDoc && root.previewDoc.nameConflict
                    text: root.strings.previewNameConflict
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.WordWrap
                    width: parent.width
                  }

                  Row {
                    spacing: Style.spacing.xs

                    Button {
                      text: root.strings.previewConfirm
                      enabled: !root.busyIntegrate && !root.busyPreview
                      foreground: root.foreground
                      accent: Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.bodySmall
                      implicitHeight: root.controlHeight
                      onClicked: root.confirmPreview()
                    }

                    Button {
                      text: root.strings.pickerCancel
                      enabled: !root.busyIntegrate && !root.busyPreview
                      foreground: root.foreground
                      accent: Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.bodySmall
                      implicitHeight: root.controlHeight
                      onClicked: root.dismissPreview()
                    }
                  }

                  Text {
                    text: root.strings.previewKeysHint
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              Text {
                visible: root.pickerFiles.length > 0
                text: root.strings.pickerDownloads
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }

              Flickable {
                visible: root.pickerFiles.length > 0
                width: parent.width
                height: Math.min(pickCol.implicitHeight, Style.space(132))
                contentWidth: width
                contentHeight: pickCol.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                interactive: contentHeight > height

                Column {
                  id: pickCol
                  width: parent.width
                  spacing: Style.spacing.xxs

                  Repeater {
                    model: root.pickerFiles

                    Button {
                      required property var modelData
                      width: pickCol.width
                      text: modelData.name
                      foreground: root.foreground
                      accent: Color.accent
                      fontFamily: root.fontFamily
                      fontSize: Style.font.caption
                      enabled: !root.pickerBusy
                      // Quick-picks ride the same confirm flow: seed the
                      // field so the preview (and a later Confirm) name
                      // the exact file, then preview — never integrate
                      // directly.
                      onClicked: {
                        root.pickerPath = modelData.path
                        root.requestPreview(modelData.path)
                      }
                    }
                  }
                }
              }

              Text {
                visible: root.pickerFiles.length === 0
                text: root.strings.pickerDownloadsEmpty
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
                width: parent.width
              }
            }
          }

          // ---- Settings card (F8 cadence only — per-app sources are on each row) -----
          BorderSurface {
            id: settingsCard
            width: parent.width
            visible: root.settingsOpen
            implicitHeight: visible ? settingsCol.implicitHeight + Style.spacing.xl * 2 : 0
            color: Util.alpha(Color.accent, 0.06)
            borderSpec: Border.flat(Util.alpha(Color.accent, 0.35), Math.max(1, Style.normalBorderWidth))
            radius: Style.cornerRadius

            Column {
              id: settingsCol
              width: parent.width - Style.spacing.xl * 2
              anchors.centerIn: parent
              spacing: Style.spacing.xs

              Text {
                text: root.strings.settingsTitle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }

              // ---- update-check cadence (F8) --------------------------------
              PanelSectionHeader {
                text: root.strings.settingsUpdateChecks
                foreground: root.foreground
                width: parent.width
              }

              Row {
                width: parent.width
                spacing: Style.spacing.sm

                Text {
                  text: root.editEnabled ? root.strings.enabledLabel : root.strings.disabledLabel
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                  width: parent.width - settingsToggle.width - Style.spacing.sm
                  anchors.verticalCenter: parent.verticalCenter
                }

                // Caller-owned value: `checked` binds to the working copy
                // and toggled() flips it (ToggleSwitch's own contract).
                ToggleSwitch {
                  id: settingsToggle
                  checked: root.editEnabled
                  foreground: root.foreground
                  accent: Color.accent
                  enabled: !root.settingsBusy
                  anchors.verticalCenter: parent.verticalCenter
                  onToggled: root.editEnabled = !root.editEnabled
                }
              }

              Loader {
                id: settingsNumbersLoader
                active: root.settingsOpen
                width: parent.width
                sourceComponent: settingsNumbersComp
              }

              Text {
                text: root.strings.notificationsOnNewUpdates
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
                width: parent.width
              }

              Button {
                id: settingsSaveButton
                text: root.strings.settingsSave
                enabled: !root.settingsBusy
                foreground: root.foreground
                accent: Color.accent
                fontFamily: root.fontFamily
                fontSize: Style.font.caption
                implicitHeight: root.controlHeight
                onClicked: root.saveSettings()
              }
            }
          }

          // ---- Body: loading / list / empty ------------------------------
          // The install card replaces the whole body while python3 is
          // confirmed missing — a list of nothing is noise next to it.
          Item {
            id: body
            visible: !(root.pythonStateKnown && root.pythonMissing)
            width: parent.width
            implicitHeight: root.items.length === 0
              ? (root.busyList ? Style.space(110) : emptyCol.implicitHeight)
              : Math.min(listCol.implicitHeight, Style.space(320))

            Column {
              id: emptyCol
              visible: root.items.length === 0 && !root.busyList
              anchors.centerIn: parent
              width: parent.width
              spacing: Style.spacing.sm

              ThemeIcon {
                name: "cube-unfolded"
                size: Style.font.displayLarge * 1.5
                strokeWidth: 1.5
                color: root.dim
                anchors.horizontalCenter: parent.horizontalCenter
              }

              Text {
                text: root.strings.emptyTitle
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                font.bold: true
                anchors.horizontalCenter: parent.horizontalCenter
              }

              Text {
                text: root.strings.emptyHint
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                width: parent.width
              }
            }

            Text {
              visible: root.busyList && root.items.length === 0
              anchors.centerIn: parent
              text: root.strings.loading
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }

            Flickable {
              id: listFlick
              visible: root.items.length > 0
              anchors.fill: parent
              contentWidth: width
              contentHeight: listCol.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds
              interactive: contentHeight > height
              opacity: root.busyList ? 0.5 : 1

              Column {
                id: listCol
                width: listFlick.width
                spacing: Style.spacing.xs

                Repeater {
                  model: root.items

                  Column {
                    id: rowOuter
                    required property var modelData
                    width: listCol.width
                    spacing: Style.spacing.xs

                      BorderSurface {
                        id: itemRow
                        width: listCol.width
                        implicitHeight: rowContent.implicitHeight + Style.spacing.sm * 2
                        radius: Style.cornerRadius
                        color: Style.normalFillFor(root.foreground, Color.accent, root.urgent)
                        borderSpec: Border.none()

                        // Clicking anywhere on the card (outside the action
                        // buttons, which stack above) launches the app (F4).
                        MouseArea {
                          anchors.fill: parent
                          acceptedButtons: Qt.LeftButton
                          onClicked: root.launchItem(rowOuter.modelData)
                        }

                        Row {
                          id: rowContent
                          anchors.left: parent.left
                          anchors.right: parent.right
                          anchors.verticalCenter: parent.verticalCenter
                          anchors.leftMargin: Style.spacing.sm
                          anchors.rightMargin: Style.spacing.sm
                          spacing: Style.spacing.sm

                          // Extracted icon when it exists (png then svg
                          // next to the AppImage under .icons/, CONTRACT.md
                          // filesystem layout), tabler cube-unfolded icon otherwise.
                          Image {
                            id: appIcon
                            property int iconIndex: 0
                            readonly property var iconCandidates: {
                              var list = []
                              var base = rowOuter.modelData.iconBase
                              if (base) {
                                var exts = [".png", ".svg"]
                                for (var i = 0; i < exts.length; i++) {
                                  list.push(Util.fileUrl(base + exts[i]))
                                }
                              }
                              return list
                            }
                            anchors.verticalCenter: parent.verticalCenter
                            visible: iconIndex < iconCandidates.length
                            source: visible ? iconCandidates[iconIndex] : ""
                            width: Style.font.body * 1.7
                            height: width
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            onStatusChanged: function(status) {
                              if (status === Image.Error) iconIndex++
                            }
                          }

                          ThemeIcon {
                            visible: !appIcon.visible || appIcon.status === Image.Error
                            anchors.verticalCenter: parent.verticalCenter
                            name: "cube-unfolded"
                            size: Style.font.body * 1.7
                            color: root.dim
                          }

                          Column {
                            spacing: Style.spacing.xxs
                            anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - actionsRow.width - Style.space(34) - Style.spacing.sm * 2

                            Text {
                              text: rowOuter.modelData.name
                              color: root.foreground
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.body
                              font.bold: true
                              elide: Text.ElideRight
                              width: parent.width
                            }

                            // The row's IDENTITY line: the version and the
                            // running dot. ONE bounded, elided Text —
                            // separate Texts in a Row have no width bound
                            // and painted over the action buttons as soon
                            // as the fragments got long (v0.9.1).
                            Text {
                              width: parent.width
                              visible: text !== ""
                              textFormat: Text.StyledText
                              elide: Text.ElideRight
                              color: root.dim
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.caption
                              text: root.metaText(
                                Model.rowIdentityMeta(rowOuter.modelData))
                            }

                            // The row's STATUS line: the pending release
                            // (or "cannot verify") and the live job
                            // progress. It has its own line so the update
                            // status never reads as part of the version
                            // (issue #51) — and it stays ONE bounded,
                            // elided Text, never a Row of Texts.
                            Text {
                              width: parent.width
                              visible: text !== ""
                              textFormat: Text.StyledText
                              elide: Text.ElideRight
                              color: root.dim
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.caption
                              text: root.metaText(Model.rowStatusMeta(
                                rowOuter.modelData,
                                root.jobActive(rowOuter.modelData.id)
                                  ? root.jobProgressLabel(rowOuter.modelData.id)
                                  : ""))
                            }

                            // F9: configured update source under the row's meta.
                            Text {
                              visible: rowOuter.modelData.manager !== ""
                              text: rowOuter.modelData.embeddedSource
                                ? Model.formatCount(rowOuter.modelData.manager, root.strings.sourceEmbedded)
                                : rowOuter.modelData.manager
                              color: root.dim
                              font.family: root.fontFamily
                              font.pixelSize: Style.font.caption
                              elide: Text.ElideRight
                              width: parent.width
                            }
                          }

                          Row {
                            id: actionsRow
                            spacing: Style.spacing.xxs
                            anchors.verticalCenter: parent.verticalCenter

                            // Launch (F4) as an icon-only action with a tooltip:
                            // the row card itself launches too, so the caption
                            // was dead weight next to the trash button.
                            PanelActionButton {
                              id: launchButton
                              tooltipText: root.strings.launch
                              foreground: root.foreground
                              hoverColor: Color.accent
                              anchors.verticalCenter: parent.verticalCenter

                              ThemeIcon {
                                anchors.centerIn: parent
                                name: "player-play"
                                size: launchButton.fontSize
                                strokeWidth: 1.75
                                color: launchButton.enabled
                                  ? (launchButton._hot ? launchButton.hoverColor : launchButton.foreground)
                                  : Qt.darker(launchButton.foreground, 2.0)
                              }

                              onClicked: root.launchItem(rowOuter.modelData)
                            }

                            // F7: one-click update, shown only once a
                            // pending release is known for the row.
                            // Per-app single flight (ticket #11): only
                            // THIS row's button dims and loops its arrow
                            // while its job runs — other rows keep
                            // launching/updating in parallel, and the
                            // state behind the flag is the app's job
                            // document, not a click.
                            PanelActionButton {
                              id: updateButton
                              // The button appears exactly when the row's
                              // pending verdict says a newer release is
                              // known (issue #50: the listing's
                              // pending_update) — the same rule the badge
                              // counts on, so the marker, the badge and
                              // this button always agree.
                              // A source that cannot prove its artifact
                              // (ticket #39) offers no update action: the
                              // backend would refuse the install, so the row
                              // shows "cannot verify" instead.
                              visible: rowOuter.modelData.hasUpdate === true
                                && rowOuter.modelData.installable !== false
                              enabled: !root.jobActive(rowOuter.modelData.id)
                              tooltipText: root.strings.updateAction
                              foreground: root.foreground
                              hoverColor: Color.accent
                              fontFamily: root.fontFamily
                              anchors.verticalCenter: parent.verticalCenter

                              ThemeIcon {
                                id: updateIcon
                                anchors.centerIn: parent
                                name: "arrow-up"
                                size: updateButton.fontSize
                                strokeWidth: 1.75
                                color: updateButton.enabled
                                  ? (updateButton._hot ? updateButton.hoverColor : updateButton.foreground)
                                  : Qt.darker(updateButton.foreground, 2.0)

                                // Slide+fade loop: the arrow floats out the
                                // top, reappears below and slides back into
                                // place — "moving up" without a spin (which
                                // reads as "loading" rather than direction).
                                // The icon is anchored, so the shift rides a
                                // Translate transform instead of y.
                                transform: Translate {
                                  id: updateIconShift
                                  y: 0
                                }

                                SequentialAnimation {
                                  running: root.jobActive(rowOuter.modelData.id)
                                  loops: Animation.Infinite

                                  ParallelAnimation {
                                    NumberAnimation {
                                      target: updateIconShift
                                      property: "y"
                                      from: 0
                                      to: -updateIcon.height * 0.6
                                      duration: 220
                                      easing.type: Easing.InQuad
                                    }
                                    NumberAnimation {
                                      target: updateIcon
                                      property: "opacity"
                                      from: 1
                                      to: 0
                                      duration: 220
                                      easing.type: Easing.InQuad
                                    }
                                  }

                                  PropertyAction {
                                    target: updateIconShift
                                    property: "y"
                                    value: updateIcon.height * 0.6
                                  }

                                  ParallelAnimation {
                                    NumberAnimation {
                                      target: updateIconShift
                                      property: "y"
                                      from: updateIcon.height * 0.6
                                      to: 0
                                      duration: 220
                                      easing.type: Easing.OutQuad
                                    }
                                    NumberAnimation {
                                      target: updateIcon
                                      property: "opacity"
                                      from: 0
                                      to: 1
                                      duration: 220
                                      easing.type: Easing.OutQuad
                                    }
                                  }

                                  PauseAnimation { duration: 120 }

                                  // The update can finish mid-loop: park the
                                  // icon back at rest when the loop stops.
                                  onRunningChanged: if (!running) {
                                    updateIconShift.y = 0
                                    updateIcon.opacity = 1
                                  }
                                }
                              }

                              onClicked: root.requestUpdate(rowOuter.modelData)
                            }

                            // F9: open this row's inline source editor.
                            PanelActionButton {
                              id: sourceRowButton
                              tooltipText: root.strings.sourceEditTooltip
                              foreground: root.foreground
                              hoverColor: Color.accent
                              anchors.verticalCenter: parent.verticalCenter

                              ThemeIcon {
                                anchors.centerIn: parent
                                name: "settings"
                                size: sourceRowButton.fontSize
                                strokeWidth: 1.75
                                color: sourceRowButton.enabled
                                  ? (sourceRowButton._hot ? sourceRowButton.hoverColor : sourceRowButton.foreground)
                                  : Qt.darker(sourceRowButton.foreground, 2.0)
                              }

                              onClicked: root.openSourceEditor(rowOuter.modelData)
                            }

                            // Two-click inline remove (F3): first click arms
                            // "Remove?" on this row only, second click runs it.
                            // Blocked while this row's update job runs: the
                            // job is installing over the AppImage, so a
                            // remove would delete the target under it.
                            Button {
                              visible: root.confirmRemoveId === rowOuter.modelData.id
                              text: root.strings.removeConfirm
                              anchors.verticalCenter: parent.verticalCenter
                              foreground: root.urgent
                              accent: root.urgent
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              implicitHeight: root.controlHeight
                              enabled: !root.busyRemove && !root.jobActive(rowOuter.modelData.id)
                              onClicked: root.requestRemove(rowOuter.modelData)
                            }

                            PanelActionButton {
                              id: removeButton
                              visible: root.confirmRemoveId !== rowOuter.modelData.id
                              // same per-row gate as the update button: only
                              // THIS row dims while ITS job runs (other rows
                              // stay removable)
                              enabled: !root.jobActive(rowOuter.modelData.id)
                              tooltipText: root.strings.removeTooltip
                              foreground: root.foreground
                              hoverColor: root.urgent
                              fontFamily: root.fontFamily
                              anchors.verticalCenter: parent.verticalCenter

                              ThemeIcon {
                                anchors.centerIn: parent
                                name: "trash"
                                size: removeButton.fontSize
                                strokeWidth: 1.75
                                color: removeButton.enabled
                                  ? (removeButton._hot ? removeButton.hoverColor : removeButton.foreground)
                                  : Qt.darker(removeButton.foreground, 2.0)
                              }

                              onClicked: root.requestRemove(rowOuter.modelData)
                            }
                          }
                        }
                      }

                    // Inline F9 editor under the row whose gear was
                    // clicked: the app name fixes context, the manager
                    // dropdown swaps the field set from Model.MANAGERS.
                    BorderSurface {
                      width: listCol.width
                      visible: root.sourceEditId === rowOuter.modelData.id
                      implicitHeight: visible ? sourceEditCol.implicitHeight + Style.spacing.xl * 2 : 0
                      color: Util.alpha(Color.accent, 0.06)
                      borderSpec: Border.flat(Util.alpha(Color.accent, 0.25), Math.max(1, Style.normalBorderWidth))
                      radius: Style.cornerRadius

                      Column {
                        id: sourceEditCol
                        width: parent.width - Style.spacing.xl * 2
                        anchors.centerIn: parent
                        spacing: Style.spacing.xs

                        Text {
                          text: root.sourceEditName
                          color: root.foreground
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.bodySmall
                          font.bold: true
                          elide: Text.ElideRight
                          width: parent.width
                        }

                        Dropdown {
                          id: sourceManagerDropdown
                          label: root.strings.sourceManagerLabel
                          width: parent.width
                          options: root.managerOptions
                          foreground: root.foreground
                          accent: Color.accent
                          fontFamily: root.fontFamily
                          enabled: !root.sourceBusy
                          onPopupOpenChanged: root.sourceDropdownOpen = sourceManagerDropdown.popupOpen
                          onChanged: function(value) {
                            root.sourceEditManager = value
                            root.rebuildSourceEditValues()
                          }

                          // selectCurrent() writes `value` directly, which
                          // would sever a plain value: binding on the
                          // persisted delegate; a one-way Binding survives.
                          Binding {
                            target: sourceManagerDropdown
                            property: "value"
                            value: root.sourceEditManager
                          }
                        }

                        Repeater {
                          // The model is global (root.sourceEditManager), so
                          // without the row-visibility gate every other row's
                          // hidden editor card would instantiate the same
                          // editable inputs — no field may exist while its
                          // card is hidden (see the field templates note).
                          model: {
                            if (root.sourceEditId !== rowOuter.modelData.id) return []
                            var mgr = Model.managersByName()[root.sourceEditManager]
                            return mgr ? mgr.fields : []
                          }

                          Loader {
                            required property var modelData
                            width: sourceEditCol.width
                            sourceComponent: modelData.type === "bool"
                              ? sourceBoolFieldComp : sourceStringFieldComp
                            onLoaded: item.fieldKey = modelData.key
                          }
                        }

                        Row {
                          spacing: Style.spacing.xs

                          Button {
                            id: sourceSaveButton
                            text: root.strings.sourceSave
                            enabled: !root.sourceBusy && root.sourceEditManager !== ""
                            foreground: root.foreground
                            accent: Color.accent
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            implicitHeight: root.controlHeight
                            onClicked: root.saveSource()
                          }

                          Button {
                            id: sourceUnsetButton
                            text: root.strings.sourceUnset
                            enabled: !root.sourceBusy
                            foreground: root.foreground
                            accent: Color.accent
                            fontFamily: root.fontFamily
                            fontSize: Style.font.caption
                            implicitHeight: root.controlHeight
                            onClicked: root.unsetSource()
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    // ---- drop-to-integrate (#21): the whole window is a drop target -----
    // Dropping an AppImage opens the picker seeded with the file and
    // straight into the preview flow (handleDropUris — never an install).
    // keys restricts the target to real file drags (text/uri-list), so a
    // dragged web address never even lights the affordance — URL-text
    // drops are out of scope by the spec. The veil + chip below are the
    // hover affordance; theme-derived like every other surface here.
    DropArea {
      id: panelDropArea
      anchors.fill: parent
      enabled: root.opened
      keys: ["text/uri-list"]
      onDropped: function(drop) {
        root.handleDropUris(drop.hasUrls ? drop.urls : [])
      }
    }

    Rectangle {
      anchors.fill: parent
      visible: panelDropArea.containsDrag
      color: Util.alpha(Color.accent, 0.10)

      BorderSurface {
        anchors.centerIn: parent
        implicitWidth: dropChipRow.implicitWidth + Style.spacing.md * 2
        implicitHeight: dropChipRow.implicitHeight + Style.spacing.sm * 2
        color: Color.popups.background
        borderSpec: Border.flat(Color.accent, Math.max(1, Style.normalBorderWidth))
        radius: Style.cornerRadius

        Row {
          id: dropChipRow
          anchors.centerIn: parent
          spacing: Style.spacing.xs

          ThemeIcon {
            name: "cube-unfolded"
            size: Style.font.icon
            strokeWidth: 1.75
            color: Color.accent
            anchors.verticalCenter: parent.verticalCenter
          }

          Text {
            text: root.strings.dropHint
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
            anchors.verticalCenter: parent.verticalCenter
          }
        }
      }
    }
  }

  // Source editor field templates (F9): one Loader per MANAGERS field in
  // the editor above, keyed by `fieldKey` into root.sourceEditValues.
  // Bool values are "true"/"false" strings so the Save argv passes them
  // through verbatim; the focus count tells the key catcher when a field
  // is typing (see formEditing).
  //
  // Text entry goes through SafeTextField (round 3 of review #11415): no
  // native editable input may exist even while its card is open — the
  // Qt 6.12 clipboard hook arms on every readOnly: false QQuickTextInput.
  // `value` mirrors sourceEditValues; `edited` writes it back storage-only
  // (setSourceValue never reassigns the object, so the value binding never
  // re-evaluates mid-typing — same contract the old text binding had).
  Component {
    id: sourceStringFieldComp

    SafeTextField {
      property string fieldKey: ""
      placeholderText: Model.fieldLabel(fieldKey)
      foreground: root.foreground
      accent: Color.accent
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      implicitHeight: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2)
      width: parent ? parent.width : 0
      enabled: !root.sourceBusy
      value: root.sourceEditValues[fieldKey] || ""
      onEdited: function(v) { root.setSourceValue(fieldKey, v) }
      onActiveFocusChanged: root.textFieldFocusCount = activeFocus ? root.textFieldFocusCount + 1 : Math.max(0, root.textFieldFocusCount - 1)
    }
  }

  Component {
    id: sourceBoolFieldComp

    Row {
      property string fieldKey: ""
      width: parent ? parent.width : 0
      spacing: Style.spacing.sm

      Text {
        text: Model.fieldLabel(fieldKey)
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.bodySmall
        anchors.verticalCenter: parent.verticalCenter
      }

      ToggleSwitch {
        checked: root.sourceEditValues[fieldKey] === "true"
        foreground: root.foreground
        accent: Color.accent
        enabled: !root.sourceBusy
        anchors.verticalCenter: parent.verticalCenter
        onToggled: root.setSourceValue(fieldKey,
          root.sourceEditValues[fieldKey] === "true" ? "false" : "true")
      }
    }
  }

  // Integrate-picker URL field (F2) and the settings cadence fields (F8):
  // Loader-created so no text input exists while its card is hidden
  // (defense in depth — see the SafeTextField note above for the round-3
  // rule: no native editable input may exist even while the card is open,
  // so the field type itself carries the guarantee; the Loader gates keep
  // dead instances from piling up at all). The picker field is a
  // SafeTextField: `value` mirrors pickerPath, `edited` writes it back,
  // Enter previews the current path (#18 confirm-before-integrate).
  Component {
    id: pickerFieldComp

    SafeTextField {
      width: parent ? parent.width : 0
      placeholderText: root.strings.pickerPlaceholder
      foreground: root.foreground
      accent: Color.accent
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      implicitHeight: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2)
      enabled: !root.busyIntegrate && !root.busyPreview
      value: root.pickerPath
      onEdited: function(v) {
        root.pickerPath = v
        // A preview computed for the old text is stale the moment the
        // path changes: drop it so Confirm can never install anything
        // other than what is on screen (same-trimmed edits keep it).
        if (root.previewOpen && v.trim() !== root.previewInput) {
          root.dismissPreview()
        }
      }
      onAccepted: root.requestPreview(root.pickerPath)
      // Same count as the editor fields: the key catcher stays blocked
      // while this field types, whatever card is on screen (see
      // textFieldFocusCount).
      onActiveFocusChanged: root.textFieldFocusCount = activeFocus ? root.textFieldFocusCount + 1 : Math.max(0, root.textFieldFocusCount - 1)
    }
  }

  Component {
    id: settingsNumbersComp

    Row {
      width: parent ? parent.width : 0
      spacing: Style.spacing.sm

      readonly property bool anyFieldFocused:
        (intervalField.field.contentItem
         ? intervalField.field.contentItem.activeFocus : false)
        || (delayField.field.contentItem
            ? delayField.field.contentItem.activeFocus : false)

      NumberField {
        id: intervalField
        label: root.strings.checkIntervalMinutes
        value: root.editInterval
        from: 15
        to: 10080
        enabled: !root.settingsBusy
        foreground: root.foreground
        accent: Color.accent
        fontFamily: root.fontFamily
        // Round 3 of review #11415: the SpinBox contentItem is a native
        // TextInput that arms the Qt 6.12 clipboard hook while editable.
        // editable: false is the one switch that flips it readOnly —
        // value changes go through the spin buttons only.
        Component.onCompleted: field.editable = false
        onModified: function(v) { root.editInterval = v }
      }

      NumberField {
        id: delayField
        label: root.strings.firstCheckDelayMinutes
        value: root.editDelay
        from: 0
        to: 1440
        enabled: !root.settingsBusy
        foreground: root.foreground
        accent: Color.accent
        fontFamily: root.fontFamily
        Component.onCompleted: field.editable = false
        onModified: function(v) { root.editDelay = v }
      }
    }
  }

  // Downloads quick-pick sources (F2 fallback picker): both spellings
  // Omarchy users end up with. Merged into root.pickerFiles above.
  FolderListModel {
    id: downloadsUpper
    folder: Util.fileUrl(root.homeDir + "/Downloads")
    nameFilters: ["*.AppImage", "*.appimage", "*.Appimage"]
    showDirs: false
    showDotAndDotDot: false
    sortField: FolderListModel.Name
    onCountChanged: root.rebuildPickerFiles()
    onFolderChanged: root.rebuildPickerFiles()
  }

  FolderListModel {
    id: downloadsLower
    folder: Util.fileUrl(root.homeDir + "/downloads")
    nameFilters: ["*.AppImage", "*.appimage", "*.Appimage"]
    showDirs: false
    showDotAndDotDot: false
    sortField: FolderListModel.Name
    onCountChanged: root.rebuildPickerFiles()
    onFolderChanged: root.rebuildPickerFiles()
  }

  onIntegrateRequested: openPicker()

  // JS-library state carries no property notifications, so statusText is
  // mirrored by syncFromModel — this observer is the panel's one hook on
  // it. A fresh status (with no error showing) arms the auto-hide window;
  // the status emptying again or an error taking over the banner stops it.
  onStatusTextChanged: {
    if (root.statusText !== "" && root.lastError === "") statusTimer.restart()
    else statusTimer.stop()
  }
}

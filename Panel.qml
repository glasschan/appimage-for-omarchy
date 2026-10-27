import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend
import "lib/JobDocs.js" as JobDocs

// Main AppImage manager surface. The plugin declares both "bar-widget" and
// "panel" kinds: this panel is mounted by the shell's panel loader (summon
// path) as a standalone layer-shell surface; BarWidget.qml is just the bar
// launcher button. The qs.Ui.Panel base supplies opened/open()/close()/
// toggle(); open(payloadJson) below extends open() for the summon payload.
// Every action runs backend/main.py (backend/CONTRACT.md) through dedicated
// Process/Timer pairs: a shared short-call pair for refresh-style calls, a
// dedicated pair for the --update --async spawn (refresh traffic can never
// synthetically fail it) and the python probe pair. Integrate previews
// first through the offline --preview-integrate verb and installs only on
// explicit confirm; update runs per row as a detached background job whose
// on-disk job document (reader lib/JobDocs.js) is the only row state; the
// background service lives in Service.qml — this panel only edits what it
// runs on.
Panel {
  id: root
  moduleName: "io.github.glasschan.appimage"
  ipcTarget: "io.github.glasschan.appimage"
  // Own IPC handler: extra verbs (refresh, list) ride alongside the
  // standard lifecycle set; the base would install a second handler on
  // the same target if left enabled.
  manageIpc: false

  // Injected by the shell's panel loader when summoned.
  property var shell: null
  property var manifest: null
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")
  readonly property string homeDir: Quickshell.env("HOME") || ""

  // Cache policy: show the cached list immediately and rescan only when
  // the cache is older than this. Refresh always forces one.
  readonly property int staleAfterMs: 60000
  // Integrating means squashfs extraction + a file copy; give it room.
  readonly property int integrateTimeoutMs: 120000
  // A URL integrate downloads the whole AppImage inside this panel's
  // process — the watchdog must outlast the transfer, not the UI.
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
  // Launch-failure banner: the id of the app awaiting its post-launch
  // settle verdict ("" when none) and the moment the launch was clicked.
  // execDetached cannot report the AppImage runtime's own death, so the
  // verdict comes from the first completed listing that STARTED after the
  // click — a listing already in flight at click time has a pre-spawn ps
  // snapshot and must never judge the launch. On a failed verdict the
  // banner gets the fuse2 hint line only when that same listing's runtime
  // block says "missing" (Model.launchFailureBanner). Status notices fade
  // after this long (matching the backend's desktop-notification expire
  // time); errors never auto-hide.
  readonly property int statusTimeoutMs: 5000

  // ---- background update jobs ----------------------------------------------
  // Row update state is derived from the backend's on-disk job documents
  // (CONTRACT.md "Job documents", reader lib/JobDocs.js): every open and
  // every poll tick re-reads the documents, so a reload mid-download
  // re-derives everything from disk and no spinner can outlive its doc.
  readonly property int jobPollMs: 3000
  // jobs.py's path rule; XDG_STATE_HOME defaults to ~/.local/state.
  readonly property string jobsDir: (Quickshell.env("XDG_STATE_HOME")
    || (homeDir + "/.local/state")) + "/io.github.glasschan.appimage/jobs"
  // desktop_id → adopted decision (JobDocs shape); row bindings read
  // jobStates[id]; jobsBusy gates the job polls.
  property var jobStates: ({})
  property bool jobsBusy: false
  // Sequential single-document read state for jobFileView: at most one
  // job document is being read at any time (a FileView repointed mid-read
  // aborts the old load, so the queue never races on one instance).
  property var jobReadQueue: []
  property string jobReadingId: ""
  // Click grace (per id): a "no document" read landing within
  // jobStartGraceMs of a job-started reply is a race with the grandchild's
  // FIRST write — during the grace it must not overwrite the provisional
  // active decision. Past the grace an absent document means the job died
  // before writing anything, and idle wins.
  readonly property int jobStartGraceMs: 10000
  property var jobStartGraces: ({})

  // Force-quit confirm card: Update on a row whose last --list-installed
  // poll said the app runs opens this card first. The id, name and
  // process count are pinned at click time (the row's model data may be
  // replaced by the refresh that follows); nothing is spawned until
  // confirmForceQuit, and cancel clears the trio with zero side effects.
  // running_pids 0 means "not known" and the card falls back to its
  // count-free sentence.
  property string forceQuitId: ""
  property string forceQuitName: ""
  property int forceQuitPids: 0

  // "Update all" consent card: one action for every pending app. The
  // click only OPENS this card — the batch is spawned after the user
  // accepts it, and dismissing starts nothing. The apps are pinned at
  // open time (id, name, running, running_pids), so a refresh cannot
  // rewrite what the user is consenting to.
  property bool updateAllOpen: false
  property var updateAllApps: []
  // The card's wording, derived from the pinned app list.
  readonly property var updateAllSummary: Model.updateAllSummary(root.updateAllApps)
  // The ids whose background job still has to be spawned; the spawn pair
  // advances the queue when each spawn answers.
  property var updateAllQueue: []

  // Current Backend.run context (null while idle); the Process and Timer
  // signal handlers feed it back into lib/Backend.js.
  property var runCtx: null

  // Backend.run context for the --update --async spawn (null while idle).
  // The spawn owns a dedicated Process/Timer pair: routing it through the
  // shared short-call pair would let any in-flight re-list or update
  // check synthetically fail the click via callBackend's single flight.
  property var updateSpawnCtx: null

  // ---- python3 dependency (authorized install) ------------------------------
  // The probe settles python presence once per open (and every second while
  // an authorized install runs). A confirmed-missing python3 swaps the body
  // for the install card, whose button opens a visible floating terminal
  // presenting `/usr/bin/omarchy pkg add python` — the terminal (and its
  // sudo prompt) is the authorization; the marker files under
  // XDG_RUNTIME_DIR are its report channel. The presented terminal command
  // is a CONSTANT shell source: it reads XDG_RUNTIME_DIR itself via
  // "${XDG_RUNTIME_DIR:?}" expansion, and the runtime dir the panel holds
  // is validated (syntactically by Model.pythonRuntimeDirUsable(), then
  // authoritatively by the prep step) and travels only as argv data, never
  // inside a shell string.
  property bool pythonStateKnown: false
  property bool pythonMissing: false
  property bool installingPython: false
  // Install-button verify gate (verify-then-start): set when the user
  // presses Install and consumed by the next probe result, which alone
  // decides whether the prep step + terminal may start. The verify probe
  // runs with installingPython=false, so stale marker files from an old
  // attempt cannot trigger a new terminal.
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
  // The ids of the apps a batch update would touch: the "Update all"
  // action is enabled exactly while at least one app has a pending
  // release the backend would accept.
  property var pendingIds: []

  // Integrate picker: Quickshell ships no FileDialog, so the picker is a
  // path field, a Browse button backed by the backend's crash-isolated
  // --pick-file dialog (portal first, zenity fallback), and AppImages
  // found in ~/Downloads and ~/downloads.
  property bool pickerOpen: false
  property string pickerPath: ""

  // Confirm-before-integrate preview: pressing the integrate action first
  // runs the offline, fast --preview-integrate and renders the preview
  // card; Confirm then runs the integrate command and Esc/Cancel dismisses
  // back to the picker with zero side effects. previewInput pins the exact
  // input the shown document was computed from: editing the path dismisses
  // the card, so Confirm can only ever install what the user actually saw
  // previewed.
  readonly property int previewTimeoutMs: 10000
  property bool previewOpen: false
  property bool busyPreview: false
  property string previewInput: ""
  property var previewDoc: null

  // Single source of the picker-owning busy state — a browse dialog, a
  // preview call or an integrate run in flight. Browse, Go and the
  // Downloads quick-picks all disable on exactly this conjunction; sites
  // whose guard intentionally differs keep their extra terms separate.
  readonly property bool pickerBusy: busyPick || busyIntegrate || busyPreview

  // Drop-to-integrate: the bar widget converts its dropped URIs through
  // Model.dropUriToPath and hands the usable path over in the summon
  // payload; open() parks it here and tryConsumePendingDrop runs it once
  // the store is idle (the panel's own DropArea path checks the same guard
  // live and reports a busy drop with a status line). The panel window
  // DropArea never queues — this pending pair is transport for a gesture
  // that happened before the panel existed. Closing the panel cancels a
  // still-pending drop (onOpenedChanged).
  property string pendingDropPath: ""
  property int pendingDropIgnored: 0

  // desktop_id whose Remove button is armed for the second click.
  property string confirmRemoveId: ""

  // Settings card: opened by the header gear, mutually exclusive with the
  // integrate picker. The edit* fields are working copies the card
  // mutates; Save pushes them key by key through --set-setting and the
  // store is re-read afterwards (loadSettingsCard).
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
  // Focus count over EVERY editable text field in the panel. PanelKeyCatcher
  // must hand raw keys to whichever field is typing (see formEditing) and be
  // blocked while one holds focus: its Keys handler CONSUMES every key it is
  // handed, so an unblocked catcher silently eats typed characters.
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

  // Emitted by the [Integrate] button and the "i" shortcut.
  signal integrateRequested()
  // Aliases for visual-tree ids the controllers reach (QML ids are
  // lexically scoped to this file, not properties of the object).
  property alias keyCatcherRef: keyCatcher
  property alias pickerCardRef: pickerCard
  property alias listFlickRef: listFlick

  // Domain controllers (one per feature cluster; state stays on root).
  PanelJobs {
    id: jobs
    panel: root
  }
  PanelIntegrate {
    id: integrateCtl
    panel: root
  }
  PanelUpdates {
    id: updatesCtl
    panel: root
  }
  PanelSourceEditor {
    id: sourceEditorCtl
    panel: root
  }
  PanelPython {
    id: pythonCtl
    panel: root
  }


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


  function syncFromModel() {
    items = Model.state.items.slice()
    // One snapshot of the operation state: the panel and the service read
    // the same shape, so a second surface can never disagree about who
    // owns a run.
    var ops = Model.snapshot()
    busyList = ops.busy.list
    busyIntegrate = ops.busy.integrate
    busyPick = ops.busy.pick
    busyRemove = ops.busy.remove
    busyUpdates = ops.busy.updates
    var mirrors = {}
    for (var id in Model.state.jobStates) {
      mirrors[id] = Model.state.jobStates[id].decision
    }
    jobStates = mirrors
    jobsBusy = Model.anyJobActive()
    lastError = Model.state.lastError
    statusText = Model.state.status
    pendingIds = Model.pendingUpdateIds()
    // A bar-widget drop waits here for an idle store (see
    // tryConsumePendingDrop): the sync after the last in-flight call
    // lands is the moment the pending preview can start.
    tryConsumePendingDrop()
  }

  // True while any editor inside the panel should own raw keys instead of
  // the key catcher's shortcuts (see the keyCatcher binding below). The
  // picker and settings fields are Loader-created (see the field templates
  // at the bottom of this file), so guard on the loaded items.
  readonly property bool formEditing: pickerCard.fieldFocused
    || textFieldFocusCount > 0
    || root.sourceDropdownOpen
    || settingsCard.anyFieldFocused

  // ---- lifecycle -----------------------------------------------------------

  function open(payloadJson) {
    Model.setMockMode(false)
    if (backendCommand.length > 0) Model.configureBackend(backendCommand)
    // Bar-widget drop handoff: the payload carries the file the user
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
    // Adopt whatever the on-disk job documents say right now: state
    // comes from disk on EVERY open, so a reload mid-download re-renders
    // the running job (or its interruption) instead of resurrecting an
    // in-memory flag.
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
    // is in flight would overwrite runCtx and misroute the first call's
    // output. Fail the newcomer synthetically (same result shape as
    // Backend.handleExit's timeout report) so the caller's normal failure
    // path runs.
    if (runCtx && runCtx.active === true) {
      // A second call is a WAIT, never a failure: the caller's normal
      // failure path would otherwise invent "Backend call failed (list)"
      // and wipe the status line of the run that is still healthy.
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
  // about one app must name it, and only that same app's next outcome may
  // replace it.
  function reportFailure(result, what, app) {
    var subject = app ? app.id : ""
    var detail = function(message) {
      return app ? Model.appErrorBanner(app.name, message) : message
    }
    // Another operation owns the shared Process: say so quietly and leave
    // whatever status the running operation set exactly as it is — this is
    // a wait, not an outcome.
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

  // Delegating wrappers: the python3 probe/install cluster lives in
  // PanelPython.qml.
  function probePython() { return pythonCtl.probePython() }
  function handleProbe(exitCode, output) { return pythonCtl.handleProbe(exitCode, output) }
  function installPython() { return pythonCtl.installPython() }
  function startPythonInstall() { return pythonCtl.startPythonInstall() }
  function stopPythonRecovery() { return pythonCtl.stopRecoveryPoll() }

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

  // Launch through the backend's `--exec-with-scale` entry point —
  // [backendCommand..., "--exec-with-scale", image, ...args] — so the
  // panel launch renders at the focused monitor's scale exactly like a
  // launcher click; DESKTOPINTEGRATION=1 comes from the verb itself. The
  // scale tail and the env-prefixed argv come from the backend's ONE
  // launch-command builder (the listing's `launch_scale_argv` /
  // `launch_argv` keys) — the launch assembly is never re-implemented in
  // QML, and no shell ever sees the path: it arrives as a single argv
  // token. The backend has already dropped the desktop field codes the
  // panel cannot fill; the fallback ladder for an older pairing is
  // launch_argv, then the bare path. execDetached fully detaches the
  // child, and the backend hands over with os.execve — the detached pid
  // BECOMES the app — so spawn success says nothing about the AppImage
  // runtime; the settle listing below still judges it.
  function launchArgvFor(item) {
    if (backendCommand.length > 0 && item.launchScaleArgv
        && item.launchScaleArgv.length > 0) {
      return backendCommand.concat(["--exec-with-scale"], item.launchScaleArgv)
    }
    if (item.launchArgv && item.launchArgv.length > 0) return item.launchArgv
    return [item.path]
  }

  function launchItem(item) {
    if (!item || item.path === "") return
    Quickshell.execDetached(launchArgvFor(item))
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

  // First click arms the inline confirmation, second click runs --remove
  // keyed on the desktop_id (the stable identifier — names can repeat when
  // two versions coexist, CONTRACT.md).
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

  // Run --integrate on the chosen path. Outcome handling per CONTRACT.md:
  // "integrated", "already-integrated" (exit 0, success with different
  // copy) and {"result":"error"}.
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
    // give it the long transfer budget. Local paths keep the fast extract
    // budget. Only the URL run streams progress documents (the backend's
    // emitter exists for URL targets only), so only it attaches the stdout
    // line reader: each complete line goes through
    // Model.noteIntegrateProgress, which ignores everything that is not a
    // progress document — the result document on the same stream still
    // reaches the outcome handling below through the full stdout text.
    var isUrl = /^https?:\/\//i.test(trimmed)
    var options = {
      timeoutMs: isUrl ? integrateUrlTimeoutMs : integrateTimeoutMs
    }
    if (isUrl) options.onStdoutLine = Model.noteIntegrateProgress
    callBackend(["--integrate", trimmed, "--yes", "--json"],
        options, function(result) {
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

  // Sweep every configured source. The offline error document arrives as
  // exit 1 + {"result":"error"} and reportFailure below surfaces its own
  // message ("Internet connection not available" and friends).
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

  // Delegating wrappers: the update / force-quit / update-all cluster lives
  // in PanelUpdates.qml.
  function requestUpdate(item) { return updatesCtl.requestUpdate(item) }
  function dismissForceQuit() { return updatesCtl.dismissForceQuit() }
  function confirmForceQuit() { return updatesCtl.confirmForceQuit() }
  function requestUpdateAll() { return updatesCtl.requestUpdateAll() }
  function dismissUpdateAll() { return updatesCtl.dismissUpdateAll() }
  function confirmUpdateAll() { return updatesCtl.confirmUpdateAll() }
  function pumpUpdateAll() { return updatesCtl.pumpUpdateAll() }
  function updateItem(item, killRunning) { return updatesCtl.updateItem(item, killRunning) }

  // Delegating wrappers: the background-job cluster lives in PanelJobs.qml.
  function jobActive(id) { return jobs.jobActive(id) }
  function refreshJobStates() { return jobs.refreshJobStates() }
  function queueJobRead(id) { return jobs.queueJobRead(id) }
  function pumpJobReads() { return jobs.pumpJobReads() }
  function idleParsed() { return jobs.idleParsed() }
  function adoptJob(id, parsed) { return jobs.adoptJob(id, parsed) }
  function surfaceJobOutcome(id, parsed, consumed) { return jobs.surfaceJobOutcome(id, parsed, consumed) }
  function jobProgressLabel(id) { return jobs.jobProgressLabel(id) }

  // ---- settings card ----------------------------------------------------------

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

  // Delegating wrappers: the per-app source editor lives in
  // PanelSourceEditor.qml.
  function openSourceEditor(item) { return sourceEditorCtl.openSourceEditor(item) }
  function closeSourceEditor() { return sourceEditorCtl.closeSourceEditor() }
  function rebuildSourceEditValues() { return sourceEditorCtl.rebuildSourceEditValues() }
  function setSourceValue(key, value) { return sourceEditorCtl.setSourceValue(key, value) }
  function saveSource() { return sourceEditorCtl.saveSource() }
  function unsetSource() { return sourceEditorCtl.unsetSource() }

  // Delegating wrappers: the picker/preview/drop cluster lives in
  // PanelIntegrate.qml.
  function openPicker() { return integrateCtl.openPicker() }
  function closePicker() { return integrateCtl.closePicker() }
  function clearPreview() { return integrateCtl.clearPreview() }
  function requestPreview(path) { return integrateCtl.requestPreview(path) }
  function confirmPreview() { return integrateCtl.confirmPreview() }
  function dismissPreview() { return integrateCtl.dismissPreview() }
  function previewAppLine() { return integrateCtl.previewAppLine() }
  function previewDestinationLine() { return integrateCtl.previewDestinationLine() }
  function browseForFile() { return integrateCtl.browseForFile() }
  function focusPickerField() { return integrateCtl.focusPickerField() }
  function dropBusy() { return integrateCtl.dropBusy() }
  function seedDropPreview(path, ignored) { return integrateCtl.seedDropPreview(path, ignored) }
  function handleDropUris(uris) { return integrateCtl.handleDropUris(uris) }
  function readDropPayload(payloadJson) { return integrateCtl.readDropPayload(payloadJson) }
  function tryConsumePendingDrop() { return integrateCtl.tryConsumePendingDrop() }

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
      stopPythonRecovery()
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
        // card outranks both: Esc cancels it, nothing runs. The
        // "Update all" card is modal in the same way: Esc dismisses it
        // and no job is spawned.
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
          CardHeader {
            id: cardHeader
            pickerOpen: root.pickerOpen
            busyList: root.busyList
            busyUpdates: root.busyUpdates
            pendingIds: root.pendingIds
            pinned: root.pinned
            subtitleText: root.subtitleText
            strings: root.strings
            foreground: root.foreground
            dim: root.dim
            fontFamily: root.fontFamily
            controlHeight: root.controlHeight
            onIntegrateClicked: {
              if (root.pickerOpen) root.closePicker()
              else root.integrateRequested()
            }
            onRefreshClicked: root.refresh()
            onCheckUpdatesClicked: root.checkUpdates()
            onUpdateAllClicked: root.requestUpdateAll()
            onPinToggled: root.pinned = !root.pinned
            onSettingsClicked: root.toggleSettings()
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

          // ---- Force-quit confirm card -----------------------------------
          ForceQuitCard {
            id: forceQuitCard
            forceQuitId: root.forceQuitId
            forceQuitName: root.forceQuitName
            forceQuitPids: root.forceQuitPids
            jobActive: root.jobActive(root.forceQuitId)
            strings: root.strings
            foreground: root.foreground
            dim: root.dim
            urgent: root.urgent
            fontFamily: root.fontFamily
            controlHeight: root.controlHeight
            onConfirmed: root.confirmForceQuit()
            onDismissed: root.dismissForceQuit()
          }

          // ---- "Update all" consent card ---------------------------------
          UpdateAllCard {
            id: updateAllCard
            updateAllOpen: root.updateAllOpen
            appsLine: root.updateAllSummary.appsLine
            processLine: root.updateAllSummary.processLine
            confirmEnabled: root.pendingIds.length > 0
            strings: root.strings
            foreground: root.foreground
            dim: root.dim
            urgent: root.urgent
            fontFamily: root.fontFamily
            controlHeight: root.controlHeight
            onConfirmed: root.confirmUpdateAll()
            onDismissed: root.dismissUpdateAll()
          }

          // ---- Python dependency card (authorized install) ----------------
          PythonDependencyCard {
            id: pythonCard
            pythonVisible: root.pythonStateKnown && root.pythonMissing
            installingPython: root.installingPython
            strings: root.strings
            foreground: root.foreground
            dim: root.dim
            fontFamily: root.fontFamily
            controlHeight: root.controlHeight
            onInstallRequested: root.installPython()
          }

          // ---- Integrate picker -------------------------------------------
          IntegratePicker {
            id: pickerCard
            pickerOpen: root.pickerOpen
            pickerPath: root.pickerPath
            busyPick: root.busyPick
            busyIntegrate: root.busyIntegrate
            busyPreview: root.busyPreview
            previewOpen: root.previewOpen
            previewDoc: root.previewDoc
            pickerBusy: root.pickerBusy
            previewAppLine: root.previewAppLine()
            previewDestinationLine: root.previewDestinationLine()
            strings: root.strings
            foreground: root.foreground
            dim: root.dim
            urgent: root.urgent
            fontFamily: root.fontFamily
            controlHeight: root.controlHeight
            homeDir: root.homeDir
            onPathEdited: function(v) {
              root.pickerPath = v
              // A preview computed for the old text is stale the moment
              // the path changes: drop it so Confirm can never install
              // anything other than what is on screen (same-trimmed edits
              // keep it).
              if (root.previewOpen && v.trim() !== root.previewInput) {
                root.dismissPreview()
              }
            }
            onPreviewRequested: root.requestPreview(root.pickerPath)
            onBrowseClicked: root.browseForFile()
            onGoClicked: root.previewOpen
              ? root.confirmPreview() : root.requestPreview(root.pickerPath)
            onQuickPicked: function(path) {
              root.pickerPath = path
              root.requestPreview(path)
            }
            onPreviewConfirmed: root.confirmPreview()
            onPreviewDismissed: root.dismissPreview()
            onFieldFocusChanged: function(delta) {
              root.textFieldFocusCount = delta > 0
                ? root.textFieldFocusCount + delta
                : Math.max(0, root.textFieldFocusCount + delta)
            }
          }

          // ---- Settings card (cadence only — per-app sources are on each row) ----
          SettingsCard {
            id: settingsCard
            settingsOpen: root.settingsOpen
            settingsBusy: root.settingsBusy
            editEnabled: root.editEnabled
            strings: root.strings
            foreground: root.foreground
            dim: root.dim
            fontFamily: root.fontFamily
            controlHeight: root.controlHeight
            onEnableToggled: root.editEnabled = !root.editEnabled
            onSaveRequested: root.saveSettings()
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

                  AppRow {
                    // Explicit assignment, never a `required` property:
                    // this Qt does not auto-initialize required delegate
                    // properties, so every row fails to instantiate
                    // without it.
                    modelData: root.items[index]
                    jobActive: root.jobActive(modelData.id)
                    progressLabel: root.jobProgressLabel(modelData.id)
                    confirmRemoveId: root.confirmRemoveId
                    busyRemove: root.busyRemove
                    sourceEditId: root.sourceEditId
                    sourceEditName: root.sourceEditName
                    sourceEditManager: root.sourceEditManager
                    sourceEditValues: root.sourceEditValues
                    sourceBusy: root.sourceBusy
                    managerOptions: root.managerOptions
                    strings: root.strings
                    foreground: root.foreground
                    dim: root.dim
                    urgent: root.urgent
                    fontFamily: root.fontFamily
                    controlHeight: root.controlHeight
                    onLaunchRequested: root.launchItem(modelData)
                    onUpdateRequested: root.requestUpdate(modelData)
                    onRemoveRequested: root.requestRemove(modelData)
                    onSourceEditRequested: root.openSourceEditor(modelData)
                    onSourceSaveRequested: root.saveSource()
                    onSourceUnsetRequested: root.unsetSource()
                    onSourceValueChanged: function(key, value) {
                      root.setSourceValue(key, value)
                    }
                    onManagerSelected: function(value) {
                      root.sourceEditManager = value
                      root.rebuildSourceEditValues()
                    }
                    onPopupOpenChanged: function(open) {
                      root.sourceDropdownOpen = open
                    }
                    onFieldFocusChanged: function(delta) {
                      root.textFieldFocusCount = delta > 0
                        ? root.textFieldFocusCount + delta
                        : Math.max(0, root.textFieldFocusCount + delta)
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    // ---- drop-to-integrate: the whole window is a drop target ----------
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

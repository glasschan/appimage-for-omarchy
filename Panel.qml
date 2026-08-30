import QtQuick
import Qt.labs.folderlistmodel
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend

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
// through the shared Process in this file — the long --update runs on its
// own Process pair so it can never stomp a short call in flight —
// F1 list (cache-first, the
// store renders instantly and refreshes in the background), F2 integrate
// (inline picker: Quickshell ships no file dialog, so a path field plus
// Downloads quick-picks stands in), F3 remove (two-click inline confirm,
// keyed on desktop_id), F4 launch (detached exec of the AppImage path),
// F5 bar badge (observer over the same Model store).
//
// M2 wiring: F6 check updates (--list-updates with a long watchdog; the
  // offline error document surfaces its own message), F7 one-click update
  // per row (--update --yes, 10-minute timeout for large downloads), F9
  // per-row update-source editors plus the F8 cadence knobs in a settings
  // card toggled by the header gear. The F8 background service itself
  // lives in Service.qml — this panel only edits what it runs on.
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
  // Update sweeps walk every configured release source over the network.
  readonly property int checkTimeoutMs: 120000
  // F7 downloads can be a full AppImage; the backend replaces the file in
  // place, so the watchdog must outlast the transfer, not the UI.
  readonly property int updateTimeoutMs: 600000
  // How long the two-click remove confirmation stays armed.
  readonly property int confirmResetMs: 4000
  // Grace before the post-launch refresh so `ps` can see the new process.
  readonly property int launchSettleMs: 1500
  // Status notices fade on their own after this long (matching the
  // backend's desktop-notification expire time); errors never auto-hide.
  readonly property int statusTimeoutMs: 5000

  // Current Backend.run context (null while idle); the Process and Timer
  // signal handlers feed it back into lib/Backend.js.
  property var runCtx: null

  // Dedicated Backend.run context for the long --update (null while idle):
  // a 10-minute download must not occupy the shared short-call Process —
  // mirror of Service.qml's fetchProc/settingsProc split.
  property var updateCtx: null

  // Mirrored from the shared Model store (no property notifications in a
  // JS library, so a subscription copies state into reactive properties).
  property var items: []
  property bool busyList: false
  property bool busyIntegrate: false
  property bool busyRemove: false
  property bool busyUpdates: false
  // desktop_id of the in-flight --update ("" when idle) — rows compare it
  // to dim every Update button and spin the updating one.
  property string updatingId: ""
  property string lastError: ""
  property string statusText: ""
  property var unsubscribe: null

  // Integrate picker (F2): Quickshell has no FileDialog type (checked
  // against /usr/lib/qt6/qml/Quickshell — no dialog types ship with it),
  // so the picker is a path field plus AppImages found in ~/Downloads and
  // ~/downloads.
  property bool pickerOpen: false
  property string pickerPath: ""
  property var pickerFiles: []

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
  // Focus count over the editor's dynamic text fields; PanelKeyCatcher
  // must hand raw keys to whichever field is typing (see formEditing).
  property int sourceFieldFocusCount: 0
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
  // derived from the manifest's source dir. Nothing is hardcoded to an
  // absolute path.
  readonly property var backendCommand: {
    var configured = Model.state.backendCommand
    if (configured && configured.length > 0) return configured
    var sourceDir = manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : ""
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
    busyList = Model.state.busy.list === true
    busyIntegrate = Model.state.busy.integrate === true
    busyRemove = Model.state.busy.remove === true
    busyUpdates = Model.state.busy.updates === true
    updatingId = String(Model.state.busy.update || "")
    lastError = Model.state.lastError
    statusText = Model.state.status
  }

  // True while any editor inside the panel should own raw keys instead of
  // the key catcher's shortcuts (see the keyCatcher binding below).
  readonly property bool formEditing: pickerField.activeFocus
    || sourceFieldFocusCount > 0
    || root.sourceDropdownOpen
    || (intervalField.field.contentItem
        ? intervalField.field.contentItem.activeFocus : false)
    || (delayField.field.contentItem
        ? delayField.field.contentItem.activeFocus : false)

  // ---- lifecycle -----------------------------------------------------------

  function open(payloadJson) {
    Model.setMockMode(false)
    if (backendCommand.length > 0) Model.configureBackend(backendCommand)
    syncFromModel()
    // Cache-first: whatever the store holds renders now; a backend rescan
    // only fires when the cache is empty or stale (never blocks the UI —
    // the Process is asynchronous and the watchdog bounds it).
    if (Model.state.items.length === 0
        || Date.now() - Model.state.lastSyncMs > staleAfterMs) {
      refresh()
    }
    controller.show()
    Qt.callLater(function() {
      if (root.opened && keyCatcher) keyCatcher.forceActiveFocus()
    })
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
      onDone({ ok: false, exitCode: -1, stdout: "", stderr: "", json: null,
               timedOut: false, spawnFailed: false, error: "busy" })
      return
    }
    var ctx = Backend.run(backendProc, backendTimer, backendCommand, args, options || {}, onDone)
    runCtx = ctx && ctx.active ? ctx : null
  }

  // Map a finished Backend.run result onto the store's error/status state.
  // Covers: spawn failure + timeout (python3 missing / hung backend),
  // non-JSON stdout, and the backend's own {"result":"error"} documents.
  function reportFailure(result, what) {
    if (result.timedOut || result.spawnFailed || result.error === "no-backend-command") {
      Model.setError(strings.backendMissing)
      return
    }
    // A genuine error document (exit != 0) beats stderr for copy quality;
    // garbage stdout parses to null and falls through to the stderr line.
    var doc = Backend.parseJson(result.stdout)
    if (doc && doc.result === "error" && doc.error) {
      Model.setError(String(doc.error))
      return
    }
    if (result.stderr !== "" || result.stdout !== "") {
      Model.setError(strings.backendFailed + ": "
        + Backend.elide(result.stderr || result.stdout))
      return
    }
    Model.setError(strings.backendFailed + " (" + what + ")")
  }

  function refresh() {
    if (Model.state.busy.list) return
    if (Model.state.mockMode) return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    Model.setBusy("list", true)
    syncFromModel()
    callBackend(["--list-installed", "--json"], null, function(result) {
      Model.setBusy("list", false)
      if (result.ok) {
        var parsed = Model.parseListJson(result.stdout)
        if (parsed.ok) {
          Model.setItems(parsed.items)
          Model.clearError()
        } else {
          Model.setError(parsed.error)
        }
      } else {
        reportFailure(result, "list")
      }
      syncFromModel()
    })
  }

  // F4: launch the AppImage binary detached. The argv form keeps the path
  // (which came from the backend's JSON) a single literal argument — no
  // shell ever sees it. DESKTOPINTEGRATION=1 matches the generated desktop
  // entry's Exec line so AppImages that bundle their own integration
  // prompt stay quiet.
  function launchItem(item) {
    if (!item || item.path === "") return
    Quickshell.execDetached(["env", "DESKTOPINTEGRATION=1", item.path])
    Model.markRunning(item.id, true)
    launchSettleTimer.restart()
  }

  // F3: first click arms the inline confirmation, second click runs
  // --remove keyed on the desktop_id (the stable identifier — names can
  // repeat when two versions coexist, CONTRACT.md).
  function requestRemove(item) {
    if (!item) return
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
          reportFailure(result, "remove")
        }
      } else {
        reportFailure(result, "remove")
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
    callBackend(["--integrate", trimmed, "--yes", "--json"],
        { timeoutMs: integrateTimeoutMs }, function(result) {
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
      pickerField.text = ""
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
    if (!Model.claimUpdateCheck()) return
    Model.clearError()
    Model.setStatus(strings.checkingUpdates)
    syncFromModel()
    callBackend(["--list-updates", "--json"], { timeoutMs: checkTimeoutMs }, function(result) {
      Model.releaseUpdateCheck()
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

  // F7: run --update in place. No confirm — the backend replaces the
  // AppImage where it sits and refuses when the app is running
  // (result "skipped-running", shown as status, not error).
  function updateItem(item) {
    if (!item || item.id === "") return
    if (Model.state.busy.update !== "") return
    if (backendCommand.length === 0) {
      Model.setError(strings.backendMissing)
      syncFromModel()
      return
    }
    Model.clearError()
    Model.setUpdatingId(item.id)
    Model.setStatus(Model.formatCount(item.name, strings.updating))
    syncFromModel()
    // Dedicated Process/Timer pair: the transfer runs for up to ten
    // minutes, so it must not occupy backendProc — refresh/settings calls
    // stay possible meanwhile (callBackend's guard fails them cleanly
    // rather than stomping this context). Two concurrent --update calls
    // stay impossible through the setUpdatingId guard above.
    var ctx = Backend.run(updateProc, updateTimer, backendCommand,
        ["--update", item.id, "--yes", "--json"],
        { timeoutMs: updateTimeoutMs }, function(result) {
      Model.setUpdatingId("")
      if (result.ok) {
        var action = Model.parseActionJson(result.stdout)
        if (action.ok) {
          if (action.result === "updated") {
            var version = action.app && action.app.updateVersion !== ""
              ? action.app.updateVersion : item.updateVersion
            // A version the backend could not resolve falls back to the
            // action's own message — never "Updated to " with an empty %1.
            Model.setStatus(version !== ""
              ? Model.formatCount(version, strings.updated)
              : action.message)
          } else if (action.message !== "") {
            // "up-to-date" / "skipped-running" carry the user-facing copy.
            Model.setStatus(action.message)
          } else {
            Model.setStatus(action.result === "up-to-date"
              ? strings.upToDate : strings.skippedRunning)
          }
          Model.clearError()
        } else {
          Model.setError(action.error !== "" ? action.error : strings.backendFailed)
        }
      } else {
        reportFailure(result, "update")
      }
      syncFromModel()
      // The AppImage (and its desktop entry's version) just changed:
      // re-list so names, versions and manager columns stay truthful.
      refresh()
    })
    updateCtx = ctx && ctx.active ? ctx : null
  }

  // ---- settings card (F8 cadence) -------------------------------------------

  function toggleSettings() {
    if (settingsOpen) {
      settingsOpen = false
      closeSourceEditor()
      return
    }
    pickerOpen = false
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
    sourceFieldFocusCount = 0
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
    sourceFieldFocusCount = 0
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
    pickerOpen = true
    rebuildPickerFiles()
  }

  function closePicker() {
    pickerOpen = false
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

  Component.onCompleted: {
    syncFromModel()
    unsubscribe = Model.subscribe(syncFromModel)
  }

  Component.onDestruction: {
    if (unsubscribe) unsubscribe()
  }

  Process {
    id: backendProc
    command: []
    stdout: StdioCollector {
      id: backendStdout
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: backendStderr
      waitForEnd: true
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

  // Long-op pair for --update (F7), kept separate from backendProc so a
  // download never fights the short calls for one Process (same split as
  // Service.qml's fetchProc/settingsProc).
  Process {
    id: updateProc
    command: []
    stdout: StdioCollector {
      id: updateStdout
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: updateStderr
      waitForEnd: true
    }
    onStarted: Backend.markStarted(root.updateCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.updateCtx, exitCode, updateStdout.text, updateStderr.text)
    }
  }

  Timer {
    id: updateTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.updateCtx)
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
  // operation is in flight and resumes after; refresh()'s own busy guard
  // makes a racing trigger a no-op anyway.
  Timer {
    id: runningPollTimer
    interval: 10000
    repeat: true
    running: root.opened && !root.busyList && !root.busyIntegrate && !root.busyRemove
      && !root.busyUpdates && root.updatingId === "" && !root.settingsBusy && !root.sourceBusy
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
        onCloseRequested: root.requestClose()
        onTextKey: function(t) {
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

                TextField {
                  id: pickerField
                  placeholderText: root.strings.pickerPlaceholder
                  foreground: root.foreground
                  accent: Color.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  implicitHeight: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2)
                  enabled: !root.busyIntegrate
                  width: parent.width - pickerGoButton.width - Style.spacing.xs
                  onTextChanged: root.pickerPath = text
                  onAccepted: root.integratePath(text)
                }

                Button {
                  id: pickerGoButton
                  text: root.busyIntegrate ? root.strings.integrateWorking : root.strings.pickerGo
                  enabled: !root.busyIntegrate && root.pickerPath.trim() !== ""
                  anchors.verticalCenter: parent.verticalCenter
                  foreground: root.foreground
                  accent: Color.accent
                  fontFamily: root.fontFamily
                  fontSize: Style.font.bodySmall
                  implicitHeight: root.controlHeight
                  onClicked: root.integratePath(root.pickerPath)
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
                      enabled: !root.busyIntegrate
                      onClicked: root.integratePath(modelData.path)
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

              Row {
                width: parent.width
                spacing: Style.spacing.sm

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
                  onModified: function(v) { root.editDelay = v }
                }
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
          Item {
            id: body
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

                          // Extracted icon when it exists (try png → svg →
                          // xpm next to the AppImage under .icons/, CONTRACT.md
                          // filesystem layout), tabler cube-unfolded icon otherwise.
                          Image {
                            id: appIcon
                            property int iconIndex: 0
                            readonly property var iconCandidates: {
                              var list = []
                              var base = rowOuter.modelData.iconBase
                              if (base) {
                                var exts = [".png", ".svg", ".xpm"]
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

                            Row {
                              spacing: Style.spacing.xxs

                              Text {
                                visible: rowOuter.modelData.version !== ""
                                text: rowOuter.modelData.version
                                color: root.dim
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.caption
                              }

                              Text {
                                visible: rowOuter.modelData.running
                                text: "● " + root.strings.runningLabel
                                color: Color.accent
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.caption
                              }

                              // F6: pending release, accent like the running dot.
                              Text {
                                visible: rowOuter.modelData.updateVersion !== ""
                                text: Model.formatCount(rowOuter.modelData.updateVersion, root.strings.updateAvailable)
                                color: Color.accent
                                font.family: root.fontFamily
                                font.pixelSize: Style.font.caption
                              }
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
                            // Single flight: every button dims
                            // while one download runs, and the row being
                            // updated loops its arrow upward.
                            PanelActionButton {
                              id: updateButton
                              visible: rowOuter.modelData.updateVersion !== ""
                              enabled: root.updatingId === ""
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
                                  running: root.updatingId === rowOuter.modelData.id
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

                              onClicked: root.updateItem(rowOuter.modelData)
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
                            Button {
                              visible: root.confirmRemoveId === rowOuter.modelData.id
                              text: root.strings.removeConfirm
                              anchors.verticalCenter: parent.verticalCenter
                              foreground: root.urgent
                              accent: root.urgent
                              fontFamily: root.fontFamily
                              fontSize: Style.font.caption
                              implicitHeight: root.controlHeight
                              enabled: !root.busyRemove
                              onClicked: root.requestRemove(rowOuter.modelData)
                            }

                            PanelActionButton {
                              id: removeButton
                              visible: root.confirmRemoveId !== rowOuter.modelData.id
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
                          model: {
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
  }

  // Source editor field templates (F9): one Loader per MANAGERS field in
  // the editor above, keyed by `fieldKey` into root.sourceEditValues.
  // Bool values are "true"/"false" strings so the Save argv passes them
  // through verbatim; the focus count tells the key catcher when a field
  // is typing (see formEditing).
  Component {
    id: sourceStringFieldComp

    TextField {
      property string fieldKey: ""
      placeholderText: Model.fieldLabel(fieldKey)
      foreground: root.foreground
      accent: Color.accent
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      implicitHeight: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2)
      width: parent ? parent.width : 0
      enabled: !root.sourceBusy
      text: root.sourceEditValues[fieldKey] || ""
      onTextChanged: root.setSourceValue(fieldKey, text)
      onActiveFocusChanged: root.sourceFieldFocusCount = activeFocus ? root.sourceFieldFocusCount + 1 : Math.max(0, root.sourceFieldFocusCount - 1)
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

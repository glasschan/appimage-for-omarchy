import QtQuick
import Quickshell
import Quickshell.Io
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend

// Background update checker (F8).
//
// The plugin declares the "service" kind, so the shell instantiates this
// document once at startup (shell.qml ensureService injects shell, manifest
// and omarchyPath when the properties exist) and destroys it when the
// plugin is disabled — nothing here is per-monitor.
//
// Scheduling: read --settings once, then a single repeat:false Timer arms
// the first --fetch-updates after the configured login delay and each next
// check one interval after the previous one finished. Every fetch
// completion rearms exactly once (see fetch()); a tick that fires while a
// sweep is still running repaths on the short floor instead of dying, so
// the service can never stall. Re-reading settings rides on
// Model.settingsRevision (the store subscription is the only notification
// channel a JS library has) so a panel-side save reschedules the running
// timer. When checks are disabled the timer is stopped: idle cost is zero
// — no polling Timers, no running Processes between checks.
//
// Results go through Model.applyUpdates so the bar badge updates
// engine-wide. A sweep reporting an app the store never saw (integrated
// after the startup sync ran) triggers one --list-installed self-heal so
// the badge counts it without anyone opening the panel. Failures are
// silent (console.warn): the panel owns user-facing errors, and the
// backend already sent the desktop notification itself when the sweep
// found something — QML never notifies.
Item {
  id: root

  // Injected by the shell's service loader.
  property var shell: null
  property var manifest: null
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")

  readonly property string homeDir: Quickshell.env("HOME") || ""
  readonly property string moduleName: manifest && manifest.id
    ? String(manifest.id) : "io.github.glasschan.appimage"

  // argv for the backend CLI — the same derivation as Panel.qml but
  // self-contained: the service loads before any panel, so it falls back
  // to the canonical install location like BarWidget.qml does.
  readonly property var backendCommand: {
    var configured = Model.state.backendCommand
    if (configured && configured.length > 0) return configured
    var sourceDir = manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : ""
    if (sourceDir !== "") return ["python3", sourceDir + "/backend/main.py"]
    return ["python3", homeDir + "/.config/omarchy/plugins/" + moduleName + "/backend/main.py"]
  }

  // Update sweeps download release metadata at least; give them the same
  // room the panel's check gets.
  readonly property int fetchTimeoutMs: 120000
  // Floor for any scheduled delay: delayMinutes 0 means "check as soon as
  // the shell has settled", not "spawn python during shell startup".
  readonly property int minScheduleMs: 60000

  // In-flight Backend.run contexts (null while idle); the Process and Timer
  // handlers below feed them back into lib/Backend.js. One Process/Timer
  // pair each for the sweep, the startup settings read and the badge
  // self-heal list, so concurrent backend calls can never stomp each
  // other's context.
  property var fetchCtx: null
  property var settingsCtx: null
  property var listCtx: null

  // False until the first --settings answer (or its failure) lands; guards
  // the revision subscription so the startup write doesn't double-schedule.
  property bool settingsLoaded: false
  property int seenSettingsRevision: -1
  property var unsubscribe: null

  function syncFromModel() {
    var revision = Model.state.settingsRevision
    if (revision === root.seenSettingsRevision) return
    root.seenSettingsRevision = revision
    if (root.settingsLoaded) root.reschedule(false)
  }

  // (Re)arm the single check timer: `first` waits the login delay, every
  // later arm waits one interval. Disabled settings stop the timer.
  function reschedule(first) {
    checkTimer.stop()
    if (!Model.state.settings.enabled) return
    var minutes = first
      ? Model.state.settings.delayMinutes
      : Model.state.settings.intervalMinutes
    checkTimer.interval = Math.max(root.minScheduleMs, Math.round(minutes * 60000))
    checkTimer.restart()
  }

  // The single tail for every completed fetch: arm the next check one
  // interval out (disabled settings stop the timer instead).
  function scheduleNext() {
    reschedule(false)
  }

  // Short-floor repath for a tick that could not run: the previous sweep
  // is still in flight, or a panel-initiated sweep owns the single flight.
  // This check is delayed, not lost — the normal interval resumes from the
  // next completion's scheduleNext().
  function scheduleRetry() {
    checkTimer.stop()
    checkTimer.interval = root.minScheduleMs
    checkTimer.restart()
  }

  // One-shot settings read at startup. On failure the store's tolerant
  // defaults (360/5/on) stand and we still schedule — a dev checkout
  // without an installed backend must stay quiet, same as the bar.
  function loadSettings() {
    var ctx = Backend.run(settingsProc, settingsTimer, backendCommand,
      ["--settings", "--json"], {}, function(result) {
        settingsCtx = null
        if (result.ok) {
          var parsed = Model.parseSettingsJson(result.stdout)
          if (parsed.ok) {
            // Revision bump routes through syncFromModel (which skips
            // scheduling while settingsLoaded is still false); then the
            // first check arms with the login delay, not the interval.
            Model.applySettings(parsed.settings)
            settingsLoaded = true
            reschedule(true)
            return
          }
        }
        console.warn("appimage service: settings load failed, using defaults",
          result.error, Backend.elide(result.stderr || result.stdout))
        settingsLoaded = true
        reschedule(true)
      })
    settingsCtx = ctx && ctx.active ? ctx : null
  }

  // F8 sweep. --fetch-updates exits 0 even offline (offline:true, empty
  // updates); an offline answer must NOT clear the badge, so only a real
  // online result reaches applyUpdates. Never notifies: the backend sent
  // the desktop notification (with its own dedup) before this returns.
  //
  // INVARIANT: every fetch completion rearms exactly once. fetch() ends in
  // exactly one scheduleRetry() (tick could not run), or it spawns one run
  // whose onDone ends in exactly one scheduleNext() — lib/Backend.js fires
  // onDone exactly once per run (exit, watchdog timeout, or its synchronous
  // no-backend-command failure) and checkTimer is a single one-shot Timer,
  // so restart() can never stack two pending shots. No path skips the
  // rearm: one that did would silently kill the service, because nothing
  // else arms checkTimer — a settings-save reschedule only re-times the
  // pending shot.
  function fetch() {
    if (fetchCtx || !Model.claimUpdateCheck()) {
      // Either our own sweep is still running (a settings save re-timed
      // checkTimer mid-flight) or a panel-initiated sweep owns the flight.
      scheduleRetry()
      return
    }
    var ctx = Backend.run(fetchProc, fetchTimer, backendCommand,
      ["--fetch-updates", "--json"], { timeoutMs: fetchTimeoutMs }, function(result) {
        fetchCtx = null
        Model.releaseUpdateCheck()
        if (result.ok) {
          var parsed = Model.parseUpdatesJson(result.stdout)
          // An offline empty list says nothing about newly integrated
          // apps, so the badge self-heal runs on real online results only.
          if (parsed.ok && !parsed.offline) {
            Model.applyUpdates(parsed.updates)
            selfHealBadge(parsed.updates)
          }
        } else {
          console.warn("appimage service: fetch-updates failed:",
            result.error, Backend.elide(result.stderr || result.stdout))
        }
        scheduleNext()
      })
    // A synchronous onDone (no-backend-command) has already rearmed by the
    // time this line runs; ctx.active is false then, fetchCtx stays null.
    fetchCtx = ctx && ctx.active ? ctx : null
  }

  // Badge self-heal: applyUpdates only merges into ids the store already
  // holds, and that list comes from the startup sync — an app integrated
  // after the shell started stayed badge-blind until someone opened the
  // panel. When a sweep reports an id the store lacks, one short-lived
  // --list-installed refreshes the list and the sweep's items are
  // re-applied so the new app's pending update is counted (at most one
  // extra process, and only when new apps actually appeared). The heal owns
  // no timer: the fetch's scheduleNext() already covered the rearm.
  function selfHealBadge(updates) {
    var unknown = false
    for (var i = 0; i < updates.length; i++) {
      if (!Model.findItem(updates[i].id)) { unknown = true; break }
    }
    if (!unknown) return
    if (listCtx) return                     // a heal is in flight; the next sweep retries
    var ctx = Backend.run(listProc, listTimer, backendCommand,
      ["--list-installed", "--json"], {}, function(result) {
        listCtx = null
        if (result.ok) {
          var parsed = Model.parseListJson(result.stdout)
          if (parsed.ok) {
            Model.setItems(parsed.items)
            Model.applyUpdates(updates)
          } else {
            console.warn("appimage service: badge heal list unparsable:", parsed.error)
          }
        } else {
          console.warn("appimage service: badge heal list failed:",
            result.error, Backend.elide(result.stderr || result.stdout))
        }
      })
    listCtx = ctx && ctx.active ? ctx : null
  }

  Component.onCompleted: {
    syncFromModel()
    unsubscribe = Model.subscribe(syncFromModel)
    loadSettings()
  }

  Component.onDestruction: {
    if (unsubscribe) unsubscribe()
    checkTimer.stop()
  }

  Process {
    id: fetchProc
    command: []
    stdout: StdioCollector {
      id: fetchStdout
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: fetchStderr
      waitForEnd: true
    }
    onStarted: Backend.markStarted(root.fetchCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.fetchCtx, exitCode, fetchStdout.text, fetchStderr.text)
    }
  }

  Timer {
    id: fetchTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.fetchCtx)
  }

  Process {
    id: settingsProc
    command: []
    stdout: StdioCollector {
      id: settingsStdout
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: settingsStderr
      waitForEnd: true
    }
    onStarted: Backend.markStarted(root.settingsCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.settingsCtx, exitCode, settingsStdout.text, settingsStderr.text)
    }
  }

  Timer {
    id: settingsTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.settingsCtx)
  }

  // Short-call pair for the badge self-heal's --list-installed (same
  // wiring as settingsProc; a heal can overlap a fetch's own completion).
  Process {
    id: listProc
    command: []
    stdout: StdioCollector {
      id: listStdout
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: listStderr
      waitForEnd: true
    }
    onStarted: Backend.markStarted(root.listCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.listCtx, exitCode, listStdout.text, listStderr.text)
    }
  }

  Timer {
    id: listTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.listCtx)
  }

  Timer {
    id: checkTimer
    repeat: false
    onTriggered: root.fetch()
  }
}

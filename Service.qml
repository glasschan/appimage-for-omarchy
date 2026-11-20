import QtQuick
import Quickshell
import Quickshell.Io
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend
import "lib/JobDocs.js" as JobDocs

// Background update checker. The plugin declares the "service" kind, so the
// shell instantiates this once at startup and destroys it when the plugin is
// disabled; nothing here is per-monitor.
//
// A single repeat:false Timer arms each --fetch-updates, and every fetch
// completion rearms exactly once (see fetch()). Results go through
// Model.applyUpdates so the bar badge updates engine-wide. Failures are
// silent: the panel owns user-facing errors.
//
// Job adoption (lib/JobDocs.js) runs after the next check is armed, so it can
// never delay the sweep.
Item {
  id: root

  // Injected by the shell's service loader.
  property var shell: null
  property var manifest: null
  property string omarchyPath: Quickshell.env("OMARCHY_PATH")

  readonly property string homeDir: Quickshell.env("HOME") || ""
  readonly property string moduleName: manifest && manifest.id
    ? String(manifest.id) : "io.github.glasschan.appimage"

  // argv for the backend CLI: the service loads before any panel, so it
  // falls back to the canonical install location like BarWidget.qml does.
  readonly property var backendCommand: {
    var configured = Model.state.backendCommand
    if (configured && configured.length > 0) return configured
    var sourceDir = manifest && manifest.__sourceDir ? String(manifest.__sourceDir) : ""
    if (sourceDir !== "") return ["python3", sourceDir + "/backend/main.py"]
    return ["python3", homeDir + "/.config/omarchy/plugins/" + moduleName + "/backend/main.py"]
  }

  // Update sweeps download release metadata; give them the same room the
  // panel's check gets.
  readonly property int fetchTimeoutMs: 120000
  // Floor for any scheduled delay: 0 minutes means "check soon", not "spawn
  // python during shell startup".
  readonly property int minScheduleMs: 60000

  // In-flight Backend.run contexts (null while idle), fed back by the Process
  // and Timer handlers below. One pair each so concurrent backend calls can
  // never stomp each other's context.
  property var fetchCtx: null
  property var settingsCtx: null
  property var listCtx: null

  // False until the first --settings answer lands; guards the revision
  // subscription so the startup write doesn't double-schedule.
  property bool settingsLoaded: false
  property int seenSettingsRevision: -1
  property var unsubscribe: null

  // Background job adoption. jobsDir mirrors the backend's
  // plugin_state_dir()/jobs layout. jobSurfaced maps desktop id to the
  // state@job-cycle already surfaced this shell session (see
  // surfaceJobDecision); notifyQueue carries the bounded notify-send backlog.
  property var notifyCtx: null
  property var notifyQueue: []
  property var jobSurfaced: ({})
  readonly property string jobsDir:
    (Quickshell.env("XDG_STATE_HOME") || homeDir + "/.local/state")
    + "/" + moduleName + "/jobs"

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

  // The single tail for every completed fetch.
  function scheduleNext() {
    reschedule(false)
  }

  // Short-floor repath for a tick that could not run because the previous
  // sweep is still in flight: the check is delayed, not lost.
  function scheduleRetry() {
    checkTimer.stop()
    checkTimer.interval = root.minScheduleMs
    checkTimer.restart()
  }

  // One-shot settings read at startup. On failure the store's tolerant
  // defaults stand and we still schedule, so a dev checkout without an
  // installed backend stays quiet.
  function loadSettings() {
    var ctx = Backend.run(settingsProc, settingsTimer, backendCommand,
      ["--settings", "--json"], {}, function(result) {
        settingsCtx = null
        if (result.ok) {
          var parsed = Model.parseSettingsJson(result.stdout)
          if (parsed.ok) {
            // The revision bump routes through syncFromModel; settingsLoaded
            // stays false until now so the first check gets the login delay.
            Model.applySettings(parsed.settings)
            settingsLoaded = true
            reschedule(true)
            checkJobs()
            return
          }
        }
        console.warn("appimage service: settings load failed, using defaults",
          result.error, Backend.elide(result.stderr || result.stdout))
        settingsLoaded = true
        reschedule(true)
        checkJobs()
      })
    settingsCtx = ctx && ctx.active ? ctx : null
  }

  // --fetch-updates exits 0 even offline (offline:true, empty updates); an
  // offline answer must NOT clear the badge, so only a real online result
  // reaches applyUpdates. Never notifies: the backend sent the desktop
  // notification itself before this returns.
  //
  // INVARIANT: every fetch completion rearms exactly once. fetch() ends in
  // exactly one scheduleRetry() (tick could not run), or it spawns one run
  // whose onDone ends in exactly one scheduleNext(). Nothing else arms
  // checkTimer, so a path that skipped the rearm would silently kill the
  // service.
  function fetch() {
    if (fetchCtx || !Model.claimUpdateCheck("service")) {
      // Our own sweep is still running or a panel-initiated sweep owns the
      // flight.
      scheduleRetry()
      return
    }
    var ctx = Backend.run(fetchProc, fetchTimer, backendCommand,
      ["--fetch-updates", "--json"], { timeoutMs: fetchTimeoutMs }, function(result) {
        fetchCtx = null
        Model.releaseUpdateCheck("service")
        if (result.ok) {
          var parsed = Model.parseUpdatesJson(result.stdout)
          // An offline empty list says nothing about newly integrated apps,
          // so the self-heal runs on real online results only.
          if (parsed.ok && !parsed.offline) {
            Model.applyUpdates(parsed.updates)
            selfHealBadge(parsed.updates)
          }
        } else {
          console.warn("appimage service: fetch-updates failed:",
            result.error, Backend.elide(result.stderr || result.stdout))
        }
        scheduleNext()
        // Adoption runs strictly after the rearm, so it can never delay the
        // sweep cadence.
        checkJobs()
      })
    // A synchronous onDone (no-backend-command) has already rearmed; ctx.active
    // is false then, fetchCtx stays null.
    fetchCtx = ctx && ctx.active ? ctx : null
  }

  // Badge self-heal: applyUpdates only merges into ids the store already
  // holds from the startup sync, so an app integrated after the shell started
  // stayed badge-blind until someone opened the panel. One --list-installed
  // refresh fixes that. The heal owns no timer; the fetch already rearmed.
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
            // The runtime verdict (fuse2 probe) rides every listing.
            Model.setItems(parsed.items, parsed.runtime)
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

  // ---- background job adoption ---------------------------------------------
  //
  // Reads the background update-job documents (backend/CONTRACT.md, writer
  // backend/omarchy_appimage/jobs.py, parser lib/JobDocs.js) and surfaces
  // their finished outcomes once per job cycle per shell session. Called only
  // from the startup settings read and the end of a completed sweep, never
  // from its own timer.
  //
  // No directory listing: Quickshell 0.3's Io module has no Dir type. The
  // candidate set is Model.state.items, so paths derive from backend-provided
  // ids, never raw directory entries.
  function checkJobs() {
    // With auto-checks disabled the service stays completely idle.
    if (!Model.state.settings.enabled) return
    var items = Model.state.items
    if (!items || items.length === 0) return
    for (var i = 0; i < items.length; i++) {
      try {
        adoptJobDocument(String((items[i] && items[i].id) || ""))
      } catch (e) {
        // The helpers below never throw; this is insurance only.
        console.warn("appimage service: job adoption skipped:", e)
      }
    }
  }

  // One candidate app: validate the id, read its document, adopt it.
  function adoptJobDocument(desktopId) {
    // Job documents exist only for real desktop ids (the file stem is the id
    // minus ".desktop"); a null id falls back to the AppImage path and can
    // never have a document.
    if (!/\.desktop$/.test(desktopId)) return
    // Shared stem rule (lib/JobDocs.js jobDocumentStem, mirror of jobs.py
    // _job_stem): the path is always one flat component inside the jobs dir,
    // never a traversal.
    var stem = JobDocs.jobDocumentStem(desktopId)
    if (stem === "") return
    jobView.path = ""       // force a re-read even when the previous
    jobView.path = jobsDir + "/" + stem + ".json"  // candidate had this path
    // Inline completion, same contract as PanelJobs.pumpJobReads: without
    // waitForJob() a missing document's async load never completes on this
    // Quickshell, and blockLoading hands back the PREVIOUS file's content.
    jobView.waitForJob()
    var text = String(jobView.text() || "")
    // `null`, not `true`: QML cannot probe a pid, so pass the "caller cannot
    // probe" verdict and let the heartbeat rule decide; `true` would make
    // pidDead unreachable.
    var adopted = JobDocs.adoptDocument(text, null, Date.now())
    // Missing, aged out or corrupt: skip silently like the rest of the
    // service's failures.
    if (adopted.parseError !== "") return
    // Act only on a document that belongs to the app we meant to read; the
    // parse layer is tolerant, the id check is ours.
    if (String(adopted.decision.desktopId) !== desktopId) return
    surfaceJobDecision(adopted.decision, desktopId)
  }

  // Notify once per job cycle per shell session: jobSurfaced[desktopId] holds
  // the outcome state plus the job cycle (state@started) already surfaced. A
  // different cycle is news again, and an active document clears the record
  // so a retry's outcome surfaces again.
  function surfaceJobDecision(decision, desktopId) {
    var state = decision.state
    if (state === "active") {
      delete jobSurfaced[desktopId]   // a new cycle began; its outcome is news again
      return
    }
    if (state !== "done" && state !== "error" && state !== "interrupted") return
    if (jobSurfaced[desktopId] === state + "@" + decision.started) return
    jobSurfaced[desktopId] = state + "@" + decision.started
    var name = jobAppName(desktopId)
    if (state === "done") {
      // Only a genuinely installed release is news: up-to-date and
      // skipped-running were answers to an update the user started in the
      // panel, where the status row already showed them.
      if (decision.status !== "updated") return
      notifyDesktop("AppImage updated",
        Backend.elide(decision.newVersion !== ""
          ? name + " updated to " + decision.newVersion
          : (decision.message !== ""
              ? decision.message
              : name + " was updated"), 160))
    } else if (state === "error") {
      var why = decision.errorText !== "" ? decision.errorText : decision.message
      notifyDesktop("AppImage update failed",
        why !== "" ? name + ": " + Backend.elide(why, 160)
                   : name + ": the update failed")
    } else {
      notifyDesktop("AppImage update interrupted",
        name + ": the update was interrupted — retry it from the AppImage panel.")
    }
  }

  function jobAppName(desktopId) {
    var item = Model.findItem(desktopId)
    if (item && String(item.name) !== "") return String(item.name)
    return desktopId.substring(0, desktopId.length - 8)
  }

  // Notify-send through the shared Backend.run seam: title and body travel as
  // single argv arguments, never through a shell. A missing binary or non-zero
  // exit is a console.warn. Notifications queue behind the single in-flight
  // send; the queue is bounded and beyond it drops, since the panel still
  // shows the state.
  function notifyDesktop(title, body) {
    if (notifyCtx) {
      if (notifyQueue.length >= 8) return
      notifyQueue.push([title, body])
      return
    }
    var ctx = Backend.run(notifyProc, notifyTimer,
      ["notify-send", "-a", "AppImage for Omarchy", "--expire-time=5000"],
      [title, body], {}, function(result) {
        notifyCtx = null
        if (!result.ok) {
          console.warn("appimage service: notify-send failed:",
            result.error, Backend.elide(result.stderr || result.stdout))
        }
        drainNotifyQueue()
      })
    notifyCtx = ctx && ctx.active ? ctx : null
  }

  function drainNotifyQueue() {
    if (notifyCtx || notifyQueue.length === 0) return
    var next = notifyQueue.shift()
    notifyDesktop(next[0], next[1])
  }

  Component.onCompleted: {
    syncFromModel()
    unsubscribe = Model.subscribe(syncFromModel)
    loadSettings()
  }

  Component.onDestruction: {
    if (unsubscribe) unsubscribe()
    checkTimer.stop()
    notifyTimer.stop()
  }

  Process {
    id: fetchProc
    command: []
    stdout: StdioCollector {
      id: fetchStdout
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.fetchCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: fetchStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.fetchCtx, "stderr", data) }
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
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.settingsCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: settingsStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.settingsCtx, "stderr", data) }
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

  // Short-call pair for the badge self-heal's --list-installed.
  Process {
    id: listProc
    command: []
    stdout: StdioCollector {
      id: listStdout
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.listCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: listStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.listCtx, "stderr", data) }
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

  // Single-slot reader for job documents. Adoption reads them synchronously
  // (preload + waitForJob) and stays plain JS. blockLoading is deliberately
  // NOT used: on this Quickshell it defers loads past text() and hands back
  // the previous file's content.
  FileView {
    id: jobView
    path: ""
    preload: true
    printErrors: false
  }

  // Fire-and-forget pair for job-outcome notifications, same wiring as
  // listProc; a runaway notify-send is killed by the watchdog like any call.
  Process {
    id: notifyProc
    command: []
    stdout: StdioCollector {
      id: notifyStdout
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(root.notifyCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: notifyStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(root.notifyCtx, "stderr", data) }
    }
    onStarted: Backend.markStarted(root.notifyCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.notifyCtx, exitCode, notifyStdout.text, notifyStderr.text)
    }
  }

  Timer {
    id: notifyTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.notifyCtx)
  }

  Timer {
    id: checkTimer
    repeat: false
    onTriggered: root.fetch()
  }
}

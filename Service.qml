import QtQuick
import Quickshell
import Quickshell.Io
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend
import "lib/JobDocs.js" as JobDocs

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
// found something.
//
// Background job adoption (issue #12): when the startup settings read
// lands and at the end of every completed sweep, the service reads the
// background update-job documents (lib/JobDocs.js) for the apps the
// store knows and surfaces their finished outcomes — done, error,
// interrupted — as desktop notifications via notify-send, once per job
// cycle per shell session. This is the only thing QML notifies about:
// sweep announcements stay backend-owned (--fetch-updates' own dedup'd
// notify-send). Job reads are synchronous FileView reads of tiny local
// JSON documents, strictly best-effort, and always run AFTER the next
// check is armed — adoption can never delay, block or break the sweep.
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

  // Background job adoption (issue #12). jobsDir mirrors the backend's
  // plugin_state_dir()/jobs layout (constants.py honours XDG_STATE_HOME
  // with the same ~/.local/state default). jobSurfaced is the
  // once-per-session acknowledgement memory (desktop id → the outcome
  // state@job-cycle already surfaced for it this shell session — see
  // surfaceJobDecision); notifyCtx/notifyQueue carry the single
  // in-flight notify-send and its bounded queue.
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
            // Startup job adoption (issue #12): surface what a previous
            // session's background jobs left behind without waiting for
            // the first sweep. Runs after the rearm; a no-op when the
            // store's installed list has not landed yet (the first
            // sweep's own adoption pass covers that case).
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
        // End-of-sweep job adoption (issue #12). Strictly after
        // scheduleNext(): the next check is already armed before any
        // job document is touched, so adoption — however weird the
        // state dir looks — can never delay or derail the sweep cadence.
        checkJobs()
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

  // ---- background job adoption (issue #12) ------------------------------------
  //
  // Reads the background update-job documents the detached --update
  // --async runner publishes (backend/CONTRACT.md, "Job documents";
  // parse/adoption: lib/JobDocs.js) and surfaces their finished outcomes
  // once per job cycle per shell session. Called from the two hooks
  // above only — the startup settings read and the end of a completed
  // sweep — never from a timer of its own.
  //
  // Read mechanism: no directory listing. Quickshell 0.3's Io module has
  // no Dir type, and the candidate set is known anyway: a job document
  // only exists for an app that has a desktop id, and every such app is
  // in Model.state.items (the bar's startup sync and the sweeps keep
  // that list fresh). Iterating those ids means the paths we open are
  // derived from backend-provided ids instead of raw directory entries,
  // and idle cost stays zero — nothing watches the jobs dir between
  // checks.
  function checkJobs() {
    // A service feature like the sweeps themselves: with auto-checks
    // disabled the service stays completely idle (the panel still shows
    // job state when opened).
    if (!Model.state.settings.enabled) return
    var items = Model.state.items
    if (!items || items.length === 0) return
    for (var i = 0; i < items.length; i++) {
      try {
        adoptJobDocument(String((items[i] && items[i].id) || ""))
      } catch (e) {
        // Insurance only — the helpers below are written never to throw.
        console.warn("appimage service: job adoption skipped:", e)
      }
    }
  }

  // One candidate app: validate the id, read its document, adopt it.
  function adoptJobDocument(desktopId) {
    // Job documents exist only for real desktop ids (the document's file
    // name is the id's stem — jobs.py job_document_path). Model falls
    // back to the AppImage path when the backend reports a null id, and
    // such an app can never have a document.
    if (!/\.desktop$/.test(desktopId)) return
    // Shared stem rule (lib/JobDocs.js jobDocumentStem, mirror of
    // jobs.py _job_stem): the path we open is always one flat component
    // inside the jobs dir — never a traversal. The .desktop-suffix
    // expectation stays at this call site; the helper owns the stem rule.
    var stem = JobDocs.jobDocumentStem(desktopId)
    if (stem === "") return
    jobView.path = ""       // force a re-read even when the previous
    jobView.path = jobsDir + "/" + stem + ".json"  // candidate had this path
    var text = String(jobView.text() || "")
    var adopted = JobDocs.adoptDocument(text, true, Date.now())
    // Missing/unreadable (empty text — includes documents the janitor
    // aged out, which vanish silently per the contract) or corrupt:
    // skip silently, exactly like the rest of the service's failures.
    if (adopted.parseError !== "") return
    // Act only on a document that really belongs to the app we meant to
    // read; the parse layer is tolerant by design, the id check is ours.
    if (String(adopted.decision.desktopId) !== desktopId) return
    surfaceJobDecision(adopted.decision, desktopId)
  }

  // The notification decision, deduplicated once per job cycle per shell
  // session: jobSurfaced[desktopId] holds the outcome state plus the job
  // cycle it was surfaced for (state + "@" + the document's started
  // stamp), so re-reading the same terminal document on later sweeps is
  // free, a shell restart re-notifies once (the accepted semantics —
  // "once only, or until acknowledged"), the same outcome across
  // DIFFERENT job cycles is news each time (a retry that fails again
  // notifies again; a second completed update announces again), and a
  // retry or a resumed job (its document goes ACTIVE again) clears the
  // record so the genuinely new terminal outcome can surface once more.
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
      // skipped-running are answers to an update the user started in
      // the panel, where the status row already showed them. The whole
      // body is elided at the same bound as the error path — the
      // document-controlled version/message strings never own the
      // notification's size.
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

  // Desktop notifications for job outcomes: notify-send with the same
  // best-effort argv shape the backend's --fetch-updates announcement
  // uses (app name 'AppImage for Omarchy', expire-time 5000). Pure argv
  // through the shared Backend.run seam — title and body travel as
  // single arguments, nothing is ever interpolated into a shell. A
  // missing binary or non-zero exit is a console.warn, never an error.
  // Several outcomes can land in one adoption pass, so notifications
  // queue behind the single in-flight send; the queue is bounded and
  // beyond it drops (the panel still shows the state).
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

  // Short-call pair for the badge self-heal's --list-installed (same
  // wiring as settingsProc; a heal can overlap a fetch's own completion).
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

  // Single-slot reader for job documents (issue #12). These are tiny
  // local JSON files, so adoption reads them synchronously (blockLoading)
  // and stays plain JS — no timers, no callback chains, nothing left
  // running between checks. A failed/missing read yields empty text and
  // is skipped silently, so printErrors stays off.
  FileView {
    id: jobView
    path: ""
    blockLoading: true
    printErrors: false
  }

  // Fire-and-forget pair for job-outcome notifications (issue #12): the
  // same wiring as listProc. notify-send produces nothing we parse —
  // the handlers only feed the shared Backend.run ctx machinery, so a
  // runaway binary is killed by the watchdog like any other call.
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

import QtQuick
import Quickshell.Io
import "lib/Backend.js" as Backend
import "lib/JobDocs.js" as JobDocs

// Update controller for Panel.qml: the row Update flow, the force-quit consent
// card and the "Update all" batch. Non-visual wiring; the consent-card state and
// the spawn context stay on the panel. The spawn runs on the DEDICATED
// Process/Timer pair owned here, because the shared short-call pair would let an
// in-flight re-list or update check synthetically fail the click.
Item {
  id: updatesCtl

  // The owning panel (untyped on purpose: no import cycle).
  property var panel

  // One-click update. The click only spawns: `--update --async` answers
  // job-started (exit 0) or an error document (exit 1) within milliseconds, and
  // the transfer runs outside this panel's process tree, reaching the row
  // through its job document. A running app is the one exception: the confirm
  // card is shown instead. The `running` flag comes from the last listing and
  // can be stale: stale-but-running only grants a close the backend finds
  // nothing to do with; stale-but-not-running is caught by the backend.
  function requestUpdate(item) {
    if (!item || item.id === "") return
    if (panel.jobActive(item.id)) return
    if (item.running === true) {
      // Only one consent card at a time: the row's force-quit card replaces
      // the batch card and the other way round.
      dismissUpdateAll()
      panel.forceQuitId = item.id
      panel.forceQuitName = item.name !== "" ? item.name : item.id
      panel.forceQuitPids = item.runningPids > 0 ? item.runningPids : 0
      return
    }
    updateItem(item, false)
  }

  function dismissForceQuit() {
    panel.forceQuitId = ""
    panel.forceQuitName = ""
    panel.forceQuitPids = 0
  }

  // Confirm: run exactly the async job, now with the --kill-running grant (the
  // backend closes the app at job start and again before the atomic replace).
  function confirmForceQuit() {
    var id = panel.forceQuitId
    var name = panel.forceQuitName
    if (id === "") return
    dismissForceQuit()
    updateItem({ id: id, name: name }, true)
  }

  // ---- "Update all" ---------------------------------------------------------
  //
  // One action for every pending app. The click OPENS the consent card and
  // starts nothing; only the explicit accept spawns the jobs. An app with
  // no pending release, or whose verdict is false, is never named and never
  // closed.
  function requestUpdateAll() {
    var ids = panel.modelApi.pendingUpdateIds()
    if (ids.length === 0) return
    // Only one consent card at a time (see requestUpdate).
    dismissForceQuit()
    var apps = []
    for (var i = 0; i < ids.length; i++) {
      var item = panel.modelApi.findItem(ids[i])
      if (!item) continue
      apps.push({ id: item.id, name: item.name,
                  running: item.running === true,
                  runningPids: item.runningPids > 0 ? item.runningPids : 0 })
    }
    if (apps.length === 0) return
    panel.updateAllApps = apps
    panel.updateAllOpen = true
  }

  // Dismiss: nothing is spawned, nothing is closed.
  function dismissUpdateAll() {
    panel.updateAllOpen = false
    panel.updateAllApps = []
    panel.updateAllQueue = []
  }

  // Accept: one background job per app through --update --async. The consent
  // card is the --kill-running grant. Each app gets its OWN job, so the
  // backend's per-app claim still refuses a second update of the same app.
  function confirmUpdateAll() {
    if (!panel.updateAllOpen) return
    var apps = panel.updateAllApps
    panel.updateAllOpen = false
    panel.updateAllApps = []
    var queue = []
    for (var i = 0; i < apps.length; i++) queue.push(apps[i].id)
    panel.updateAllQueue = queue
    pumpUpdateAll()
  }

  // The spawn pair is one-at-a-time, so the queue advances when a spawn
  // finishes (updateItem pumps the queue on every exit path). An app that
  // left the store in the meantime is skipped — the backend refuses an
  // update that does not exist anyway.
  function pumpUpdateAll() {
    if (panel.updateAllQueue.length === 0) return
    if (panel.updateSpawnCtx && panel.updateSpawnCtx.active === true) return
    var id = String(panel.updateAllQueue.shift())
    var item = panel.modelApi.findItem(id)
    if (!item) {
      pumpUpdateAll()
      return
    }
    updateItem(item, true)
  }

  function updateItem(item, killRunning) {
    // Every exit path pumps the "Update all" queue, so a refused spawn must
    // not stall the apps behind it.
    if (!item || item.id === "") {
      pumpUpdateAll()
      return
    }
    // Per-app single flight: a row with an active job refuses here (the
    // button is disabled too; the backend refuses a second job regardless).
    if (panel.jobActive(item.id)) {
      pumpUpdateAll()
      return
    }
    if (panel.backendCommand.length === 0) {
      panel.modelApi.setError(panel.strings.backendMissing)
      panel.syncFromModel()
      pumpUpdateAll()
      return
    }
    // Single flight on the spawn pair itself: a second --update --async while
    // one spawn is still answering would overwrite updateSpawnCtx and misroute
    // the first reply.
    if (panel.updateSpawnCtx && panel.updateSpawnCtx.active === true) {
      pumpUpdateAll()
      return
    }
    panel.modelApi.clearError()
    panel.modelApi.setStatus(panel.modelApi.formatCount(item.name, panel.strings.updating))
    panel.syncFromModel()
    var args = ["--update", item.id, "--yes", "--json", "--async"]
    if (killRunning === true) args.push("--kill-running")
    // Default watchdog on purpose: this call is over in milliseconds.
    var ctx = Backend.run(updateSpawnProc, updateSpawnTimer, panel.backendCommand,
        args, {},
        function(result) {
      panel.updateSpawnCtx = null
      if (result.ok) {
        var doc = Backend.parseJson(result.stdout)
        if (doc && doc.result === "job-started") {
          // The row may spin before the job's first document write lands: adopt
          // a provisional active decision and read the real document right
          // away. Every later state replaces this, so the spinner can never
          // outlive its document.
          panel.jobStartGraces[item.id] = Date.now()
          panel.modelApi.adoptJobDecision(item.id,
            JobDocs.provisionalActiveDecision(item.id))
        } else {
          // job-started is the only success shape --async reports.
          panel.modelApi.setError(panel.modelApi.appErrorBanner(item.name,
            panel.strings.backendFailed + " (update)"), item.id)
        }
      } else {
        // Refusals arrive as error documents; the backend's own message
        // surfaces verbatim.
        panel.reportFailure(result, "update", item)
      }
      panel.syncFromModel()
      // Converge the row with whatever its job document says now; in the
      // refusal case the already-running job's document is what the row should
      // show.
      panel.queueJobRead(item.id)
      panel.pumpJobReads()
      // The spawn pair is free again: the next pending app of an "Update all"
      // batch may start.
      pumpUpdateAll()
    })
    // A synchronous onDone (no-backend-command) has already finished; ctx.active
    // is false then, the ctx stays null.
    panel.updateSpawnCtx = ctx && ctx.active ? ctx : null
  }

  // Dedicated spawn pair for --update --async: the click must never queue
  // behind the shared backendProc's single flight, which would synthetically
  // fail the spawn. The default 15 s watchdog bounds only the spawn, never the
  // detached transfer.
  Process {
    id: updateSpawnProc
    command: []
    stdout: StdioCollector {
      id: updateSpawnStdout
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(panel.updateSpawnCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: updateSpawnStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(panel.updateSpawnCtx, "stderr", data) }
    }
    // Proof of spawn: separates "python3 missing" (never started) from
    // "ran and failed" in the failure reporting.
    onStarted: Backend.markStarted(panel.updateSpawnCtx)
    onExited: function(exitCode) {
      Backend.handleExit(panel.updateSpawnCtx, exitCode, updateSpawnStdout.text, updateSpawnStderr.text)
    }
  }

  Timer {
    id: updateSpawnTimer
    repeat: false
    onTriggered: Backend.handleTimeout(panel.updateSpawnCtx)
  }
}

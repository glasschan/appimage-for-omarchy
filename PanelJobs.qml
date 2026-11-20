import QtQuick
import Quickshell.Io
import "lib/JobDocs.js" as JobDocs

// Background update-job controller for Panel.qml. Non-visual: owns the
// job-document reader plumbing (FileView + watchdog + poll) and the
// read/adoption functions. All state stays on the panel; this object is
// wiring, never a second store.
Item {
  id: jobs

  // The owning panel (untyped on purpose: no import cycle).
  property var panel

  // True while THIS row renders an active job. The only source is the adopted
  // document view, never a click flag.
  function jobActive(id) {
    var d = panel.jobStates[id]
    return !!d && d.state === "active"
  }

  // Queue every installed app's job document for adoption. Called on every
  // panel open, whenever the installed list lands, and by the job poll; the
  // queue drains one document at a time through jobFileView.
  function refreshJobStates() {
    for (var i = 0; i < panel.items.length; i++) {
      panel.queueJobRead(panel.items[i].id)
    }
    pumpJobReads()
  }

  function queueJobRead(id) {
    if (id === "" || panel.jobReadingId === id) return
    if (panel.jobReadQueue.indexOf(id) !== -1) return
    panel.jobReadQueue.push(id)
  }

  function pumpJobReads() {
    if (panel.jobReadingId !== "" || panel.jobReadQueue.length === 0) return
    var id = String(panel.jobReadQueue.shift())
    var path = JobDocs.jobDocumentPath(panel.jobsDir, id)
    if (path === "") {
      // Unusable desktop id: jobs.py's stem rule means no document can
      // exist for it — adopt the idle decision and move on.
      adoptJob(id, idleParsed())
      pumpJobReads()
      return
    }
    panel.jobReadingId = id
    jobReadWatchdog.restart()
    // Clear-then-set (the Service.qml jobView idiom): an equal-value property
    // write fires no change signal, so re-pointing the view at the SAME
    // document would never reload and hang the read until the watchdog retired
    // it without adopting anything.
    jobFileView.path = ""       // force a re-read even when the previous
    jobFileView.path = path     // read pointed at this same document
    // Complete the read inline. Without waitForJob() this Quickshell (0.3.2)
    // never delivers onLoaded/onLoadFailed for a MISSING document: the
    // operation hangs until the watchdog retires it and no decision ever
    // reaches a row.
    jobFileView.waitForJob()
  }

  // The "no document" parse result (file missing, or an id that cannot name
  // one): idle is ONLY ever adopted from an absent document, never from a
  // corrupt one.
  function idleParsed() {
    return { parseError: "", heartbeat: 0,
             decision: JobDocs.adoptJob(null, true, Date.now()) }
  }

  // Store one adopted decision and surface any non-active outcome. The
  // desktopId check keeps a document that names another app from dressing this
  // row.
  function adoptJob(id, parsed) {
    if (parsed.decision.desktopId !== "" && parsed.decision.desktopId !== id) {
      parsed = idleParsed()
    }
    // Click grace: an early "no document" read must not overwrite a provisional
    // active decision.
    if (parsed.decision.state === "idle" && jobActive(id)
        && Date.now() - (panel.jobStartGraces[id] || 0) < panel.jobStartGraceMs) {
      return
    }
    panel.jobStartGraces[id] = 0
    panel.modelApi.adoptJobDecision(id, parsed.decision)
    panel.syncFromModel()
    var entry = panel.modelApi.jobEntry(id)
    surfaceJobOutcome(id, parsed, entry ? entry.consumed : 0)
  }

  // Terminal and interrupted outcomes surface ONCE per sign-of-life (the
  // document's heartbeat; a corrupt document has none, so -1 marks its
  // once-per-session banner).
  function surfaceJobOutcome(id, parsed, consumed) {
    var d = parsed.decision
    if (d.state === "active" || d.state === "idle") return
    var seen = parsed.heartbeat > 0
      ? consumed === parsed.heartbeat : consumed === -1
    if (seen) return
    panel.modelApi.markJobConsumed(
      id, parsed.heartbeat > 0 ? parsed.heartbeat : -1)
    if (d.state === "done") {
      // This app reached an outcome, so an older error about it is spent.
      panel.modelApi.noteOutcome(id)
      // "updated" changed the AppImage (and its desktop entry's version):
      // re-list so names, versions and manager columns stay truthful.
      if (d.status === "updated") {
        panel.modelApi.setStatus(panel.modelApi.jobDoneBanner(d, ""))
        panel.refresh()
      } else if (d.status === "up-to-date") {
        panel.modelApi.setStatus(
          panel.modelApi.jobDoneBanner(d, panel.strings.upToDate))
      } else if (d.status === "skipped-running") {
        panel.modelApi.setStatus(
          d.message !== "" ? d.message : panel.strings.skippedRunning)
      } else if (d.message !== "") {
        panel.modelApi.setStatus(d.message)
      }
    } else if (d.state === "error") {
      // The error line also carries the closed-app clause when the job
      // closed the app and THEN failed (force_quit on an error document).
      panel.modelApi.setError(
        panel.modelApi.jobErrorBanner(d, panel.strings.backendFailed), id)
    } else { // interrupted: dead updater or a heartbeat stale past 120 s
      panel.modelApi.setError(panel.strings.jobInterrupted, id)
    }
    panel.syncFromModel()
  }

  // Per-row progress label: percent from the document's progress fraction,
  // with the byte pair only when the declared total is plausibly the payload
  // (JobDocs.bytesSane).
  function jobProgressLabel(id) {
    var d = panel.jobStates[id]
    if (!d || d.state !== "active") return ""
    var label = Math.round((d.progress || 0) * 100) + "%"
    if (JobDocs.bytesSane(d.totalBytes)) {
      label += " · " + JobDocs.formatBytes(d.downloadedBytes)
        + " / " + JobDocs.formatBytes(d.totalBytes)
    }
    return label
  }

  // One reusable FileView drains the document queue: pumpJobReads points it at
  // one job document at a time, and the handlers adopt the result. loadFailed
  // means "no document", which adopts idle; a present-but-corrupt file READS
  // fine and adopts interrupted instead. preload keeps every read asynchronous;
  // printErrors stays off because a missing document is idle, not an error.
  FileView {
    id: jobFileView
    path: ""
    preload: true
    printErrors: false
    onLoaded: {
      var id = panel.jobReadingId
      panel.jobReadingId = ""
      jobReadWatchdog.stop()
      // The path guard retires a superseded read. `null` is the pid argument
      // this panel can stand behind: QML cannot probe a pid, so the heartbeat
      // rule decides alone.
      if (id !== "" && jobFileView.path === JobDocs.jobDocumentPath(panel.jobsDir, id)) {
        jobs.adoptJob(id, JobDocs.adoptDocumentForPanel(jobFileView.text(), null, Date.now()))
      }
      jobs.pumpJobReads()
    }
    onLoadFailed: {
      var id = panel.jobReadingId
      panel.jobReadingId = ""
      jobReadWatchdog.stop()
      // Same superseded-read guard as onLoaded: a failure report for a read
      // the queue has already repointed away from must not adopt idle for the
      // wrong id.
      if (id !== "" && jobFileView.path === JobDocs.jobDocumentPath(panel.jobsDir, id)) {
        jobs.adoptJob(id, jobs.idleParsed())
      }
      jobs.pumpJobReads()
    }
  }

  // Read watchdog: retire a wedged read (5 s is generous) so the queue and the
  // progress UI keep moving.
  Timer {
    id: jobReadWatchdog
    interval: 5000
    repeat: false
    onTriggered: {
      if (panel.jobReadingId === "") return
      panel.jobReadingId = ""
      jobs.pumpJobReads()
    }
  }

  // The job poll: only while the panel is open AND at least one row carries a
  // non-idle decision, re-read every job document at jobPollMs (3 s; the writer
  // heartbeats every 2 s, the contract goes stale at 120 s).
  Timer {
    id: jobPoll
    interval: panel ? panel.jobPollMs : 3000
    repeat: true
    running: panel ? (panel.opened && panel.jobsBusy) : false
    onTriggered: panel.refreshJobStates()
  }
}

import QtQuick
import Quickshell.Io
import "lib/Backend.js" as Backend

// python3 dependency controller for Panel.qml (authorized install).
// Non-visual wiring: the probe/install state stays on the panel. Owns the
// dedicated probe Process (it must answer even while a backend call is failing
// or in flight), the install pair (a short prep, then the detached floating
// terminal) and the poll/watchdog timers. The timers are driven imperatively so
// no leftover binding can fire a stray probe.
Item {
  id: pythonCtl

  // The owning panel (untyped on purpose: no import cycle).
  property var panel

  // One probe run through the same Backend.run plumbing as every backend
  // call: settles python presence, and while an authorized install runs, reads
  // the terminal's marker files.
  function probePython() {
    if (panel.probeCtx && panel.probeCtx.active) return
    var ctx = Backend.run(pythonProbeProc, pythonProbeTimer,
      panel.modelApi.pythonProbeArgv(panel.pythonFailureMarker, panel.pythonCompleteMarker,
        panel.installingPython),
      {}, {}, function(result) {
        panel.probeCtx = null
        handleProbe(result.exitCode, result.stdout)
      })
    panel.probeCtx = ctx && ctx.active ? ctx : null
  }

  // Route a finished probe. "waiting" keeps the poll running; "present" tears
  // the install state down and refreshes the backend; "canceled"/"failed"
  // surface the terminal's verdict.
  function handleProbe(exitCode, output) {
    var outcome = panel.modelApi.pythonProbeOutcome(exitCode, output, panel.installingPython)
    // Verify-then-start: a pending Install press is answered by this probe
    // alone. Only a confirmed-missing verdict backed by a REAL exit may start
    // the install. The exitCode >= 0 gate matters: a probe that never answered
    // arrives from Backend.handleTimeout as exitCode -1, which the outcome
    // mapper also labels "missing"; trusting that would open the sudo terminal
    // with no presence check at all.
    if (panel.installVerifyPending) {
      panel.installVerifyPending = false
      if (outcome === "missing" && exitCode >= 0) {
        startPythonInstall()
        return
      }
      if (outcome !== "present") {
        panel.syncFromModel()
        return
      }
    }
    if (outcome === "waiting") return
    panel.pythonStateKnown = true
    if (outcome === "missing") {
      // Wipe a stale banner only on the false→true transition: the slow
      // post-timeout poll re-reports "missing" every 15 s, and an
      // unconditional clear would erase the waiting message.
      var wasMissing = panel.pythonMissing
      panel.pythonMissing = true
      if (!wasMissing) panel.modelApi.clearError()
      panel.syncFromModel()
      return
    }
    if (outcome === "present") {
      var recovered = panel.pythonMissing || panel.installingPython
      panel.pythonMissing = false
      panel.installingPython = false
      pythonPoll.stop()
      pythonInstallTimeout.stop()
      pythonSlowPoll.stop()
      if (recovered) {
        panel.modelApi.setStatus(panel.strings.pythonReady)
        panel.syncFromModel()
        panel.refresh()
      }
      return
    }
    panel.installingPython = false
    pythonPoll.stop()
    pythonInstallTimeout.stop()
    // Defensive: a canceled/failed verdict also retires the slow poll so a
    // dead attempt cannot keep probing behind the error banner.
    pythonSlowPoll.stop()
    panel.modelApi.setError(outcome === "canceled"
      ? panel.strings.pythonInstallCanceled : panel.strings.pythonInstallFailed)
    panel.syncFromModel()
  }

  // The Install button. Nothing runs until this is pressed, and what then runs
  // is a visible floating terminal presenting the exact constant command (sudo
  // prompt included), never a hidden process.
  //
  // Verify-then-start: the click spawns nothing directly. It arms
  // installVerifyPending and probes; only a confirmed-missing answer opens the
  // terminal. It does not dedupe against an earlier terminal still at its sudo
  // prompt.
  function installPython() {
    if (panel.installingPython) return
    if (!panel.modelApi.pythonRuntimeDirUsable(panel.runtimeDir)) {
      panel.modelApi.setError(panel.strings.pythonNoRuntimeDir)
      panel.syncFromModel()
      return
    }
    // A slow poll from a timed-out attempt may still be running; this new
    // attempt owns the flow from here.
    pythonSlowPoll.stop()
    panel.installVerifyPending = true
    probePython()
  }

  // The verified install start, reached only through handleProbe. Enters
  // install mode, then arms the prep step: it re-validates the runtime dir
  // (absolute, directory, euid-owned, mode 700 — exit 5 = refused) and clears
  // stale markers so the poll sees only THIS attempt's verdict.
  function startPythonInstall() {
    panel.installingPython = true
    panel.modelApi.clearError()
    panel.syncFromModel()
    pythonInstallPrepProc.command = panel.modelApi.pythonInstallPrepArgv(panel.runtimeDir)
    pythonInstallPrepProc.running = true
    // Prep watchdog: nothing else bounds the prep step (the install timeout
    // only arms after prep success), so a hung prep would leave
    // installingPython stuck true forever without this.
    pythonPrepTimer.restart()
  }

  // Recovery-poll handover for the panel's onOpenedChanged: closing the panel
  // must stop the post-timeout slow poll.
  function stopRecoveryPoll() {
    pythonSlowPoll.stop()
  }

  Process {
    id: pythonProbeProc
    command: []
    stdout: StdioCollector {
      id: pythonProbeOutput
      waitForEnd: true
      // streamed producer-side bound (see Backend.feedStream)
      onRead: function(data) { Backend.feedStream(panel.probeCtx, "stdout", data) }
    }
    stderr: StdioCollector {
      id: pythonProbeStderr
      waitForEnd: true
      onRead: function(data) { Backend.feedStream(panel.probeCtx, "stderr", data) }
    }
    onStarted: Backend.markStarted(panel.probeCtx)
    onExited: function(exitCode) {
      Backend.handleExit(panel.probeCtx, exitCode, pythonProbeOutput.text, pythonProbeStderr.text)
    }
  }

  Timer {
    id: pythonProbeTimer
    repeat: false
    onTriggered: Backend.handleTimeout(panel.probeCtx)
  }

  Process {
    id: pythonInstallPrepProc
    onExited: function(exitCode) {
      // Any real exit retires the one-shot watchdog first: the timer must
      // never fire after the prep answered.
      pythonPrepTimer.stop()
      if (!panel.installingPython) return
      // 5 = the prep script refused the runtime dir (not an absolute directory
      // owned by the euid with mode 700): the cheap JS gate passed but the
      // filesystem disagreed, so surface the same message and stand down before
      // ever opening the terminal.
      if (exitCode === 5) {
        panel.installingPython = false
        panel.modelApi.setError(panel.strings.pythonNoRuntimeDir)
        panel.syncFromModel()
        return
      }
      if (exitCode !== 0) {
        // The terminal never opened here, so the install copy would lie; this
        // message says nothing was changed.
        panel.installingPython = false
        panel.modelApi.setError(panel.strings.pythonInstallPrepFailed)
        panel.syncFromModel()
        return
      }
      pythonInstallerProc.command = panel.modelApi.pythonInstallArgv()
      pythonInstallerProc.startDetached()
      pythonPoll.restart()
      pythonInstallTimeout.restart()
    }
  }

  // Prep watchdog: bounds the prep Process (10 s for local stat/rm work is
  // generous). Kill it, stand down and show the honest prep-failure copy; the
  // eventual onExited is a no-op because installingPython is already false.
  Timer {
    id: pythonPrepTimer
    interval: 10000
    repeat: false
    onTriggered: {
      if (!pythonInstallPrepProc.running) return
      pythonInstallPrepProc.running = false
      panel.installingPython = false
      panel.modelApi.setError(panel.strings.pythonInstallPrepFailed)
      panel.syncFromModel()
    }
  }

  // Detached on purpose: the terminal outlives probe polls and reports through
  // the marker files, not through this process object.
  Process { id: pythonInstallerProc }

  Timer {
    id: pythonPoll
    interval: 1000
    repeat: true
    onTriggered: pythonCtl.probePython()
  }

  Timer {
    id: pythonInstallTimeout
    interval: 300000
    onTriggered: {
      if (!panel.installingPython) return
      panel.installingPython = false
      pythonPoll.stop()
      panel.modelApi.setError(panel.strings.pythonInstallWaiting)
      panel.syncFromModel()
      // The terminal can outlive the panel's patience: hand recovery to a slow
      // probe, but only while the panel is open. A timeout firing on a closed
      // panel must not start a 15 s probe that outlives the session. The probe
      // re-checks python presence from the SYSTEM (installingPython is false,
      // so markers are ignored), so a leftover marker cannot false-positive it.
      if (panel.opened) pythonSlowPoll.restart()
    }
  }

  // Post-timeout recovery poll: 15 s probes that keep an OPEN panel learning
  // after the 5-minute install watchdog gave up. Started only while the panel
  // is open, stopped on any terminal verdict, when a new install attempt
  // starts, and when the panel closes.
  Timer {
    id: pythonSlowPoll
    interval: 15000
    repeat: true
    onTriggered: pythonCtl.probePython()
  }
}

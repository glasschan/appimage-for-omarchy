// ---- Python dependency card (authorized in-panel install) ---------------------
// All machinery is pure string/argv building plus one exit-code mapper, so
// the whole flow is pinned here without spawning anything. The real flow:
// probe → (missing) card → button → detached `omarchy launch floating
// terminal with presentation <command>` → marker files → 1 s probe poll.

import { Backend, Model, check } from "../core.mjs"
import { fakeProc } from "../fakes.mjs"

export function run() {
  check("python strings present", !!Model.strings.pythonMissingTitle && !!Model.strings.installPython
    && !!Model.strings.installingPython && !!Model.strings.pythonInstallCanceled
    && !!Model.strings.pythonInstallFailed && !!Model.strings.pythonInstallWaiting
    && !!Model.strings.pythonNoRuntimeDir && !!Model.strings.pythonInstallPrepFailed)
  // The prep-failure copy must stay honest: the terminal never opened, so
  // nothing was installed — never point the user at a terminal that does not
  // exist (the generic pythonInstallFailed/Waiting copy does, correctly).
  check("python prep-failure copy owns its honesty",
    Model.strings.pythonInstallPrepFailed.includes("nothing was installed")
    && !Model.strings.pythonInstallPrepFailed.includes("terminal and try"))
  check("python marker paths", Model.pythonMarkerPaths("/run/user/1").failure
      === "/run/user/1/appimage-plugin-python-install.failed"
    && Model.pythonMarkerPaths("/run/user/1").complete
      === "/run/user/1/appimage-plugin-python-install.complete")
  check("python marker paths tolerate empty dir", Model.pythonMarkerPaths("").failure
    === "/appimage-plugin-python-install.failed")

  const installArgv = Model.pythonInstallArgv()
  check("python install argv opens omarchy floating terminal", installArgv.slice(0, 6).join(" ")
    === "/usr/bin/omarchy launch floating terminal with presentation")
  const presented = installArgv[6]
  check("python install command runs omarchy pkg add",
    presented.includes("/usr/bin/omarchy pkg add python"))
  check("python install command clears markers first",
    presented.indexOf("/usr/bin/rm -f") !== -1
    && presented.indexOf("/usr/bin/rm -f") < presented.indexOf("/usr/bin/omarchy pkg add"))
  check("python install command touches complete marker on success",
    presented.includes(': > "${XDG_RUNTIME_DIR:?}/appimage-plugin-python-install.complete"'))
  check("python install command writes status to failure marker",
    presented.includes('printf \'%s\\n\' "$status" > "${XDG_RUNTIME_DIR:?}/appimage-plugin-python-install.failed"'))
  check("python install command preserves exit status",
    presented.includes('(exit "$status")'))
  check("python install command absolute paths only",
    !presented.includes(" rm ") && !presented.includes("; omarchy ")
    && !presented.includes(";rm ") && !presented.includes(";omarchy "))

  // Round-3 security finding: XDG_RUNTIME_DIR used to be interpolated into the
  // presented shell SOURCE (the omarchy wrapper does bash -c "$cmd"), so a
  // crafted runtime-dir value executed as code on Install. The source must be
  // CONSTANT: environment-derived values may only ever reach the terminal as
  // shell expansions inside double quotes or as argv/env data — never this
  // string. HOSTILE_DIRS includes command substitutions, backticks, quote
  // breakouts, newlines, expansions, traversal, relative and empty values.
  const HOSTILE_DIRS = ["$(id > /tmp/pwned)", "`id > /tmp/pwned`", '"; evil; :"', "a\nnewline", "$PATH", "/run/user/1/../../escape", "relative/path", "", "/run/user/1"]
  {
    const constantSource = Model.pythonInstallArgv().slice(-1)[0]
    let identical = true
    for (const dir of HOSTILE_DIRS) {
      // Exercise the whole builder chain for each hostile value: whatever the
      // environment holds, the presented shell line may not change.
      Model.pythonMarkerPaths(dir)
      if (Model.pythonInstallArgv().slice(-1)[0] !== constantSource) identical = false
    }
    check("shell source constant regardless of environment-derived input",
      identical === true, "presented command changed with hostile runtime dir")
    check("shell source free of hostile values",
      constantSource.indexOf("$(id") === -1 && constantSource.indexOf("`") === -1
      && constantSource.indexOf("\n") === -1 && constantSource.indexOf("$PATH") === -1,
      JSON.stringify(constantSource))
    check("shell source reads runtime dir via :? expansion",
      constantSource.includes("${XDG_RUNTIME_DIR:?}"))
  }

  // Cheap panel-side gate: syntactic absolute-path sanity only (the
  // filesystem-level owner/mode check is the prep script below).
  check("runtime dir usable rejects empty", Model.pythonRuntimeDirUsable("") === false)
  check("runtime dir usable rejects relative", Model.pythonRuntimeDirUsable("relative/path") === false)
  check("runtime dir usable rejects traversal",
    Model.pythonRuntimeDirUsable("/run/user/1/../../escape") === false)
  check("runtime dir usable accepts absolute", Model.pythonRuntimeDirUsable("/run/user/1") === true)

  // Prep step: constant `sh -c` source + the runtime dir/markers as argv DATA.
  // argv[0] and the "$0" label are the absolute /usr/bin/sh — fixed-path
  // invariant, same as /usr/bin/omarchy, /usr/bin/rm and /usr/bin/stat; a
  // bare "sh" would silently reintroduce a PATH-resolved exec hop.
  const prepArgv = Model.pythonInstallPrepArgv("/run/user/1")
  const prepScript = prepArgv[2]
  check("python prep argv shape", prepArgv[0] === "/usr/bin/sh" && prepArgv[1] === "-c"
    && typeof prepScript === "string" && prepArgv[3] === "/usr/bin/sh"
    && prepArgv[4] === "/run/user/1"
    && prepArgv[5] === "/run/user/1/appimage-plugin-python-install.failed"
    && prepArgv[6] === "/run/user/1/appimage-plugin-python-install.complete",
    JSON.stringify(prepArgv))
  check("python prep argv passes empty dir verbatim",
    Model.pythonInstallPrepArgv("")[4] === "")
  check("python prep script guards the runtime dir",
    prepScript.includes('case "$1" in') && prepScript.includes("stat -c %a")
    && prepScript.includes('"700"'), JSON.stringify(prepScript))
  check("python prep script is positional-only",
    prepScript.indexOf("runtimeDir") === -1 && prepScript.indexOf("pythonMarkerPaths") === -1
    && prepScript.includes('"$1"') && prepScript.includes('"$2"') && prepScript.includes('"$3"'))

  const probeArgv = Model.pythonProbeArgv("/f", "/c", true)
  check("python probe argv shape", probeArgv[0] === "/usr/bin/sh" && probeArgv[1] === "-c"
    && probeArgv[3] === "/usr/bin/sh" && probeArgv[4] === "/f" && probeArgv[5] === "/c"
    && probeArgv[6] === "1")
  check("python probe argv idle flag", Model.pythonProbeArgv("/f", "/c", false)[6] === "0")
  // The installing flag stays argv DATA driving the marker-aware poll mode
  // (pythonProbeOutcome unchanged): slow-poll recovery probes pass "0" and
  // decide from `command -v python3` alone — markers cannot false-positive.
  check("python probe outcome semantics unchanged",
    Model.pythonProbeOutcome(0, "", false) === "present"
    && Model.pythonProbeOutcome(3, "", false) === "missing")

  // Panel-side install-verify gate (Panel.qml handleProbe, installVerifyPending
  // branch): a probe that never answered arrives from Backend.handleTimeout as
  // exitCode -1, and the mapper's fall-through default labels -1 "missing".
  // Panel.qml therefore requires `outcome === "missing" && exitCode >= 0`
  // before the verify branch may open the sudo terminal. These checks pin both
  // ends of that chain — the timeout result really carries -1, and the mapper
  // really defaults -1 to "missing" — so either changing forces a re-look at
  // that gate.
  {
    const timeoutResults = []
    const probeCtx = Backend.run(fakeProc(), { interval: 0, restart() {}, stop() {} },
      ["python3"], [], {}, (r) => timeoutResults.push(r))
    Backend.handleTimeout(probeCtx)
    check("timed-out probe result carries exitCode -1", timeoutResults.length === 1
      && timeoutResults[0].exitCode === -1 && timeoutResults[0].timedOut === true,
      JSON.stringify(timeoutResults))
  }
  check("probe -1 never-answered maps to missing by default — Panel verify gate must not trust it",
    Model.pythonProbeOutcome(-1, "", false) === "missing"
    && Model.pythonProbeOutcome(-1, "", true) === "missing")

  // Exit-code semantics the panel's handleProbe routes on (pythonProbeOutcome).
  check("probe 0 → present", Model.pythonProbeOutcome(0, "", true) === "present")
  check("probe 1 → missing", Model.pythonProbeOutcome(1, "", false) === "missing")
  check("probe 3 while idle → missing", Model.pythonProbeOutcome(3, "", false) === "missing")
  check("probe 3 while installing → waiting", Model.pythonProbeOutcome(3, "", true) === "waiting")
  check("probe 2 with status 130 → canceled", Model.pythonProbeOutcome(2, "130\n", true) === "canceled")
  check("probe 2 with other status → failed", Model.pythonProbeOutcome(2, "1\n", true) === "failed")
}

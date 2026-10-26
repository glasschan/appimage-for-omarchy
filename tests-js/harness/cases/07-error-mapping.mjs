// ---- Panel-side error mapping (mirrors reportFailure branches) ---------------
// Timeout is its own message ("Backend timed out") — blaming python3 for a
// slow update sent users hunting the wrong fix. A spawn failure only maps to
// the python hint when the probe has not already confirmed python missing
// (then the install card owns the screen and the banner is cleared).

import { Backend, Model, cap, check, updates } from "../core.mjs"

export function run() {
  function reportFailureLike(result, pythonMissing) {
    if (result.wait === true) return "wait"
    if (result.timedOut) return "timeout"
    if (result.spawnFailed || result.error === "no-backend-command") {
      if (result.spawnFailed && pythonMissing === true) return "cleared"
      return "backendMissing"
    }
    var doc = Backend.parseJson(result.stdout)
    if (doc && doc.result === "error" && doc.error) return String(doc.error)
    if (result.stderr !== "" || result.stdout !== "") return "backendFailed"
    return "backendFailed"
  }
  check("spawn failure → hint", reportFailureLike({ timedOut: false, spawnFailed: true, error: "exit--1" }) === "backendMissing")
  check("spawn failure with python confirmed missing → card owns it",
    reportFailureLike({ timedOut: false, spawnFailed: true, error: "exit--1" }, true) === "cleared")
  check("timeout → its own message", reportFailureLike({ timedOut: true }) === "timeout")
  check("a second operation is a wait, not a failure",
    reportFailureLike({ ok: false, error: "busy", wait: true }) === "wait")
  check("exit1 error doc → backend message",
    reportFailureLike({ ok: false, exitCode: 1, stdout: cap("error.json"), stderr: "", error: "exit-1" })
      .includes("not integrated"))
  check("garbage stdout + stderr → generic",
    reportFailureLike({ ok: false, exitCode: 1, stdout: "Traceback...", stderr: "boom", error: "exit-1" }) === "backendFailed")

  // ---- F8 service: badge self-heal for apps integrated after startup -----------
  // Service.qml heals the badge when a sweep reports an id the store has never
  // seen (the app was integrated after the bar's startup --list-installed):
  // one extra --list-installed, then Model.setItems + a second applyUpdates.
  // The trigger check and the heal sequence are plain store calls, so they are
  // exercised here against the real Model library without any QML. Store state
  // from the sections above is no longer needed, so this rewrites it freely.
  const startupList = Model.parseListJson(cap("list.json")) // startup sync: neovim only
  const healUpdates = updates.updates.map((u) => Object.assign({}, u))
  healUpdates.push(Model.normalizeItem({
    desktop_id: "thirdapp.desktop", name: "Third", path: "/x/third.appimage",
    current_version: "v1.0", available_version: "v2.0", manager: "StaticFileUpdater",
    embedded_source: false, download_size: 42, running: false }))
  Model.setItems(startupList.items)
  check("heal trigger: unknown id detected",
    Model.findItem("neovim.desktop") !== null && Model.findItem("thirdapp.desktop") === null)
  // The pre-fix gap: applyUpdates alone merges into known ids only — the new
  // app is invisible to updateCount() until the heal runs.
  Model.applyUpdates(healUpdates)
  check("applyUpdates alone ignores unknown id", Model.findItem("thirdapp.desktop") === null
    && Model.updateCount() === 1)
  // The heal itself: the fresh --list-installed now contains the new app…
  const healedList = Model.parseListJson(
    '{"schema_version":1,"installed":['
    + '{"name":"Neovim (v0.11.3)","path":"/tmp/appimage-e2e/home/AppImages/neovim.appimage",'
    + '"desktop_id":"neovim.desktop","current_version":"v0.11.3","manager":null,'
    + '"embedded_source":false,"download_size":null,"running":false},'
    + '{"name":"Third","path":"/x/third.appimage","desktop_id":"thirdapp.desktop",'
    + '"current_version":"v1.0","manager":null,"embedded_source":false,'
    + '"download_size":null,"running":false}]}')
  check("heal list parses", healedList.ok === true && healedList.items.length === 2)
  // …then Service.qml's exact sequence: setItems + re-apply the sweep.
  Model.setItems(healedList.items)
  Model.applyUpdates(healUpdates)
  check("heal counts the new app", Model.findItem("thirdapp.desktop") !== null
    && Model.findItem("thirdapp.desktop").hasUpdate === true
    && Model.findItem("thirdapp.desktop").updateVersion === "v2.0")
  check("heal keeps known apps counted", Model.findItem("neovim.desktop").hasUpdate === true
    && Model.updateCount() === 2)
}

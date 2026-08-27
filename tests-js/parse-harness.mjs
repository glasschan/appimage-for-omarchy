// Harness: feeds real backend/main.py output (captured in tests-js/captures/
// during an e2e run) through the exact QML-side JS parse logic
// (lib/Model.js + lib/Backend.js).
// Run: node tests-js/parse-harness.mjs
import { readFileSync } from "node:fs"
import vm from "node:vm"

const load = (path) => {
  // .pragma library is QML-only; strip the pragma line and run the rest in
  // a sandbox so top-level function/var declarations land on the sandbox
  // object, mirroring how QML exposes a library script's members.
  const src = readFileSync(path, "utf8").replace(/^\.pragma library\s*/m, "")
  const sandbox = {}
  vm.runInNewContext(src, sandbox, { filename: path })
  return sandbox
}

const Model = load(new URL("../lib/Model.js", import.meta.url).pathname)
const Backend = load(new URL("../lib/Backend.js", import.meta.url).pathname)

let failures = 0
const check = (name, cond, detail) => {
  if (cond) console.log("PASS " + name)
  else { failures++; console.log("FAIL " + name + (detail ? " — " + detail : "")) }
}
const cap = (f) => readFileSync(new URL("./captures/" + f, import.meta.url), "utf8")

// ---- F1: --list-installed --json ------------------------------------------
const list = Model.parseListJson(cap("list.json"))
check("list ok", list.ok === true)
check("list count", list.items.length === 1, JSON.stringify(list.items))
const nvim = list.items[0]
check("list id=desktop_id", nvim.id === "neovim.desktop", nvim.id)
check("list name", nvim.name === "Neovim (v0.11.3)", nvim.name)
check("list version", nvim.version === "v0.11.3", nvim.version)
check("list path", nvim.path === "/tmp/appimage-e2e/home/AppImages/neovim.appimage", nvim.path)
check("list running", nvim.running === false)
check("list iconBase", nvim.iconBase === "/tmp/appimage-e2e/home/AppImages/.icons/neovim", nvim.iconBase)
check("list updateVersion null→empty", nvim.updateVersion === "")

const empty = Model.parseListJson('{"schema_version": 1, "installed": []}')
check("empty list ok", empty.ok === true && empty.items.length === 0)

const garbage = Model.parseListJson("python3: not found\n")
check("garbage rejected", garbage.ok === false && garbage.error !== "")

// ---- F2: --integrate outcomes ----------------------------------------------
const integ = Model.parseActionJson(cap("integrated.json"))
check("integrated result", integ.ok === true && integ.result === "integrated")
check("integrated app mapped", integ.app && integ.app.id === "neovim.desktop" && integ.app.name === "Neovim (v0.11.3)")
check("integrated message", integ.message.includes("integrated successfully"))

const already = Model.parseActionJson(cap("already.json"))
check("already result", already.ok === true && already.result === "already-integrated", JSON.stringify(already))
check("already app mapped", already.app && already.app.path.includes("neovim.appimage"))

const errDoc = Model.parseActionJson(cap("error.json"))
check("error doc result", errDoc.ok === false && errDoc.result === "error")
check("error doc message", errDoc.error.includes("AppImage not integrated"), errDoc.error)

const noYes = Model.parseActionJson(cap("noyes.json"))
check("json-without-yes doc", noYes.ok === false && noYes.error.includes("--yes"), noYes.error)

// ---- F3: --remove success shape (CONTRACT.md sample) ------------------------
const removed = Model.parseActionJson(
  '{"schema_version": 1, "result": "removed", "message": "/x/n.appimage was removed successfully", '
  + '"app": {"name": "Neovim (v0.11.3)", "path": "/x/neovim.appimage", "desktop_id": "neovim.desktop", '
  + '"current_version": "v0.11.3", "available_version": null, "download_size": null, "manager": null, '
  + '"embedded_source": false, "running": false}}')
check("removed result", removed.ok === true && removed.result === "removed")

// desktop_id null falls back to path (contract allows null)
const nullId = Model.parseListJson(
  '{"schema_version":1,"installed":[{"name":"X","path":"/opt/x.appimage","desktop_id":null,'
  + '"current_version":null,"available_version":null,"download_size":null,"manager":null,'
  + '"embedded_source":false,"running":true}]}')
check("null desktop_id → path id", nullId.items[0].id === "/opt/x.appimage", nullId.items[0].id)
check("null version → empty", nullId.items[0].version === "")
check("running passthrough", nullId.items[0].running === true)

// ---- Backend.js helpers ------------------------------------------------------
const tolerant = Backend.parseJson('{"schema_version": 1, "installed": []}')
check("Backend.parseJson strict", tolerant && tolerant.schema_version === 1)
const stray = Backend.parseJson('INFO: noise\n{"result":"integrated"}')
check("Backend.parseJson tolerant", stray && stray.result === "integrated")
check("Backend.elide clamp", Backend.elide("x".repeat(500)).length <= 200)

// ---- store behavior used by the panel ---------------------------------------
Model.setItems(list.items)
check("installedCount", Model.installedCount() === 1)
check("runningCount", Model.runningCount() === 0)
Model.markRunning("neovim.desktop", true)
check("markRunning", Model.runningCount() === 1)
Model.removeById("neovim.desktop")
check("removeById", Model.installedCount() === 0)
check("claimStartupSync once", Model.claimStartupSync() === true && Model.claimStartupSync() === false)

// ---- Panel-side error mapping (mirrors reportFailure branches) ---------------
function reportFailureLike(result) {
  if (result.timedOut || result.spawnFailed || result.error === "no-backend-command") return "backendMissing"
  var doc = Backend.parseJson(result.stdout)
  if (doc && doc.result === "error" && doc.error) return String(doc.error)
  if (result.stderr !== "" || result.stdout !== "") return "backendFailed"
  return "backendFailed"
}
check("spawn failure → hint", reportFailureLike({ timedOut: false, spawnFailed: true, error: "exit--1" }) === "backendMissing")
check("timeout → hint", reportFailureLike({ timedOut: true }) === "backendMissing")
check("exit1 error doc → backend message",
  reportFailureLike({ ok: false, exitCode: 1, stdout: cap("error.json"), stderr: "", error: "exit-1" })
    .includes("not integrated"))
check("garbage stdout + stderr → generic",
  reportFailureLike({ ok: false, exitCode: 1, stdout: "Traceback...", stderr: "boom", error: "exit-1" }) === "backendFailed")

console.log(failures === 0 ? "\nALL PASS" : "\n" + failures + " FAILURES")
process.exit(failures === 0 ? 0 : 1)

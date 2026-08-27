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

// ---- F6: --list-updates --json ------------------------------------------------
// Real capture: a custom StaticFileUpdater app (static sources carry no
// version) + the real neovim AppImage whose embedded gh-releases-zsync
// source was checked against the live GitHub API (values frozen at capture
// time, like every capture here).
const updates = Model.parseUpdatesJson(cap("updates.json"))
check("updates ok", updates.ok === true)
check("updates count", updates.updates.length === 2, JSON.stringify(updates.updates))
const embeddedUpd = updates.updates.find((u) => u.embeddedSource === true)
const staticUpd = updates.updates.find((u) => u.embeddedSource === false)
check("updates found both kinds", !!embeddedUpd && !!staticUpd)
check("updates id mapping", embeddedUpd.id === "neovim.desktop"
  && staticUpd.id === "secondapp.desktop")
check("updates version mapping", embeddedUpd.updateVersion === "v0.12.5"
  && staticUpd.updateVersion === "")
check("updates manager mapping", embeddedUpd.manager === "GithubUpdater"
  && staticUpd.manager === "StaticFileUpdater")
check("updates downloadSize mapping", embeddedUpd.downloadSize === 11442680
  && staticUpd.downloadSize === 10996235)
check("updates embedded update triple", embeddedUpd.updateVersion !== ""
  && embeddedUpd.manager !== "" && embeddedUpd.embeddedSource === true
  && embeddedUpd.downloadSize > 0)
// notified/offline only exist on --fetch-updates; --list-updates defaults false.
check("updates flags default", updates.offline === false && updates.notified === false)

// M1 items still normalize; the new keys default to their empty values.
const listed = Model.parseListJson(cap("list.json"))
check("list update keys default", listed.items[0].manager === ""
  && listed.items[0].embeddedSource === false && listed.items[0].downloadSize === 0)

// ---- F8: --fetch-updates --json -----------------------------------------------
const fetched = Model.parseUpdatesJson(cap("fetch-updates.json"))
check("fetch ok", fetched.ok === true && fetched.updates.length === 2)
check("fetch flags", fetched.notified === true && fetched.offline === false)
// offline passthrough stays covered by an inline literal (the real capture
// was taken online; offline capture files would freeze a port-dependent URL).
const offline = Model.parseUpdatesJson('{"schema_version":1,"updates":[],"notified":false,"offline":true}')
check("fetch offline flagged", offline.ok === true && offline.offline === true
  && offline.updates.length === 0)

// applyUpdates merge: the sweep result is authoritative — every app it
// reports gains hasUpdate (the badge must tint even for versionless
// static-source updates, which the old updateVersion-based count missed),
// and ids the sweep stopped reporting fall back to no update. Sweep
// metadata is also fresher than the list cache, so manager/embeddedSource/
// downloadSize refresh on reported apps; absent apps keep theirs.
// The store is seeded with copies: in the real flow --list-installed and
// the sweep parse into separate objects, and the freshness check below
// must not mutate the sweep doc it merges.
Model.setItems(updates.updates.map((u) => Object.assign({}, u)))
check("items seeded hasUpdate false", Model.findItem("neovim.desktop").hasUpdate === false
  && Model.findItem("secondapp.desktop").hasUpdate === false)
check("updateCount seeded", Model.updateCount() === 0,
  "parsed items carry no update state until a sweep is applied")
// Stale list-cache data on a reported app is overwritten by the sweep doc.
Model.findItem("neovim.desktop").manager = "SomethingStale"
Model.findItem("neovim.desktop").embeddedSource = false
Model.findItem("neovim.desktop").downloadSize = 0
Model.applyUpdates(updates.updates)
check("applyUpdates hasUpdate both kinds", Model.findItem("neovim.desktop").hasUpdate === true
  && Model.findItem("secondapp.desktop").hasUpdate === true)
check("updateCount counts versionless static update", Model.updateCount() === 2,
  "static secondapp.desktop has updateVersion \"\" but a real pending update")
check("applyUpdates refreshes stale source metadata", Model.findItem("neovim.desktop").manager === "GithubUpdater"
  && Model.findItem("neovim.desktop").embeddedSource === true
  && Model.findItem("neovim.desktop").downloadSize === 11442680)
check("applyUpdates version marker stays version-only", Model.findItem("secondapp.desktop").updateVersion === "")
Model.applyUpdates([embeddedUpd])
check("applyUpdates present id kept", Model.findItem("neovim.desktop").updateVersion === "v0.12.5"
  && Model.findItem("neovim.desktop").manager === "GithubUpdater"
  && Model.findItem("neovim.desktop").hasUpdate === true)
check("applyUpdates absent id cleared", Model.findItem("secondapp.desktop").updateVersion === ""
  && Model.findItem("secondapp.desktop").hasUpdate === false)
check("applyUpdates absent id keeps manager", Model.findItem("secondapp.desktop").manager === "StaticFileUpdater")
check("updateCount after merge", Model.updateCount() === 1)
Model.applyUpdates([])
check("applyUpdates empty sweep clears", Model.updateCount() === 0
  && Model.findItem("neovim.desktop").hasUpdate === false
  && Model.findItem("neovim.desktop").updateVersion === "")
check("applyUpdates empty sweep keeps manager", Model.findItem("neovim.desktop").manager === "GithubUpdater")

// ---- F8/F9: --settings / --set-setting ----------------------------------------
const settings = Model.parseSettingsJson(cap("settings.json"))
check("settings ok", settings.ok === true)
check("settings mapped", settings.settings.enabled === true
  && settings.settings.intervalMinutes === 360 && settings.settings.delayMinutes === 5)

// Tolerant defaults: missing/null keys fall back (360/5/on) instead of 0.
const tolerantSettings = Model.parseSettingsJson(
  '{"schema_version":1,"settings":{"update_check_enabled":false,"update_check_interval_minutes":null}}')
check("settings tolerant defaults", tolerantSettings.ok === true
  && tolerantSettings.settings.enabled === false
  && tolerantSettings.settings.intervalMinutes === 360
  && tolerantSettings.settings.delayMinutes === 5)

// Floors mirror --set-setting: interval min 15, delay min 0.
const flooredSettings = Model.parseSettingsJson(
  '{"settings":{"update_check_interval_minutes":5,"update_check_delay_minutes":-3}}')
check("settings floors", flooredSettings.settings.intervalMinutes === 15
  && flooredSettings.settings.delayMinutes === 0)

Model.applySettings(settings.settings)
check("applySettings stores", Model.state.settings.enabled === true
  && Model.state.settings.intervalMinutes === 360 && Model.state.settings.delayMinutes === 5)
const revisionAfterFirst = Model.state.settingsRevision
Model.applySettings({ enabled: false, intervalMinutes: 60, delayMinutes: 0 })
check("applySettings bumps revision", Model.state.settingsRevision === revisionAfterFirst + 1
  && Model.state.settings.enabled === false)

// ---- F9: update-source manager metadata (settings UI contract) -----------------
const expectedManagerKeys = {
  StaticFileUpdater: ["url"],
  GithubUpdater: ["repo", "repo_filename", "allow_prereleases"],
  GitlabUpdater: ["repo_url", "repo_filename"],
  CodebergUpdater: ["repo", "repo_filename", "allow_prereleases"],
  ForgejoUpdater: ["repo_url", "repo_filename", "allow_prereleases"]
}
let managersOk = Model.MANAGERS.length === Object.keys(expectedManagerKeys).length
for (const mgr of Model.MANAGERS) {
  if (!mgr.name || !mgr.labelKey || Model.strings[mgr.labelKey] === undefined) managersOk = false
  const keys = mgr.fields.map((f) => f.key)
  if (JSON.stringify(keys) !== JSON.stringify(expectedManagerKeys[mgr.name])) managersOk = false
  if (!mgr.fields.every((f) => f.type === "string" || f.type === "bool")) managersOk = false
}
check("MANAGERS match contract", managersOk)
const byName = Model.managersByName()
check("managersByName", byName.GithubUpdater.fields.length === 3
  && byName.StaticFileUpdater.fields[0].key === "url")
check("fieldLabel", Model.fieldLabel("allow_prereleases") === "Allow prereleases"
  && Model.fieldLabel("mystery_key") === "mystery_key")

// ---- M2: --update / --set-update-source outcomes -------------------------------
// Real capture: the nvim install (custom StaticFileUpdater over its embedded
// source) updated against a local server serving the same release padded —
// same current version, bytes differ.
const updatedDoc = Model.parseActionJson(cap("update-updated.json"))
check("update updated result", updatedDoc.ok === true && updatedDoc.result === "updated")
check("update updated app mapped", updatedDoc.app && updatedDoc.app.id === "neovim.desktop"
  && updatedDoc.app.version === "v0.11.3" && updatedDoc.app.updateVersion === "")
check("update downloaded bytes", Backend.parseJson(cap("update-updated.json")).downloaded_bytes
  === 10996235)

const upToDateDoc = Model.parseActionJson(cap("update-up-to-date.json"))
check("update up-to-date passthrough", upToDateDoc.ok === true && upToDateDoc.result === "up-to-date")
check("update up-to-date rechecked source", upToDateDoc.app && upToDateDoc.app.manager === "StaticFileUpdater"
  && upToDateDoc.app.downloadSize === 10996235)

const skippedDoc = Model.parseActionJson(cap("update-skipped-running.json"))
check("update skipped-running passthrough", skippedDoc.ok === true
  && skippedDoc.result === "skipped-running" && skippedDoc.message.includes("running"))
check("update skipped-running app mapped", skippedDoc.app
  && skippedDoc.app.id === "neovim.desktop" && skippedDoc.app.running === true)

const setSourceDoc = Model.parseActionJson(cap("set-source.json"))
check("set-source passthrough", setSourceDoc.ok === true && setSourceDoc.result === "set"
  && setSourceDoc.app.manager === "StaticFileUpdater" && setSourceDoc.app.downloadSize === 0)

const unsetSourceDoc = Model.parseActionJson(cap("unset-source.json"))
check("unset-source passthrough", unsetSourceDoc.ok === true && unsetSourceDoc.result === "unset"
  && unsetSourceDoc.app.manager === "" && unsetSourceDoc.app.downloadSize === 0)

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

console.log(failures === 0 ? "\nALL PASS" : "\n" + failures + " FAILURES")
process.exit(failures === 0 ? 0 : 1)

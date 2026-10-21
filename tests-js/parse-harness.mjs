// Harness: feeds real backend/main.py output (captured in tests-js/captures/
// during an e2e run) through the exact QML-side JS parse logic
// (lib/Model.js + lib/Backend.js).
// Run: node tests-js/parse-harness.mjs
import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
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

// ---- runtime block on the installed-list envelope (fuse2 probe) -------------
// CONTRACT.md (--list-installed): every successful JSON envelope carries an
// additive `runtime` block, { fuse2: "present" | "missing" | "unknown" }.
// The mapping is defensive: absent or non-object blocks and foreign values
// all normalize to "unknown" (the contract's "no claim" — never an error),
// and unknown keys anywhere are tolerated and ignored. The frozen capture
// above predates the block, so it doubles as the absence case.
check("runtime block absent → unknown", !!list.runtime
  && list.runtime.fuse2 === "unknown", JSON.stringify(list.runtime))
const runtimeOf = (doc) => Model.parseListJson(doc).runtime.fuse2
check("runtime present", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":{"fuse2":"present"}}') === "present")
check("runtime missing", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":{"fuse2":"missing"}}') === "missing")
check("runtime unknown passthrough", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":{"fuse2":"unknown"}}') === "unknown")
check("runtime foreign value → unknown", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":{"fuse2":"banana"}}') === "unknown")
check("runtime block not an object → unknown", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":"present"}') === "unknown")
check("runtime block null → unknown", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":null}') === "unknown")
check("runtime array form → unknown", runtimeOf(
  '{"schema_version":1,"installed":[],"runtime":["present"]}') === "unknown")
check("runtime unknown keys tolerated", runtimeOf(
  '{"schema_version":1,"installed":[],'
  + '"runtime":{"fuse2":"missing","probe":"ldconfig","extra":[1,2]}}') === "missing")
check("envelope unknown keys tolerated", runtimeOf(
  '{"schema_version":1,"installed":[],'
  + '"runtime":{"fuse2":"present"},"future_key":{"x":1}}') === "present")
check("bare array envelope → runtime unknown", runtimeOf('[{"name":"X"}]') === "unknown")

// The store keeps the latest listing's verdict, from every listing surface.
// A listing without the block (older backend) resets it, so a stale
// "missing" can never survive into a hint.
Model.setItems(list.items, Model.parseListJson(
  '{"schema_version":1,"installed":[],"runtime":{"fuse2":"missing"}}').runtime)
check("setItems stores runtime", Model.state.runtime.fuse2 === "missing")
Model.setItems(list.items, list.runtime)
check("setItems keeps unknown runtime", Model.state.runtime.fuse2 === "unknown")

// ---- launch-failure banner (fuse2 hint) --------------------------------------
// A launch is only known to have failed when the post-launch settle listing
// says the app never came up (execDetached cannot report the AppImage
// runtime's own death — a type-2 AppImage without fuse2 exits right after
// spawn). Model.launchFailureBanner is that banner's pure text mapping:
// the fuse2 hint line is appended ONLY for fuse2 "missing" — present,
// unknown and any foreign state keep the banner about the failure alone.
// Text only: the hint names the library, never a command, never a button
// (repo wording rule; the security gates scan install-command sources).
const failedName = "Neovim (v0.11.3)"
const bannerBase = Model.launchFailureBanner(failedName, "present")
check("banner strings present", !!Model.strings.launchFailed && !!Model.strings.launchFuse2Hint)
check("banner base line names the app", bannerBase.indexOf(failedName) !== -1
  && bannerBase.indexOf("\n") === -1, JSON.stringify(bannerBase))
for (const quiet of ["present", "unknown", "banana", "", null, undefined]) {
  const base = Model.launchFailureBanner(failedName, quiet)
  check("banner no hint on " + String(quiet),
    base === bannerBase && base.indexOf("fuse2") === -1, JSON.stringify(base))
}
const hinted = Model.launchFailureBanner(failedName, "missing")
check("banner hint line on missing", hinted.split("\n").length === 2
  && hinted.split("\n")[0] === bannerBase
  && hinted.split("\n")[1].indexOf("fuse2") !== -1
  && hinted.split("\n")[1].indexOf("libfuse.so.2") !== -1, JSON.stringify(hinted))
check("banner is text only", hinted.indexOf("/") === -1 && hinted.indexOf("`") === -1
  && hinted.indexOf("$") === -1 && hinted.indexOf("=") === -1, JSON.stringify(hinted))
check("banner tolerates an empty name", Model.launchFailureBanner("", "missing")
  .split("\n")[0] === Model.formatCount("", Model.strings.launchFailed))

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

// ---- #18: --preview-integrate document mapping -------------------------------
// The picker's confirm-before-integrate flow maps the offline preview
// document through Model.parsePreviewJson. Both captures are real
// `--preview-integrate --json` output. The panel renders appName/appVersion
// (null app → its "unknown" copy), the destination (full path for local
// files, folder-only for URLs) and the verification note, and rides its
// prominent reduced-assurance / already-integrated / name-conflict
// treatments on exactly the flags below.
const previewLocal = Model.parsePreviewJson(cap("preview-local.json"))
check("preview local ok", previewLocal.ok === true)
check("preview local source", previewLocal.preview.sourceKind === "local"
  && previewLocal.preview.sourceInput.indexOf("escrcpy.appimage") !== -1)
check("preview local app", previewLocal.preview.appName === "Escrcpy (3.3.1)"
  && previewLocal.preview.appVersion === "3.3.1")
check("preview local destination path", previewLocal.preview.destinationPath
  === "/home/glasschan/AppImages/escrcpy.appimage")
check("preview local verification", previewLocal.preview.verificationMethod === "none"
  && previewLocal.preview.reducedAssurance === false
  && previewLocal.preview.verificationNote.includes("magic-byte"))
check("preview local already integrated", previewLocal.preview.alreadyIntegrated === true
  && previewLocal.preview.nameConflict === false)
check("preview advisory flag", previewLocal.preview.advisory === true)

const previewUrl = Model.parsePreviewJson(cap("preview-url.json"))
check("preview url ok", previewUrl.ok === true)
check("preview url github kind", previewUrl.preview.sourceKind === "github")
check("preview url app null → unknown", previewUrl.preview.appName === ""
  && previewUrl.preview.appVersion === "")
check("preview url destination folder only", previewUrl.preview.destinationFolder !== ""
  && previewUrl.preview.destinationFilename === ""
  && previewUrl.preview.destinationPath === "")
check("preview url github-sha256 verified", previewUrl.preview.verificationMethod === "github-sha256"
  && previewUrl.preview.reducedAssurance === false)

// gitlab/codeberg/forgejo/static publish no digest: reduced_assurance true —
// the prominent "will install with reduced assurance" line rides on this
// flag alone, and it shows BEFORE any download starts (the preview is
// offline).
const previewReduced = Model.parsePreviewJson(
  '{"schema_version": 1, "result": "preview", "source": {"kind": "static", "input": "https://host/app.AppImage"}, '
  + '"verification": {"method": "none", "reduced_assurance": true, "note": "no publisher digest exists for this source"}, '
  + '"destination": {"folder": "/h/AppImages", "filename": null, "path": null}, '
  + '"app": null, "already_integrated": false, "name_conflict": false, "advisory": true}')
check("preview reduced assurance flagged", previewReduced.ok === true
  && previewReduced.preview.reducedAssurance === true
  && previewReduced.preview.verificationMethod === "none")

// Another version of the same app installed: integrate keeps both (the
// derived name is version-suffixed) — the panel notices from this flag.
const previewConflict = Model.parsePreviewJson(
  '{"schema_version": 1, "result": "preview", "source": {"kind": "local", "input": "/h/dl/app-2.AppImage"}, '
  + '"verification": {"method": "none", "reduced_assurance": false, "note": "n"}, '
  + '"destination": {"folder": "/h/AppImages", "filename": "app-2.appimage", "path": "/h/AppImages/app-2.appimage"}, '
  + '"app": {"name": "App", "version": "2.0"}, "already_integrated": false, "name_conflict": true, "advisory": true}')
check("preview name conflict flagged", previewConflict.ok === true
  && previewConflict.preview.nameConflict === true
  && previewConflict.preview.alreadyIntegrated === false)

// Unknown-key tolerance (contract: keys are additive within schema_version
// 1): future keys anywhere are ignored, missing keys normalize to defaults,
// null sub-fields become empty strings — never crashes, never undefined.
const previewFuture = Model.parsePreviewJson(
  '{"schema_version": 2, "result": "preview", "future_top": {"x": 1}, '
  + '"source": {"kind": "zsync", "input": "https://host/app.zsync", "future_src": true}, '
  + '"verification": {"method": "zsync-sha1", "reduced_assurance": false, "future_verf": 1}, '
  + '"destination": {"folder": "/h/AppImages", "future_dest": null}, '
  + '"already_integrated": false, "name_conflict": false, "advisory": true, "future_flag": "yes"}')
check("preview unknown keys tolerated", previewFuture.ok === true
  && previewFuture.preview.sourceKind === "zsync"
  && previewFuture.preview.destinationFilename === ""
  && previewFuture.preview.appName === "")
check("preview missing keys default", previewFuture.preview.verificationNote === ""
  && previewFuture.preview.destinationPath === ""
  && previewFuture.preview.reducedAssurance === false
  && previewFuture.preview.alreadyIntegrated === false
  && previewFuture.preview.nameConflict === false)

// Documents that must never render as a preview: the backend's error
// document (what a bad path returns, exit 1), garbage stdout, and a
// well-formed JSON that is not a preview document. All fail with a
// message so the panel can degrade to a status + usable picker.
const previewError = Model.parsePreviewJson(
  '{"schema_version": 1, "result": "error", "error": "no such file: /x"}')
check("preview error document rejected", previewError.ok === false
  && previewError.preview === null && previewError.error !== "")
check("preview garbage rejected", Model.parsePreviewJson("python3: not found\n").ok === false)
const previewWrongShape = Model.parsePreviewJson(
  '{"schema_version": 1, "result": "integrated", "message": "x"}')
check("preview non-preview document rejected", previewWrongShape.ok === false
  && previewWrongShape.preview === null)

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

// ---- F2 browse: --pick-file outcome documents (#19) -----------------------------
// Contract shapes (backend/CONTRACT.md "### --pick-file [--json]"): picked /
// cancelled / unavailable are exit-0 results, typed errors are exit-1
// documents with a reason key. No captures here — every variant needs a
// real dialog session (a portal reply, a user cancel), so they are inline
// literals, same as the offline fetch-updates case above.
const pickedPortal = Model.parsePickJson(
  '{"schema_version": 1, "result": "picked", "path": "/home/user/Downloads/nvim-linux-x86_64.appimage", "backend": "portal"}')
check("pick picked ok", pickedPortal.ok === true && pickedPortal.result === "picked")
check("pick picked path", pickedPortal.path === "/home/user/Downloads/nvim-linux-x86_64.appimage",
  pickedPortal.path)
check("pick picked backend", pickedPortal.backend === "portal", pickedPortal.backend)

const pickedZenity = Model.parsePickJson(
  '{"schema_version": 1, "result": "picked", "path": "/opt/x.AppImage", "backend": "zenity"}')
check("pick zenity backend", pickedZenity.ok === true && pickedZenity.backend === "zenity"
  && pickedZenity.path === "/opt/x.AppImage", JSON.stringify(pickedZenity))

const cancelled = Model.parsePickJson(
  '{"schema_version": 1, "result": "cancelled", "backend": "zenity"}')
check("pick cancelled ok", cancelled.ok === true && cancelled.result === "cancelled")
check("pick cancelled has no path", cancelled.path === "", cancelled.path)

const unavailable = Model.parsePickJson(
  '{"schema_version": 1, "result": "unavailable", "message": '
  + '"no file chooser is available (neither the desktop portal nor zenity); '
  + 'type or paste the path instead"}')
check("pick unavailable ok", unavailable.ok === true && unavailable.result === "unavailable")
check("pick unavailable message surfaced", unavailable.message.includes("type or paste"),
  unavailable.message)

const pickTimeout = Model.parsePickJson(
  '{"schema_version": 1, "result": "error", "reason": "timeout", '
  + '"error": "the file dialog did not answer in time"}')
check("pick timeout error", pickTimeout.ok === false && pickTimeout.result === "error"
  && pickTimeout.reason === "timeout")
check("pick timeout message surfaced", pickTimeout.error.includes("did not answer in time"),
  pickTimeout.error)

const pickBackendFailed = Model.parsePickJson(
  '{"schema_version": 1, "result": "error", "reason": "backend-failed", '
  + '"error": "the file dialog crashed"}')
check("pick backend-failed reason", pickBackendFailed.ok === false
  && pickBackendFailed.reason === "backend-failed")

const pickNotLocal = Model.parsePickJson(
  '{"schema_version": 1, "result": "error", "reason": "not-local", '
  + '"error": "the file dialog returned a non-local location"}')
check("pick not-local reason", pickNotLocal.ok === false && pickNotLocal.reason === "not-local")

// Defensive mapping: unknown envelope keys are tolerated, unknown results
// pass through like parseActionJson's (the QML layer routes on the named
// results and treats everything else as a failure), and garbage is rejected.
const pickUnknownKeys = Model.parsePickJson(
  '{"schema_version": 1, "result": "picked", "path": "/x/a.appimage", '
  + '"backend": "portal", "future_key": {"nested": true}}')
check("pick unknown keys tolerated", pickUnknownKeys.ok === true
  && pickUnknownKeys.path === "/x/a.appimage", JSON.stringify(pickUnknownKeys))
const pickUnknownResult = Model.parsePickJson(
  '{"schema_version": 1, "result": "something-new"}')
check("pick unknown result passthrough", pickUnknownResult.ok === true
  && pickUnknownResult.result === "something-new")
check("pick garbage rejected", Model.parsePickJson("zenity: not found\n").ok === false
  && Model.parsePickJson("zenity: not found\n").error !== "")

// ---- Drop-to-integrate: dropUriToPath (#21) ----------------------------------
// The bar widget's and the panel's DropAreas hand their URI lists through
// this pure helper before anything touches the picker: only file:// URIs
// are usable (URL-text drops are out of scope by the spec), the local path
// is percent-decoded, a multi-URI selection picks the FIRST entry ending
// in .AppImage (case-insensitive) and reports the rest as ignored, and
// every unusable input is a typed rejection (reason "empty" / "not-file" /
// "no-appimage") — a drop can never manufacture an install path.
const drop = (uris) => Model.dropUriToPath(uris)

const dropOk = drop(["file:///home/u/Tools/My.AppImage"])
check("drop valid file URI", dropOk.ok === true
  && dropOk.path === "/home/u/Tools/My.AppImage"
  && dropOk.ignored === 0 && dropOk.reason === "", JSON.stringify(dropOk))

const dropEncoded = drop(["file:///home/u/My%20Tools/App%20One.appimage"])
check("drop URL-encoded path decodes", dropEncoded.ok === true
  && dropEncoded.path === "/home/u/My Tools/App One.appimage", JSON.stringify(dropEncoded))
check("drop lowercase extension accepted", /\.appimage$/i.test(dropEncoded.path))

const dropString = drop("file:///x/Solo.AppImage")
check("drop single string input accepted", dropString.ok === true
  && dropString.path === "/x/Solo.AppImage", JSON.stringify(dropString))

check("drop empty list → empty", JSON.stringify(drop([]))
  === JSON.stringify({ ok: false, path: "", ignored: 0, reason: "empty" }))
check("drop empty string → empty", drop("").ok === false && drop("").reason === "empty")
check("drop whitespace-only entries → empty",
  drop(["", "   "]).ok === false && drop(["", "   "]).reason === "empty")

check("drop bare-root URI → no-appimage",
  drop(["file:///"]).ok === false && drop(["file:///"]).reason === "no-appimage")
check("drop trailing-slash dir URI → no-appimage",
  drop(["file:///home/u/dir/"]).ok === false
  && drop(["file:///home/u/dir/"]).reason === "no-appimage")

check("drop https rejected", drop(["https://example.com/app.AppImage"]).ok === false
  && drop(["https://example.com/app.AppImage"]).reason === "not-file")
check("drop http rejected", drop(["http://example.com/app.AppImage"]).ok === false
  && drop(["http://example.com/app.AppImage"]).reason === "not-file")
check("drop plain text garbage rejected", drop(["hello world"]).ok === false
  && drop(["hello world"]).reason === "not-file")
check("drop file://host path rejected (not a local path)",
  drop(["file://server/share/App.AppImage"]).ok === false
  && drop(["file://server/share/App.AppImage"]).reason === "not-file")
check("drop malformed percent rejected", drop(["file:///a/%zz.AppImage"]).ok === false
  && drop(["file:///a/%zz.AppImage"]).reason === "not-file")

const dropMulti = drop([
  "file:///home/u/notes.txt",
  "file:///home/u/First%20App.AppImage",
  "file:///home/u/Second.AppImage",
  "https://example.com/third.AppImage"
])
check("drop multi-URI first-AppImage policy", dropMulti.ok === true
  && dropMulti.path === "/home/u/First App.AppImage"
  && dropMulti.ignored === 3 && dropMulti.reason === "", JSON.stringify(dropMulti))

const dropNone = drop(["file:///home/u/notes.txt", "file:///home/u/readme.md"])
check("drop no-AppImage list typed", dropNone.ok === false
  && dropNone.reason === "no-appimage" && dropNone.path === ""
  && dropNone.ignored === 2, JSON.stringify(dropNone))

// ---- Panel-side error mapping (mirrors reportFailure branches) ---------------
// Timeout is its own message ("Backend timed out") — blaming python3 for a
// slow update sent users hunting the wrong fix. A spawn failure only maps to
// the python hint when the probe has not already confirmed python missing
// (then the install card owns the screen and the banner is cleared).
function reportFailureLike(result, pythonMissing) {
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

// ---- Backend.js streamed producer bound (security round 2) -----------------
// StdioCollector.onRead feeds every chunk through Backend.feedStream; past
// the per-stream cap the child is killed via proc.running = false (same
// kill path as handleTimeout — Quickshell Process has no terminate()).
function fakeProc() {
  return {
    _v: false,
    killCount: 0,
    set running(v) { if (v === false && this._v === true) this.killCount++; this._v = v },
    get running() { return this._v }
  }
}
const fakeTimer = { interval: 0, restarted: false, stopped: false,
  restart() { this.restarted = true }, stop() { this.stopped = true } }
let doneResults = []
const onDone = (r) => doneResults.push(r)

// ctx accounting fields + options override
{
  const proc = fakeProc()
  const ctx = Backend.run(proc, fakeTimer, ["python3", "main.py"], ["--json"], { maxStreamChars: 10 }, onDone)
  check("run ctx stream accounting", ctx.stdoutLen === 0 && ctx.stderrLen === 0
    && ctx.overflow === false, JSON.stringify(ctx))
  check("run maxStreamChars override", ctx.maxStreamChars === 10, String(ctx.maxStreamChars))
  const ctxDefault = Backend.run(fakeProc(), fakeTimer, ["python3"], [], {}, onDone)
  check("run maxStreamChars default", ctxDefault.maxStreamChars === Backend.MAX_STREAM_CHARS,
    String(ctxDefault.maxStreamChars))

  // chunks under the cap accumulate; no kill
  Backend.feedStream(ctx, "stdout", "aaaaaaaaaa") // exactly 10
  check("feedStream under cap no kill", ctx.stdoutLen === 10
    && ctx.overflow === false && proc.killCount === 0, JSON.stringify(ctx))
  // the chunk that pushes past the cap flips overflow, stops the
  // watchdog (so the timer can never fire handleTimeout and misattribute
  // the failure as "timeout") and kills the child exactly once
  Backend.feedStream(ctx, "stdout", "bbbb")
  check("feedStream past cap kills once + stops watchdog", ctx.overflow === true
    && proc.killCount === 1 && proc.running === false && fakeTimer.stopped === true,
    JSON.stringify(ctx))
  // later chunks never double-kill
  Backend.feedStream(ctx, "stderr", "err!!")
  check("feedStream overflow once only", proc.killCount === 1 && ctx.stderrLen === 5,
    JSON.stringify(ctx))
  // null ctx is a safe no-op
  let threw = false
  try { Backend.feedStream(null, "stdout", "ignored") } catch (e) { threw = true }
  check("feedStream null ctx no-op", threw === false)

  // with the watchdog already stopped, the real process exit reaches
  // handleExit, which attributes the failure to the overflow — not a
  // timeout — exactly once
  Backend.handleExit(ctx, 0, "truncated-garbage", "stderr-leftover")
  Backend.handleExit(ctx, 0, "truncated-garbage", "stderr-leftover") // late exit ignored
  const r = doneResults[0]
  check("handleExit overflow shape", r && r.ok === false && r.json === null
    && r.timedOut === false && r.spawnFailed === true && r.error === "output-overflow",
    JSON.stringify(r))
  check("handleExit overflow clamp + once", r.stdout === "truncated-garbage"
    && r.stderr === "stderr-leftover" && doneResults.length === 1,
    JSON.stringify(doneResults))
}

// ---- Python dependency card (authorized in-panel install) ---------------------
// All machinery is pure string/argv building plus one exit-code mapper, so
// the whole flow is pinned here without spawning anything. The real flow:
// probe → (missing) card → button → detached `omarchy launch floating
// terminal with presentation <command>` → marker files → 1 s probe poll.
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

// ---- Live backend: exec_arguments + the stale-record path --------------------
// The captures above are frozen e2e output. The two behaviours pinned here
// have no capture (a stale record cannot be produced by a normal run), so
// the REAL backend runs in a throwaway XDG sandbox and its output goes
// through the same JS parse logic as every other document.
const MAIN_PY = new URL("../backend/main.py", import.meta.url).pathname
// ELF magic + the AppImage type-2 magic: enough for the listing to accept
// the file (nothing is ever executed).
const FAKE_APPIMAGE = Buffer.concat([
  Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01, 0x01, 0x00, 0x41, 0x49, 0x02]),
  Buffer.from("padding")
])
// Minimal type-2 AppImage for the preview verb (--preview-integrate runs
// the ELF arch probe, so the header must be a full ELF64 header with the
// AppImage magic at offset 8). No embedded squashfs/desktop entry — exactly
// the contract's "app is null, the preview stays valid" case.
const PREVIEW_APPIMAGE = (() => {
  const header = Buffer.alloc(64)
  Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01, 0x01, 0x00]).copy(header, 0)
  header[8] = 0x41; header[9] = 0x49; header[10] = 0x02
  header.writeUInt16LE(0x3e, 0x12) // e_machine = x86-64
  return Buffer.concat([header, Buffer.from("no-desktop-entry")])
})()

const sandbox = mkdtempSync(join(tmpdir(), "appimage-harness-"))
const home = join(sandbox, "home")
const dataHome = join(sandbox, "data")
const configHome = join(sandbox, "config")
const managed = join(home, "AppImages")
const applications = join(dataHome, "applications")
mkdirSync(join(managed, ".icons"), { recursive: true })
mkdirSync(applications, { recursive: true })

const writeApp = (name, execArgs) => {
  const appimage = join(managed, name + ".appimage")
  writeFileSync(appimage, FAKE_APPIMAGE)
  const icon = join(managed, ".icons", name + ".png")
  writeFileSync(icon, Buffer.from([0x89, 0x50, 0x4e, 0x47]))
  const args = execArgs ? " " + execArgs : ""
  writeFileSync(join(applications, name + ".desktop"),
    `[Desktop Entry]\nName=${name}\n`
    + `Exec=env DESKTOPINTEGRATION=1 ${appimage}${args}\n`
    + `TryExec=${appimage}\nIcon=${icon}\nType=Application\nTerminal=false\n`
    + `X-AppImage-Version=1.0\n`)
  return { appimage, icon }
}

writeApp("biscuit", "--no-sandbox %U")
writeApp("plain", "")
// a stale record: the .desktop file is gone, the AppImage is not
const stale = writeApp("stale", "--no-sandbox %U")
const staleDesktop = join(applications, "stale.desktop")
rmSync(staleDesktop)

const configDir = join(configHome, "io.github.glasschan.appimage")
mkdirSync(configDir, { recursive: true })
const appsIni = join(configDir, "apps.ini")
writeFileSync(appsIni,
  `[app.${createHash("md5").update(stale.appimage).digest("hex")}]\n`
  + `default_exec_arguments = --no-sandbox %U\n`
  + `appimage_path = ${stale.appimage}\n`
  + `desktop_file_path = ${staleDesktop}\n`
  + `icon_path = ${stale.icon}\n`
  + `name = Stale App\n`
  + `file_path = ${stale.appimage}\n`)

const sandboxEnv = Object.assign({}, process.env, {
  HOME: home, XDG_DATA_HOME: dataHome, XDG_CONFIG_HOME: configHome
})
const runBackend = (args) => spawnSync("python3", [MAIN_PY].concat(args),
  { env: sandboxEnv, encoding: "utf8" })

const listRun = runBackend(["--list-installed", "--json"])
const liveOk = !listRun.error
check("live backend runs (python3 present)", liveOk,
  liveOk ? "" : String(listRun.error))
if (liveOk) {
  check("live backend --list-installed exits 0", listRun.status === 0,
    listRun.stderr)
  const liveDoc = Backend.parseJson(listRun.stdout)
  const liveApps = liveDoc && Array.isArray(liveDoc.installed)
    ? liveDoc.installed : []
  const argsApp = liveApps.find((a) => a.desktop_id === "biscuit.desktop")
  const noArgsApp = liveApps.find((a) => a.desktop_id === "plain.desktop")

  // the new key: the desktop entry's Exec arguments reach the UI
  check("exec_arguments present for an app with arguments",
    !!argsApp && argsApp.exec_arguments === "--no-sandbox %U",
    JSON.stringify(argsApp))
  check("exec_arguments empty for an app without arguments",
    !!noArgsApp && noArgsApp.exec_arguments === "", JSON.stringify(noArgsApp))
  check("exec_arguments is a string on every app object",
    liveApps.length === 2 && liveApps.every((a) => typeof a.exec_arguments === "string"),
    JSON.stringify(liveApps))
  // the field codes stay verbatim — the UI strips them
  check("exec_arguments keeps the field codes verbatim",
    !!argsApp && argsApp.exec_arguments.indexOf("%U") !== -1)

  // the JS parse logic still handles the live document
  const liveList = Model.parseListJson(listRun.stdout)
  check("live backend document parses in Model", liveList.ok === true
    && liveList.items.length === 2, JSON.stringify(liveList.items))

  // The merged fuse2 probe (#17): every live listing carries the additive
  // runtime block with exactly the contract's states, and the Model maps it
  // through untouched (the host's real fuse2 state is never depended on).
  check("live backend carries the runtime block", !!liveDoc
    && !!liveDoc.runtime && typeof liveDoc.runtime === "object"
    && !Array.isArray(liveDoc.runtime)
    && ["present", "missing", "unknown"].indexOf(liveDoc.runtime.fuse2) !== -1,
    JSON.stringify(liveDoc && liveDoc.runtime))
  check("live runtime block maps in Model", liveList.ok === true
    && ["present", "missing", "unknown"].indexOf(liveList.runtime.fuse2) !== -1,
    JSON.stringify(liveList.runtime))
  check("live setItems stores the mapped runtime",
    (Model.setItems(liveList.items, liveList.runtime),
      ["present", "missing", "unknown"].indexOf(Model.state.runtime.fuse2) !== -1))

  // the stale record: no crash, and it is REPORTED, not silently dropped
  check("stale record does not crash the listing", listRun.status === 0)
  check("stale record is not listed as installed",
    !liveApps.some((a) => a.desktop_id === "stale.desktop"))
  check("stale record is reported with both paths",
    listRun.stderr.includes(staleDesktop)
    && listRun.stderr.includes(stale.appimage), listRun.stderr)
  check("listing never removes the stale record", existsSync(stale.appimage))

  // ...and it can be cleaned up explicitly, by its desktop id
  const removeRun = runBackend(["--remove", "stale.desktop", "--yes", "--json"])
  check("stale record can be removed by its desktop id",
    removeRun.status === 0, removeRun.stderr)
  const removedDoc = Model.parseActionJson(removeRun.stdout)
  check("stale removal reports removed",
    removedDoc.ok === true && removedDoc.result === "removed")
  check("stale removal trashes the orphaned AppImage",
    existsSync(join(dataHome, "Trash", "files", "stale.appimage")))
  check("stale removal deletes the record",
    readFileSync(appsIni, "utf8").indexOf("stale") === -1,
    readFileSync(appsIni, "utf8"))

  // #18: the preview verb through the same live plumbing. The fixture has
  // no embedded desktop entry, so app is null while the preview stays
  // valid (CONTRACT.md) — the panel renders its "unknown" copy for it.
  const previewFixture = join(home, "preview-fixture.appimage")
  writeFileSync(previewFixture, PREVIEW_APPIMAGE)
  const previewRun = runBackend(["--preview-integrate", previewFixture, "--json"])
  check("live preview exits 0", previewRun.status === 0, previewRun.stderr)
  const livePreview = Model.parsePreviewJson(previewRun.stdout)
  check("live preview parses", livePreview.ok === true
    && livePreview.preview.sourceKind === "local"
    && livePreview.preview.alreadyIntegrated === false
    && livePreview.preview.nameConflict === false
    && livePreview.preview.appName === ""
    && livePreview.preview.appVersion === ""
    && livePreview.preview.reducedAssurance === false
    && livePreview.preview.verificationMethod === "none"
    && livePreview.preview.destinationFilename !== ""
    && livePreview.preview.destinationPath !== ""
    && livePreview.preview.advisory === true,
    JSON.stringify(livePreview))
}
// ---- #10: background update-job documents (lib/JobDocs.js) ------------------
// Captures in tests-js/captures/job-*.json are REAL backend documents,
// regenerated by backend/tests/export_job_captures.py (see its docstring
// for per-file provenance). Classification time is pinned to each
// capture's own heartbeat (+offset seconds), so the decision table is
// deterministic no matter when the harness runs.
const JobDocs = load(new URL("../lib/JobDocs.js", import.meta.url).pathname)

// Contract parity with the writer, read from jobs.py source: a backend
// constant change fails here until lib/JobDocs.js mirrors it.
const jobsPy = readFileSync(new URL("../backend/omarchy_appimage/jobs.py", import.meta.url), "utf8")
const staleFromSource = Number(jobsPy.match(/JOB_STALE_AFTER_SECONDS = (\d+)/)[1])
check("STALE_AFTER_SECONDS mirrors jobs.py",
  JobDocs.STALE_AFTER_SECONDS === staleFromSource && staleFromSource === 120)
const phasesFromSource = (name) => (jobsPy.match(new RegExp(name + " = \\(([^)]*)\\)")) || ["", ""])[1]
  .split(",").map((s) => s.trim().replace(/'/g, "")).filter(Boolean)
check("ACTIVE_PHASES mirror jobs.py",
  JSON.stringify(JobDocs.ACTIVE_PHASES) === JSON.stringify(phasesFromSource("ACTIVE_PHASES")),
  JSON.stringify(phasesFromSource("ACTIVE_PHASES")))
check("TERMINAL_PHASES mirror jobs.py",
  JSON.stringify(JobDocs.TERMINAL_PHASES) === JSON.stringify(phasesFromSource("TERMINAL_PHASES")),
  JSON.stringify(phasesFromSource("TERMINAL_PHASES")))
// The stem rule too (ticket #11): the reader's path admission must equal
// the writer's own character class. (Service.qml carries a second inline
// copy, out of this harness's reach — only lib/JobDocs.js is pinned here.)
const stemReFromSource = (jobsPy.match(/_JOB_STEM_RE = re\.compile\(r'([^']+)'\)/) || ["", ""])[1]
check("JOB_STEM_RE mirrors jobs.py _JOB_STEM_RE",
  stemReFromSource !== "" && JobDocs.JOB_STEM_RE.source === stemReFromSource,
  JobDocs.JOB_STEM_RE.source + " vs " + stemReFromSource)
check("every contract phase normalizes known, terminal only for done/error",
  JobDocs.ACTIVE_PHASES.concat(JobDocs.TERMINAL_PHASES).every((p) => {
    const j = JobDocs.normalizeJob({ phase: p })
    return j.phaseKnown === true && j.terminal === (p === "done" || p === "error")
  })
  && JobDocs.normalizeJob({ phase: "uploading" }).phaseKnown === false)

const jobDoc = (f) => JobDocs.parseJobDocument(cap(f)).doc
const at = (f, offsetSec) => jobDoc(f).heartbeat * 1000 + offsetSec * 1000
const adopt = (f, pidAlive, offsetSec) =>
  JobDocs.adoptDocument(cap(f), pidAlive, at(f, offsetSec === undefined ? 5 : offsetSec))

// -- active phases (pidAlive=true: the panel's heartbeat-only default) --------
{
  const d = adopt("job-starting.json", true)
  check("starting adopts active", d.decision.state === "active" && d.parseError === "")
  check("starting carries phase + zero progress", d.decision.phase === "starting"
    && d.decision.progress === 0 && d.decision.message === "starting"
    && d.decision.desktopId === "fakeapp.desktop" && d.decision.stale === false
    && d.decision.pidDead === false)

  const dl = adopt("job-downloading.json", true)
  check("downloading adopts active with progress", dl.decision.state === "active"
    && dl.decision.phase === "downloading" && dl.decision.progress === 0.42
    && dl.decision.message === "downloading update")
  check("downloading byte fields survive normalization",
    JobDocs.normalizeJob(jobDoc("job-downloading.json")).downloadedBytes
    === Math.round(0.42 * 11442680)
    && JobDocs.normalizeJob(jobDoc("job-downloading.json")).totalBytes === 11442680)

  const vf = adopt("job-verifying.json", true)
  check("verifying adopts active near top", vf.decision.state === "active"
    && vf.decision.phase === "verifying" && vf.decision.progress >= 0.999)

  const rp = adopt("job-replacing.json", true)
  check("replacing adopts active at progress 1", rp.decision.state === "active"
    && rp.decision.phase === "replacing" && rp.decision.progress === 1
    && rp.decision.message === "installing the downloaded release")

  // every active phase interrupts as soon as the caller's pid verdict says
  // dead — the pid probe is the CALLER's word, JS never guesses it
  check("downloading + pidAlive=false interrupts with pidDead",
    adopt("job-downloading.json", false).decision.state === "interrupted"
    && adopt("job-downloading.json", false).decision.pidDead === true
    && adopt("job-downloading.json", false).decision.stale === false)
  check("active phases tolerate missing pidAlive (heartbeat rule alone)",
    adopt("job-downloading.json").decision.state === "active")
}

// -- terminal documents (their own verdict; pid/heartbeat irrelevant) ---------
{
  const done = adopt("job-done-updated.json", false, 100000)
  check("done wins over stale heartbeat + dead pid", done.decision.state === "done"
    && done.decision.status === "updated" && done.decision.newVersion === "v0.11.3"
    && done.decision.progress === 1 && done.decision.message.includes("updated successfully"))
  check("done carries exact downloaded bytes", jobDoc("job-done-updated.json").downloaded_bytes
    === 10996216)

  const skip = adopt("job-done-skipped-running.json", true)
  check("skipped-running adopts done", skip.decision.state === "done"
    && skip.decision.status === "skipped-running" && skip.decision.newVersion === ""
    && skip.decision.message.includes("running"))

  const err = adopt("job-error.json", true)
  check("error adopts error with text", err.decision.state === "error"
    && err.decision.status === "error" && err.decision.errorText.includes("SHA-1")
    && err.decision.message !== "")
}

// -- derived interruption (stale heartbeat, dead pid) --------------------------
{
  const stale = adopt("job-stale-heartbeat.json", true, 310)
  check("stale heartbeat interrupts", stale.decision.state === "interrupted"
    && stale.decision.stale === true && stale.decision.pidDead === false
    && stale.decision.message === "downloading update")
  check("stale wins even with a live pid", adopt("job-stale-heartbeat.json", true, 310)
    .decision.state === "interrupted")

  const dead = adopt("job-dead-pid.json", false)
  check("dead pid interrupts on a fresh document", dead.decision.state === "interrupted"
    && dead.decision.pidDead === true && dead.decision.stale === false)
  // the same document with the caller vouching for the pid reads active:
  // heartbeat-only panels pass true and accept the <=120 s blind window
  check("dead-pid capture + pidAlive=true stays active (caller's word)",
    adopt("job-dead-pid.json", true).decision.state === "active")

  // …and the state both production readers actually pass: `null`, the
  // documented "cannot probe". Identical classification to `true` (no
  // behaviour change), but pidDead must stay unreachable — it means "a
  // real probe said dead", which a null caller never said.
  check("pidAlive=null classifies exactly like true (heartbeat alone)",
    adopt("job-downloading.json", null).decision.state === "active"
    && adopt("job-dead-pid.json", null).decision.state === "active")
  check("pidAlive=null never claims pidDead",
    adopt("job-dead-pid.json", null).decision.pidDead === false
    && adopt("job-downloading.json", null).decision.pidDead === false)
  check("pidAlive=null still interrupts on a stale heartbeat",
    adopt("job-stale-heartbeat.json", null, 310).decision.state === "interrupted"
    && adopt("job-stale-heartbeat.json", null, 310).decision.stale === true
    && adopt("job-stale-heartbeat.json", null, 310).decision.pidDead === false)
  check("adoptDocumentForPanel accepts the null probe state",
    JobDocs.adoptDocumentForPanel(cap("job-downloading.json"), null,
      at("job-downloading.json", 5)).decision.state === "active")
}

// -- malformed / truncated / empty: safe fallback, never an exception ----------
{
  for (const f of ["job-empty.json", "job-truncated.json", "job-malformed.json"]) {
    // not via adopt(): these documents have no doc to pin a clock to —
    // going straight through adoptDocument IS the corrupt-file path
    const d = JobDocs.adoptDocument(cap(f), true, Date.now())
    check(f + " falls back to interrupted", d.parseError !== ""
      && d.decision.state === "interrupted" && d.decision.progress === 0
      && d.decision.message === "" && d.decision.newVersion === "")
  }
  // wrong-type JSON documents parse as JSON but fail the object-shape gate
  // (mirror of read_job's isinstance(dict)) and classify interrupted too
  for (const text of ["[]", "[{\"phase\":\"done\"}]", "42", "null", "\"str\""]) {
    const d = JobDocs.adoptDocument(text, true, Date.now())
    check("wrong-type doc " + text + " → interrupted", d.decision.state === "interrupted")
  }

  // hostile-input sweep: nothing may throw, everything lands in the enum
  const activeCaptures = ["job-starting.json", "job-downloading.json", "job-verifying.json",
    "job-replacing.json", "job-done-updated.json", "job-done-skipped-running.json",
    "job-error.json", "job-stale-heartbeat.json", "job-dead-pid.json"].map(cap)
  const hostile = ["", "   ", "{", "}", '{"phase":', "\x00\x01", cap("job-truncated.json"),
    cap("job-malformed.json"), JSON.stringify({ phase: 7 }), JSON.stringify({ heartbeat: "x" }),
    JSON.stringify({ phase: "done", progress: "high" }), JSON.stringify(Array(64).fill("x"))]
  const states = ["idle", "active", "interrupted", "done", "error"]
  let threw = false, offEnum = false
  for (const text of hostile.concat(activeCaptures)) {
    try {
      const d = JobDocs.adoptDocument(text, false, 1e12)
      if (!states.includes(d.decision.state)) offEnum = true
    } catch (e) { threw = true }
  }
  check("hostile documents never throw", threw === false)
  check("every decision lands in the state enum", offEnum === false)
}

// -- normalization edges + the classification boundary -------------------------
{
  const now = 1792494500000
  const mk = (over) => JobDocs.normalizeJob(Object.assign({
    schema_version: 1, desktop_id: "x.desktop", phase: "downloading",
    progress: 0.5, total_bytes: 1000, downloaded_bytes: 500, pid: 4242,
    started: 1792494000, heartbeat: 1792494400, message: "m"
  }, over))
  check("unknown phase fails safe to interrupted (deliberate divergence from classify_job)",
    JobDocs.classifyJob(mk({ phase: "uploading" }), now, true) === "interrupted")
  check("missing phase fails safe too",
    JobDocs.classifyJob(mk({ phase: undefined }), now, true) === "interrupted")
  check("heartbeat boundary is inclusive at 120s (backend <= rule)",
    JobDocs.classifyJob(mk({}), mk({}).heartbeat * 1000 + 120 * 1000, true) === "active"
    && JobDocs.classifyJob(mk({}), mk({}).heartbeat * 1000 + 121 * 1000, true) === "interrupted")
  check("missing heartbeat can never be fresh",
    JobDocs.classifyJob(mk({ heartbeat: null }), now, true) === "interrupted")
  check("terminal phases win unconditionally",
    JobDocs.classifyJob(mk({ phase: "done", heartbeat: null }), now, false) === "done"
    && JobDocs.classifyJob(mk({ phase: "error" }), now, false) === "error")
  check("progress clamps to [0,1]",
    mk({ progress: 1.7 }).progress === 1 && mk({ progress: -3 }).progress === 0)
  check("progress accepts numeric strings, rejects junk",
    mk({ progress: "0.25" }).progress === 0.25 && mk({ progress: "high" }).progress === 0)
  check("pid coerces like the contract (string ok, junk → 0)",
    mk({ pid: "12345" }).pid === 12345 && mk({ pid: "junk" }).pid === 0
    && mk({ pid: -4 }).pid === 0)
  check("null strings stringify, null bytes default to 0",
    mk({ message: null }).message === "" && mk({ total_bytes: null }).totalBytes === 0
    && mk({ new_version: null }).newVersion === "")
  check("normalizeJob of unusable input returns the safe shape, never null",
    JobDocs.normalizeJob(null).ok === false && JobDocs.normalizeJob([]).ok === false
    && JobDocs.normalizeJob("x").ok === false
    && JobDocs.normalizeJob(null).phase === "")
  // force_quit is additive (ticket 26): present only when the update
  // actually closed processes, and the reader never trusts its shape.
  check("force_quit absent/malformed → null, never throws",
    mk({}).forceQuit === null
    && mk({ force_quit: null }).forceQuit === null
    && mk({ force_quit: "closed 3" }).forceQuit === null
    && mk({ force_quit: [3, true] }).forceQuit === null
    && mk({ force_quit: 7 }).forceQuit === null)
  check("force_quit normalizes to {count, sigkill}",
    mk({ force_quit: { count: 3, sigkill: false } }).forceQuit.count === 3
    && mk({ force_quit: { count: 3, sigkill: false } }).forceQuit.sigkill === false
    && mk({ force_quit: { count: "3", sigkill: true } }).forceQuit.count === 3
    && mk({ force_quit: { count: "3", sigkill: true } }).forceQuit.sigkill === true
    && mk({ force_quit: { count: "junk", sigkill: "yes" } }).forceQuit.count === 0
    && mk({ force_quit: { count: -2 } }).forceQuit.count === 0
    && mk({ force_quit: { count: -2 } }).forceQuit.sigkill === false)
  check("elapsed/age helpers", JobDocs.heartbeatAgeSeconds(mk({}), 1792494410000) === 10
    && JobDocs.isHeartbeatFresh(mk({}), 1792494410000) === true
    && JobDocs.elapsedSeconds(mk({}), 1792494600000) === 600
    && JobDocs.heartbeatAgeSeconds(mk({ heartbeat: null }), now)
      === Number.POSITIVE_INFINITY)
  check("adoptJob(null) is idle — no document, not a broken one",
    JobDocs.adoptJob(null, true, now).state === "idle")
  check("classifyJob tolerates null input",
    JobDocs.classifyJob(null, now, true) === "interrupted")
}

// -- #27: force-quit done banner (the panel side of ticket 26) ---------------
{
  const nowMs = 1792494500000
  const done = (over) => JobDocs.adoptDocumentForPanel(
    JSON.stringify(Object.assign({
      schema_version: 1, desktop_id: "fakeapp.desktop", phase: "done",
      status: "updated", new_version: "v2.0", message: "updated successfully",
      heartbeat: 1792494400
    }, over)), null, nowMs).decision
  const err = (over) => JobDocs.adoptDocumentForPanel(
    JSON.stringify(Object.assign({
      schema_version: 1, desktop_id: "fakeapp.desktop", phase: "error",
      status: "error", error: "download failed", message: "download failed",
      heartbeat: 1792494400
    }, over)), null, nowMs).decision
  const updatedLine = Model.formatCount("v2.0", Model.strings.updated)

  check("#27: forceQuit rides the panel decision (null vs real record)",
    done({}).forceQuit === null
    && done({ force_quit: { count: 2, sigkill: true } }).forceQuit.count === 2
    && done({ force_quit: { count: 2, sigkill: true } }).forceQuit.sigkill === true)
  check("#27: done banner without force_quit is exactly the pre-#27 line",
    Model.jobDoneBanner(done({}), "") === updatedLine
    && Model.jobDoneBanner(done({ force_quit: null }), "") === updatedLine)
  check("#27: done banner appends the closed-app clause once a close was recorded",
    Model.jobDoneBanner(done({ force_quit: { count: 2, sigkill: false } }), "")
      === updatedLine + Model.strings.forceQuitApplied
    && Model.jobDoneBanner(done({ force_quit: { count: 1 } }), "")
      === updatedLine + Model.strings.forceQuitApplied)
  check("#27: SIGKILL reads as force-quit, TERM as closed",
    Model.jobDoneBanner(done({ force_quit: { count: 2, sigkill: true } }), "")
      === updatedLine + Model.strings.forceQuitAppliedSigkill
    && Model.strings.forceQuitAppliedSigkill
      !== Model.strings.forceQuitApplied)
  check("#27: a malformed force_quit never makes the banner claim a close",
    ["closed 3", [3, true], 7, true].every((v) =>
      Model.jobDoneBanner(done({ force_quit: v }), "") === updatedLine)
    && Model.jobDoneBanner(done({ force_quit: { count: "junk" } }), "") === updatedLine
    && Model.jobDoneBanner(done({ force_quit: { count: -2 } }), "") === updatedLine)
  check("#27: up-to-date banner keeps message/fallback and claims no close",
    Model.jobDoneBanner(done({ status: "up-to-date", new_version: "",
      message: "" }), Model.strings.upToDate) === Model.strings.upToDate
    && Model.jobDoneBanner(done({ status: "up-to-date", new_version: "",
      message: "already current" }), Model.strings.upToDate) === "already current"
    && Model.jobDoneBanner(done({ status: "up-to-date", new_version: "",
      message: "already current", force_quit: { count: 1, sigkill: false } }),
      Model.strings.upToDate) === "already current"
    && Model.jobDoneBanner(done({ status: "up-to-date", new_version: "",
      message: "already current", force_quit: { count: 1, sigkill: true } }),
      Model.strings.upToDate) === "already current")
  check("#27: an error banner says the close came before the failure",
    Model.jobErrorBanner(err({ force_quit: { count: 2, sigkill: false } }),
      Model.strings.backendFailed)
      === "download failed" + Model.strings.forceQuitFailed
    && Model.jobErrorBanner(err({ force_quit: { count: 2, sigkill: true } }),
      Model.strings.backendFailed)
      === "download failed" + Model.strings.forceQuitFailedSigkill
    && Model.strings.forceQuitFailed !== Model.strings.forceQuitApplied)
  check("#27: an error banner without force_quit is exactly the error text",
    Model.jobErrorBanner(err({}), Model.strings.backendFailed) === "download failed"
    && Model.jobErrorBanner(err({ force_quit: { count: 0 } }),
      Model.strings.backendFailed) === "download failed"
    && Model.jobErrorBanner(err({ force_quit: "closed 2" }),
      Model.strings.backendFailed) === "download failed")
  check("#27: an error banner falls back to message, then to the caller's line",
    Model.jobErrorBanner(err({ error: "", message: "install failed" }),
      Model.strings.backendFailed) === "install failed"
    && Model.jobErrorBanner(err({ error: "", message: "" }),
      Model.strings.backendFailed) === Model.strings.backendFailed
    && Model.jobErrorBanner(null, Model.strings.backendFailed)
      === Model.strings.backendFailed)
  check("#27: a version-less updated doc falls back to the backend message",
    Model.jobDoneBanner(done({ new_version: "" }), "") === "updated successfully")
  check("#27: banner helper tolerates a missing decision",
    Model.jobDoneBanner(null, "") === "" && Model.jobDoneBanner(undefined, "") === "")
}

// -- #27: force-quit card copy (singular / plural / count-free fallback) -----
{
  // Model.forceQuitBody owns the whole sentence decision: the QML card only
  // passes the name and process count pinned at click time, so the choice
  // is assertable here without a panel.
  const singular = Model.forceQuitBody("Neovim", 1)
  const plural = Model.forceQuitBody("Neovim", 3)
  const fallback = Model.forceQuitBody("Neovim", 0)
  check("#27: one process reads as a singular sentence",
    singular === "Neovim is running — 1 process will be closed to apply the update.",
    singular)
  check("#27: two or more processes read as a plural sentence",
    plural === "Neovim is running — 3 processes will be closed to apply the update.",
    plural)
  check("#27: an unknown count keeps the count-free fallback sentence",
    fallback === Model.formatCount("Neovim", Model.strings.forceQuitBody)
    && !/[0-9]/.test(fallback), fallback)
  check("#27: absent/junk/negative counts all take the fallback",
    [undefined, null, "", "junk", -3, NaN].every((v) =>
      Model.forceQuitBody("Neovim", v) === fallback))
  check("#27: a name carrying a placeholder is substituted, not re-expanded",
    Model.forceQuitBody("a%1b", 2)
    === "a%1b is running — 2 processes will be closed to apply the update.")

  // -- row meta line: one bounded, elided StyledText ----------------------
  // The meta line used to be a Row of separate Texts, which painted over the
  // row's action buttons as soon as the fragments got long (a version, a
  // pending release and a byte-pair progress label). Model.rowMeta now owns
  // the segment list, so the panel renders one spanned Text and this asserts
  // the order, the accent flags and the escaping.
  {
    const item = Model.parseListJson(JSON.stringify({
      schema_version: 1,
      installed: [{ desktop_id: "o.desktop", name: "Orca",
        path: "/x/orca.appimage", current_version: "1.4.235",
        available_version: "v1.4.237", running: true }]
    })).items[0]
    const segs = Model.rowMeta(item, "0% · 0 B / 232.1 MB")
    check("#row-meta: segments arrive in display order",
      segs.map((s) => s.text).join("|") === "1.4.235|● running|⬆ v1.4.237 available|0% · 0 B / 232.1 MB",
      segs.map((s) => s.text).join("|"))
    check("#row-meta: only the version is quiet text",
      segs.map((s) => s.accent).join(",") === "false,true,true,true",
      segs.map((s) => s.accent).join(","))
    check("#row-meta: no progress label means no progress segment",
      Model.rowMeta(item, "").length === 3)
    const quiet = Model.parseListJson(JSON.stringify({
      schema_version: 1,
      installed: [{ desktop_id: "q.desktop", name: "Quiet",
        path: "/x/quiet.appimage", current_version: "2.1.2" }]
    })).items[0]
    check("#row-meta: a plain row is the version alone",
      Model.rowMeta(quiet, "").length === 1
      && Model.rowMeta(quiet, "").length === 1
      && Model.rowMeta(quiet, "")[0].text === "2.1.2")
    check("#row-meta: missing item yields no segments",
      Model.rowMeta(null, "50%").length === 0
      && Model.rowMeta(undefined, "").length === 0)
    check("#row-meta: markup in user data is escaped, not interpreted",
      Model.escapeHtml('<b>a & b</b>') === "&lt;b&gt;a &amp; b&lt;/b&gt;")
    check("#row-meta: escapeHtml tolerates null/undefined/number",
      Model.escapeHtml(null) === "" && Model.escapeHtml(undefined) === ""
      && Model.escapeHtml(7) === "7")
  }

  // running_pids rides the app normalization as a clamped int; the key is
  // additive, so an older capture (or backend) has none at all.
  const itemWith = (entry) => Model.parseListJson(JSON.stringify({
    schema_version: 1,
    installed: [Object.assign({ desktop_id: "x.desktop", name: "X",
      path: "/x.appimage", running: true }, entry)]
  })).items[0].runningPids
  check("#27: running_pids normalizes to a non-negative int",
    itemWith({ running_pids: 3 }) === 3 && itemWith({ running_pids: "3" }) === 3
    && itemWith({ running_pids: 0 }) === 0
    && itemWith({ running_pids: -2 }) === 0
    && itemWith({ running_pids: "junk" }) === 0)
  check("#27: an absent running_pids is 0 (older backend/capture)",
    itemWith({}) === 0 && list.items[0].runningPids === 0)
}

// -- #11 panel-reader additions: path rules + adoption plumbing ---------------
{
  // jobDocumentStem/jobDocumentPath: the writer's own character class is
  // the reader's admission rule — a stem the writer could not derive
  // yields "" and the caller skips the read (no traversal, no hidden
  // files). An id WITHOUT the .desktop suffix is accepted as its own
  // stem (jobs.py's _job_stem only strips the suffix when present); the
  // QML call sites gate the suffix themselves.
  check("jobDocumentStem strips the .desktop suffix",
    JobDocs.jobDocumentStem("neovim.desktop") === "neovim"
    && JobDocs.jobDocumentStem("fakeapp.desktop") === "fakeapp")
  check("jobDocumentStem accepts a suffix-less id as its own stem",
    JobDocs.jobDocumentStem("secondapp") === "secondapp")
  check("jobDocumentStem rejects unsafe ids",
    JobDocs.jobDocumentStem("../x") === "" && JobDocs.jobDocumentStem(".hidden") === ""
    && JobDocs.jobDocumentStem("x/y") === "" && JobDocs.jobDocumentStem("") === ""
    && JobDocs.jobDocumentStem(null) === "" && JobDocs.jobDocumentStem(undefined) === "")
  check("jobDocumentPath joins the stem under the jobs dir",
    JobDocs.jobDocumentPath("/state/jobs", "neovim.desktop") === "/state/jobs/neovim.json")
  check("jobDocumentPath empty on an unusable id or dir",
    JobDocs.jobDocumentPath("/state/jobs", "../x") === ""
    && JobDocs.jobDocumentPath("", "neovim.desktop") === "")

  // adoptDocumentForPanel: the decision plus the panel's two extras —
  // the byte fields for the sane-bytes progress gate and the heartbeat,
  // the consume-once sign-of-life. A corrupt document has no usable
  // heartbeat (0), which the panel's surfaceJobOutcome maps to the -1
  // once-per-session consumed marker (pinned on Model below).
  const dlPanel = JobDocs.adoptDocumentForPanel(cap("job-downloading.json"), true,
    at("job-downloading.json", 5))
  check("adoptDocumentForPanel carries heartbeat + byte fields",
    dlPanel.parseError === "" && dlPanel.decision.state === "active"
    && dlPanel.heartbeat === JobDocs.normalizeJob(jobDoc("job-downloading.json")).heartbeat
    && dlPanel.decision.totalBytes === 11442680
    && dlPanel.decision.downloadedBytes === Math.round(0.42 * 11442680))
  const corruptPanel = JobDocs.adoptDocumentForPanel(cap("job-truncated.json"), true, Date.now())
  check("adoptDocumentForPanel corrupt → interrupted, no heartbeat",
    corruptPanel.parseError !== "" && corruptPanel.decision.state === "interrupted"
    && corruptPanel.heartbeat === 0)
  const donePanel = JobDocs.adoptDocumentForPanel(cap("job-done-updated.json"), true, Date.now())
  check("adoptDocumentForPanel leaves the done decision intact",
    donePanel.decision.state === "done" && donePanel.decision.progress === 1
    && donePanel.decision.totalBytes === 106
    && donePanel.decision.downloadedBytes === 10996216)

  // provisionalActiveDecision: the pre-first-write view a job-started
  // reply adopts — active at zero, carrying the id, byte fields zeroed.
  const prov = JobDocs.provisionalActiveDecision("fakeapp.desktop")
  check("provisional decision is active at zero and carries the id",
    prov.state === "active" && prov.progress === 0 && prov.phase === "starting"
    && prov.desktopId === "fakeapp.desktop")
  check("provisional decision zeroes the byte fields, tolerates an empty id",
    prov.totalBytes === 0 && prov.downloadedBytes === 0
    && JobDocs.provisionalActiveDecision("").desktopId === "")

  // bytesSane/formatBytes: only payload-sized totals earn a byte pair —
  // a zsync control-file "total" (kilobytes) would read as a lie under a
  // truthful percent.
  check("bytesSane rejects 0/undefined/too-small",
    JobDocs.bytesSane(0) === false && JobDocs.bytesSane(undefined) === false
    && JobDocs.bytesSane(1024 * 1024 - 1) === false && JobDocs.bytesSane(-1) === false)
  check("bytesSane accepts 1 MiB and real payloads",
    JobDocs.bytesSane(1024 * 1024) === true && JobDocs.bytesSane(11442680) === true)
  check("formatBytes labels",
    JobDocs.formatBytes(0) === "0 B" && JobDocs.formatBytes(undefined) === "0 B"
    && JobDocs.formatBytes(-5) === "0 B" && JobDocs.formatBytes(512) === "512 B"
    && JobDocs.formatBytes(2048) === "2 KB" && JobDocs.formatBytes(11442680) === "10.9 MB")

  // Model store: the adopted-decision plumbing (shape-keyed dedup, the
  // consumed sign-of-life, per-app activity, ghost pruning). This block
  // owns the only store subscription, so `storeChanges` counts exactly
  // the changed() announcements the calls below make.
  Model.setItems([Model.normalizeItem({ desktop_id: "kept.desktop",
    name: "Kept", path: "/x/kept.appimage" })])
  const provKept = JobDocs.provisionalActiveDecision("kept.desktop")
  let storeChanges = 0
  const unsubJobs = Model.subscribe(() => { storeChanges++ })
  Model.adoptJobDecision("kept.desktop", provKept)
  const changesAfterFirst = storeChanges
  Model.adoptJobDecision("kept.desktop", JobDocs.provisionalActiveDecision("kept.desktop"))
  check("adoptJobDecision dedups an identical shape (one change)",
    changesAfterFirst === 1 && storeChanges === 1, "storeChanges=" + storeChanges)
  Model.adoptJobDecision("kept.desktop", Object.assign(
    JobDocs.provisionalActiveDecision("kept.desktop"), { progress: 0.5 }))
  check("adoptJobDecision announces a real shape change", storeChanges === 2)
  Model.adoptJobDecision("", provKept)
  Model.adoptJobDecision("kept.desktop", null)
  check("adoptJobDecision ignores an empty id / non-object decision",
    storeChanges === 2 && Model.jobDecision("kept.desktop").progress === 0.5)
  check("jobActive true only while the decision is active",
    Model.jobActive("kept.desktop") === true && Model.jobActive("nope.desktop") === false)
  // The consumed sign-of-life survives re-adoption; 0/non-finite marks
  // are ignored (0 = "no heartbeat yet", never a settled outcome).
  Model.markJobConsumed("kept.desktop", 424242)
  Model.adoptJobDecision("kept.desktop", Object.assign(
    JobDocs.provisionalActiveDecision("kept.desktop"), { progress: 0.75 }))
  check("adoptJobDecision preserves the consumed marker",
    Model.jobEntry("kept.desktop").consumed === 424242 && storeChanges === 4)
  Model.markJobConsumed("kept.desktop", 0)
  Model.markJobConsumed("kept.desktop", NaN)
  check("markJobConsumed ignores 0/non-finite marks",
    Model.jobEntry("kept.desktop").consumed === 424242)
  // -1 is the corrupt-document marker: it can never collide with a real
  // heartbeat, so a corrupt outcome surfaces exactly once per session.
  Model.markJobConsumed("kept.desktop", -1)
  check("markJobConsumed stores the -1 corrupt marker",
    Model.jobEntry("kept.desktop").consumed === -1)
  let threwJobs = false
  try { Model.markJobConsumed("ghost.desktop", 5) } catch (e) { threwJobs = true }
  check("markJobConsumed on an unknown id is a no-op", threwJobs === false
    && Model.jobEntry("ghost.desktop") === null)
  // anyJobActive + pruneJobStates: ghost views die with the list, and a
  // settled view keeps no poll alive.
  Model.adoptJobDecision("kept.desktop", provKept)
  Model.adoptJobDecision("ghost.desktop",
    JobDocs.provisionalActiveDecision("ghost.desktop"))
  check("anyJobActive true while any row is active", Model.anyJobActive() === true
    && Model.jobActive("ghost.desktop") === true)
  Model.setItems([Model.normalizeItem({ desktop_id: "kept.desktop",
    name: "Kept", path: "/x/kept.appimage" })])
  check("pruneJobStates keeps installed views, drops ghosts",
    Model.jobEntry("kept.desktop") !== null && Model.jobEntry("ghost.desktop") === null)
  Model.adoptJobDecision("kept.desktop", Object.assign(
    JobDocs.provisionalActiveDecision("kept.desktop"), { state: "done", progress: 1 }))
  check("jobActive/anyJobActive false once the view settles",
    Model.jobActive("kept.desktop") === false && Model.anyJobActive() === false)
  unsubJobs()
}

// ---- Model.releaseOrphanedRuns: a torn-down panel's run flags ---------------
{
  let announcements = 0
  const unsubscribe = Model.subscribe(() => { announcements++ })
  // The panel set the flag, then the panel QML object was destroyed (panel
  // close) and its Process took the backend child with it — so the clear
  // site inside onDone never ran and the flag survived the reload.
  Model.setBusy("integrate", true)
  Model.setStatus(Model.strings.integrateWorking)
  check("orphaned integrate flag and status are in the store",
    Model.state.busy.integrate === true
      && Model.state.status === Model.strings.integrateWorking)
  Model.releaseOrphanedRuns()
  check("releaseOrphanedRuns clears the orphaned integrate flag",
    Model.state.busy.integrate === false)
  check("releaseOrphanedRuns drops the in-flight status line",
    Model.state.status === "")
  check("releaseOrphanedRuns announces to listeners", announcements > 0)
  // The same orphan class on the update-check claim: without the release
  // Service.qml's claimUpdateCheck() would fail for the shell's lifetime.
  check("a claim is refused to a second caller",
    Model.claimUpdateCheck("panel") === true
      && Model.claimUpdateCheck("service") === false)
  check("releaseOrphanedRuns releases a claim the PANEL took",
    (Model.releaseOrphanedRuns(), Model.claimUpdateCheck("service") === true))
  check("releaseOrphanedRuns keeps a claim the SERVICE took",
    (Model.releaseOrphanedRuns(), Model.state.busy.updates === true
      && Model.state.updatesChecking === true))
  Model.setError("boom")
  check("releaseOrphanedRuns keeps a real outcome",
    (Model.releaseOrphanedRuns(), Model.state.lastError === "boom"))
  check("the service claim still blocks, owner untouched",
    Model.state.updateCheckOwner === "service")
  Model.releaseUpdateCheck()
  check("releaseUpdateCheck clears claim and owner",
    Model.state.busy.updates === false && Model.state.updateCheckOwner === "")
  unsubscribe()
}

rmSync(sandbox, { recursive: true, force: true })

console.log(failures === 0 ? "\nALL PASS" : "\n" + failures + " FAILURES")
process.exit(failures === 0 ? 0 : 1)

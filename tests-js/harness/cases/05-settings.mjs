// ---- F8/F9: --settings / --set-setting ----------------------------------------

import { Backend, Model, cap, check } from "../core.mjs"

export function run() {
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
}

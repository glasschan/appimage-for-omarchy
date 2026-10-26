// ---- Backend.js helpers ------------------------------------------------------

import { Backend, Model, check, list } from "../core.mjs"

export function run() {
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
}

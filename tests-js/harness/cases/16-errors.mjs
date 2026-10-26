// ---- #48 errors persist and name their app ---------------------------------

import { Model, check } from "../core.mjs"

export function run() {
  Model.clearError()
  Model.clearStatus()
  Model.setError("update failed", "launch.desktop")
  Model.setStatus("checking updates")
  check("#48 a status line never clears an error",
    Model.state.lastError === "update failed")
  Model.noteOutcome("other.desktop")
  check("#48 another app's outcome leaves the error alone",
    Model.state.lastError === "update failed")
  Model.noteOutcome("launch.desktop")
  check("#48 the same app's next outcome replaces it",
    Model.state.lastError === "" && Model.state.lastErrorSubject === "")
  Model.clearStatus()
  Model.setError(Model.appErrorBanner("Launch App", "Backend call failed"), "launch.desktop")
  check("#48 an app error names the app",
    Model.state.lastError.indexOf("Launch App") === 0, Model.state.lastError)
  Model.setError("no app", "")
  Model.noteOutcome("")
  check("#48 an error about no app is never cleared by an outcome",
    Model.state.lastError === "no app")
  Model.clearError()
  check("#48 dismissal clears error and subject",
    Model.state.lastError === "" && Model.state.lastErrorSubject === "")
}

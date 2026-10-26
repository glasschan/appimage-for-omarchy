// -- #27: force-quit done banner (the panel side of ticket 26) ---------------

import { JobDocs, Model, check, list } from "../core.mjs"

export function run() {
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

    // -- row meta lines: two bounded, elided StyledTexts --------------------
    // The row's meta used to be ONE segment list (version · running dot ·
    // pending release · job progress) rendered as one StyledText: the update
    // status read as part of the version and grew into the row's action
    // buttons, so it has its own line now (issue #51). Model.rowIdentityMeta
    // and Model.rowStatusMeta own the two lists; each line stays ONE
    // bounded, elided Text (a Row of separate Texts has no width bound and
    // painted over the buttons — v0.9.1).
    {
      const item = Model.parseListJson(JSON.stringify({
        schema_version: 1,
        installed: [{ desktop_id: "o.desktop", name: "Orca",
          path: "/x/orca.appimage", current_version: "1.4.235",
          available_version: "v1.4.237", running: true }]
      })).items[0]
      const identity = Model.rowIdentityMeta(item)
      const status = Model.rowStatusMeta(item, "0% · 0 B / 232.1 MB")
      check("#row-meta: the identity line is the version and the running dot",
        identity.map((s) => s.text).join("|") === "1.4.235|● running",
        identity.map((s) => s.text).join("|"))
      check("#row-meta: the identity line's accent flags",
        identity.map((s) => s.accent).join(",") === "false,true",
        identity.map((s) => s.accent).join(","))
      check("#row-meta: the status line is the pending release and the progress",
        status.map((s) => s.text).join("|") === "⬆ v1.4.237 available|0% · 0 B / 232.1 MB",
        status.map((s) => s.text).join("|"))
      check("#row-meta: the status line's accent flags",
        status.map((s) => s.accent).join(",") === "true,true",
        status.map((s) => s.accent).join(","))
      check("#row-meta: no progress label means no progress segment",
        Model.rowStatusMeta(item, "").length === 1)
      const quiet = Model.parseListJson(JSON.stringify({
        schema_version: 1,
        installed: [{ desktop_id: "q.desktop", name: "Quiet",
          path: "/x/quiet.appimage", current_version: "2.1.2" }]
      })).items[0]
      check("#row-meta: a plain row's identity line is the version alone",
        Model.rowIdentityMeta(quiet).length === 1
        && Model.rowIdentityMeta(quiet)[0].text === "2.1.2")
      check("#row-meta: a plain row has no status line at all",
        Model.rowStatusMeta(quiet, "").length === 0)
      check("#row-meta: missing item yields no segments",
        Model.rowIdentityMeta(null).length === 0
        && Model.rowIdentityMeta(undefined).length === 0
        && Model.rowStatusMeta(null, "50%").length === 0
        && Model.rowStatusMeta(undefined, "").length === 0)
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
}

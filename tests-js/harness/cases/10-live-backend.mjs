// ---- Live backend: exec_arguments + the stale-record path --------------------
// The captures above are frozen e2e output. The two behaviours pinned here
// have no capture (a stale record cannot be produced by a normal run), so
// the REAL backend runs in a throwaway XDG sandbox and its output goes
// through the same JS parse logic as every other document.

import { existsSync, readFileSync, writeFileSync } from "node:fs"
import { join } from "node:path"
import { Backend, Model, check } from "../core.mjs"
import { ensureLive } from "../live.mjs"

export function run() {
  const { home, dataHome, appsIni, stale, staleDesktop, PREVIEW_APPIMAGE,
    runBackend, listRun, liveOk } = ensureLive()
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
}

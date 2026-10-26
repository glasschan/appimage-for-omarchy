// ---- F2: --integrate outcomes ----------------------------------------------

import { Model, cap, check } from "../core.mjs"

export function run() {
  const integ = Model.parseActionJson(cap("integrated.json"))
  check("integrated result", integ.ok === true && integ.result === "integrated")
  check("integrated app mapped", integ.app && integ.app.id === "neovim.desktop" && integ.app.name === "Neovim")
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
}

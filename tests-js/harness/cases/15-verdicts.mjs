// ---- #39 installability: the row never offers a refused install -------------
// Distinct desktop ids: applyUpdates merges by id, so two rows must not share one.

import { readFileSync } from "node:fs"

import { Model, check } from "../core.mjs"

export function run() {
  const parsedWithSource = (overrides) => Model.parseListJson(JSON.stringify({
    schema_version: 1,
    installed: [Object.assign({
      name: "Gitlab App", desktop_id: "gitlabapp.desktop",
      path: "/x/gitlabapp.appimage",
      current_version: "1.0", available_version: "2.0",
      manager: "GitlabUpdater", embedded_source: false,
      running: false, running_pids: 0, exec_arguments: ""
    }, overrides)]
  })).items[0]

  const unverifiable = parsedWithSource({ installable: false, verification: "size" })
  check("#39 installability carried onto the item",
    unverifiable.installable === false && unverifiable.verification === "size")
  check("#39 an absent field means not determinable",
    parsedWithSource({}).installable === null)
  const verifiable = parsedWithSource({
    installable: true, verification: "sha256",
    name: "Github App", desktop_id: "githubapp.desktop",
    path: "/x/githubapp.appimage", manager: "GithubUpdater"
  })

  Model.setItems([unverifiable, verifiable])
  Model.applyUpdates([unverifiable, verifiable])
  check("#39 the badge excludes a non-installable update",
    Model.updateCount() === 1, String(Model.updateCount()))
  const unverifiableSegs = Model.rowStatusMeta(
    Model.findItem("gitlabapp.desktop"), "").map((s) => s.text)
  const verifiableSegs = Model.rowStatusMeta(
    Model.findItem("githubapp.desktop"), "").map((s) => s.text)
  check("#39 a non-installable row says cannot verify",
    unverifiableSegs.includes(Model.strings.cannotVerify), unverifiableSegs.join("|"))
  check("#39 a non-installable row drops the pending release",
    !unverifiableSegs.some((t) => t.indexOf("available") !== -1), unverifiableSegs.join("|"))
  check("#39 a verifiable row keeps its pending release",
    verifiableSegs.some((t) => t.indexOf("available") !== -1), verifiableSegs.join("|"))
  check("#39 the row hides the update action for a refused install",
    unverifiable.installable === false && verifiable.installable !== false)

  // ---- #47 the launch verdict outlives the panel -----------------------------
  // The click closes the panel, so the verdict cannot live on the panel object;
  // it lives in the store and any later listing settles it.
  const launchRow = (overrides) => parsedWithSource(Object.assign({
    name: "Launch App", desktop_id: "launch.desktop", path: "/x/launch.appimage",
    current_version: "1.0", available_version: null, manager: null
  }, overrides))

  Model.setItems([])
  Model.clearError()
  Model.armLaunchVerdict("launch.desktop")
  check("#47 arming the verdict records it",
    Model.state.launchVerdict !== null
    && Model.state.launchVerdict.id === "launch.desktop")
  const armedAt = Model.state.launchVerdict.atMs

  // A listing that began before the click cannot judge it.
  Model.setItems([launchRow({ running: false })], { fuse2: "present" }, armedAt - 1)
  check("#47 an earlier listing does not settle the verdict",
    Model.state.launchVerdict !== null && Model.state.lastError === "")

  // A running app is a silence, not a banner.
  Model.setItems([launchRow({ running: true })], { fuse2: "present" }, armedAt + 1)
  check("#47 a running app settles silently",
    Model.state.launchVerdict === null && Model.state.lastError === "")
  check("#47 a settled verdict never names an app in the banner",
    Model.state.lastErrorSubject === "")

  // A present, non-running row is the failure, and the listing's own runtime
  // block decides the fuse2 hint.
  Model.clearError()
  Model.armLaunchVerdict("launch.desktop", armedAt + 10)
  Model.setItems([launchRow({ running: false })], { fuse2: "missing" }, armedAt + 11)
  const launchBanner = Model.state.lastError
  check("#47 a dead launch raises the banner",
    launchBanner.indexOf("Launch App") !== -1, launchBanner)
  check("#47 the fuse2 hint rides the same listing",
    launchBanner.indexOf(Model.strings.launchFuse2Hint) !== -1)
  check("#47 the banner belongs to that app",
    Model.state.lastErrorSubject === "launch.desktop")

  // An app that vanished from the list is no claim at all.
  Model.clearError()
  Model.armLaunchVerdict("gone.desktop", armedAt + 20)
  Model.setItems([launchRow({})], { fuse2: "present" }, armedAt + 21)
  check("#47 a missing row settles without a banner",
    Model.state.launchVerdict === null && Model.state.lastError === "")

  // The caller has to be able to tell a banner it must keep from a refresh
  // it may clear: setItems reports whether it settled one.
  Model.clearError()
  Model.armLaunchVerdict("launch.desktop", armedAt + 30)
  check("#47 a settled banner is reported to the caller",
    Model.setItems([launchRow({ running: false })],
                   { fuse2: "present" }, armedAt + 31) === true)
  check("#47 an idle listing reports nothing",
    Model.setItems([launchRow({ running: true })],
                   { fuse2: "present" }, armedAt + 32) === false)
  Model.clearError()

  // ---- #50 the pending verdict survives the listing -------------------------
  // The verdict used to have two owners: the check response set it and the
  // next listing (polled every 10 s) overwrote it with "no update", so the
  // pending marker, the badge count and the row's Update button vanished
  // within ten seconds of every check. The backend's durable record owns the
  // verdict now and the listing carries it (pending_update /
  // pending_version / pending_installable); the frontend mirrors it.
  {
    const pendingListing = (entry) => Model.parseListJson(JSON.stringify({
      schema_version: 1,
      installed: [Object.assign({
        name: "Pending App", desktop_id: "pending.desktop",
        path: "/x/pending.appimage", current_version: "1.0",
        manager: "GithubUpdater", embedded_source: false,
        running: false, running_pids: 0
      }, entry)]
    })).items[0]

    const pending = pendingListing({ pending_update: true, pending_version: "v2.0",
      pending_installable: true })
    check("#50 the listing's pending triple lands on the item",
      pending.hasUpdate === true && pending.updateVersion === "v2.0"
      && pending.installable === true)
    const versionless = pendingListing({ pending_update: true, pending_version: null,
      pending_installable: true })
    check("#50 a pending release with no named version is versionless, not absent",
      versionless.hasUpdate === true && versionless.updateVersion === "",
      JSON.stringify(versionless))
    const oldBackend = pendingListing({})
    check("#50 an older backend reports no pending triple",
      oldBackend.hasUpdate === false && oldBackend.updateVersion === ""
      && oldBackend.installable === null)
    const oldBackendVerdict = pendingListing({ available_version: "v2.0", installable: false })
    check("#50 an older backend still reads available_version and installable",
      oldBackendVerdict.updateVersion === "v2.0"
      && oldBackendVerdict.installable === false)
    check("#50 an explicit null verdict is not determinable",
      pendingListing({ pending_update: false, pending_version: null,
        pending_installable: null }).installable === null)

    // The marker, the badge and the row's Update button agree with the listing.
    Model.setItems([pending])
    check("#50 the listing's verdict marks the row",
      Model.findItem("pending.desktop").hasUpdate === true)
    check("#50 the listing's verdict badges the bar",
      Model.updateCount() === 1)
    check("#50 the row's update action follows the listing's verdict",
      Model.findItem("pending.desktop").hasUpdate === true
      && Model.findItem("pending.desktop").installable !== false)

    // The regression this change fixes: a check response marks the row, and
    // the listing that follows must not take the verdict away.
    const checked = Model.parseUpdatesJson(JSON.stringify({
      schema_version: 1,
      updates: [{ name: "Pending App", desktop_id: "pending.desktop",
        path: "/x/pending.appimage", current_version: "1.0",
        available_version: "v2.0", installable: true, manager: "GithubUpdater",
        embedded_source: false, running: false }]
    })).updates
    Model.setItems([pendingListing({})])
    Model.applyUpdates(checked)
    check("#50 a check response marks the row at once",
      Model.findItem("pending.desktop").hasUpdate === true
      && Model.updateCount() === 1)
    Model.setItems([pending])
    check("#50 the verdict survives the next listing",
      Model.findItem("pending.desktop").hasUpdate === true
      && Model.findItem("pending.desktop").updateVersion === "v2.0"
      && Model.updateCount() === 1)

    // A listing that reports no pending release is the honest answer: the
    // verdict is gone.
    Model.setItems([pendingListing({ pending_update: false, pending_version: null,
      pending_installable: null })])
    check("#50 a listing without a pending release retires the verdict",
      Model.findItem("pending.desktop").hasUpdate === false
      && Model.findItem("pending.desktop").updateVersion === ""
      && Model.updateCount() === 0)
    // A listing from a backend that reports no pending fields cannot know the
    // verdict either — it is gone.
    Model.setItems([pendingListing({})])
    check("#50 a listing from an older backend retires the verdict",
      Model.findItem("pending.desktop").hasUpdate === false
      && Model.updateCount() === 0)

    // A refused install is never offered: no badge, no update action, and the
    // row says "cannot verify".
    const refused = pendingListing({ pending_update: true, pending_version: "v2.0",
      pending_installable: false })
    Model.setItems([refused])
    check("#50 a refused verdict badges nothing",
      Model.updateCount() === 0)
    check("#50 a refused verdict offers no update action",
      Model.findItem("pending.desktop").installable === false
      && Model.findItem("pending.desktop").hasUpdate === true)
    check("#50 a refused verdict says cannot verify",
      Model.rowStatusMeta(Model.findItem("pending.desktop"), "")
        .map((s) => s.text).includes(Model.strings.cannotVerify))

    // The batch helper: the ids the backend would accept.
    Model.setItems([pending, refused])
    check("#52 the batch helper returns the acceptable ids",
      Model.pendingUpdateIds().join(",") === "pending.desktop",
      Model.pendingUpdateIds().join(","))
    Model.setItems([pendingListing({ pending_update: false })])
    check("#52 the batch helper is empty without a pending release",
      Model.pendingUpdateIds().length === 0)

    // The card's wording: the apps it names and the processes it would close.
    const summary = Model.updateAllSummary([
      { id: "a.desktop", name: "Alpha", running: true, runningPids: 2 },
      { id: "b.desktop", name: "Beta", running: false, runningPids: 0 }])
    check("#52 the card names the apps",
      summary.appsLine === "Apps: Alpha, Beta", summary.appsLine)
    check("#52 the card counts the processes it would close",
      summary.processLine === "2 processes will be closed to apply the updates."
      && summary.totalProcesses === 2, summary.processLine)
    check("#52 a single process reads as a singular sentence",
      Model.updateAllSummary([{ id: "a.desktop", name: "Alpha", running: true,
        runningPids: 1 }]).processLine
        === "1 process will be closed to apply the updates.")
    check("#52 no running app means no closing",
      Model.updateAllSummary([{ id: "b.desktop", name: "Beta", running: false }]).processLine
        === Model.strings.updateAllProcessesNone)
    check("#52 an undeterminable count keeps the count-free sentence",
      Model.updateAllSummary([{ id: "a.desktop", name: "Alpha", running: true,
        runningPids: 0 }]).processLine === Model.strings.updateAllProcessesUnknown)

    // A finished update retires the verdict in the STORE, not on the panel
    // object: the shell unloads the panel QML on close, so a verdict held
    // there could never be settled.
    Model.setItems([pending])
    Model.adoptJobDecision("pending.desktop", {
      state: "active", desktopId: "pending.desktop", phase: "downloading",
      progress: 0.4, message: "downloading", newVersion: "", status: "",
      errorText: "", stale: false, pidDead: false, elapsedSeconds: 2,
      forceQuit: null })
    check("#50 a running job keeps the verdict",
      Model.findItem("pending.desktop").hasUpdate === true)
    Model.adoptJobDecision("pending.desktop", {
      state: "done", desktopId: "pending.desktop", phase: "done",
      progress: 1, message: "updated", newVersion: "v2.0", status: "updated",
      errorText: "", stale: false, pidDead: false, elapsedSeconds: 3,
      forceQuit: null })
    check("#50 a finished update retires the verdict",
      Model.findItem("pending.desktop").hasUpdate === false
      && Model.findItem("pending.desktop").updateVersion === ""
      && Model.updateCount() === 0)
    check("#50 a finished update that changed nothing keeps the verdict",
      (() => {
        Model.setItems([pendingListing({ pending_update: true, pending_version: "v2.0",
          pending_installable: true })])
        Model.adoptJobDecision("other.desktop", {
          state: "done", desktopId: "other.desktop", phase: "done",
          progress: 1, message: "up to date", newVersion: "", status: "up-to-date",
          errorText: "", stale: false, pidDead: false, elapsedSeconds: 1,
          forceQuit: null })
        return Model.findItem("pending.desktop").hasUpdate === true
      })())

    // The #52 card renders desktop-entry names, which are attacker-
    // controlled: its Texts must declare PlainText so Text never rich-
    // detects them (adversarial review, 0.11.0). Pinned on the Panel.qml
    // source itself — same pattern as case 11's jobs.py parity checks.
    const panelQml = readFileSync(new URL("../../../Panel.qml", import.meta.url), "utf8")
    const cardBody = (panelQml.match(/id: updateAllCard[\s\S]*?\n          \}/) || [""])[0]
    const appsLineStart = cardBody.indexOf("root.updateAllSummary.appsLine")
    const processLineStart = cardBody.indexOf("root.updateAllSummary.processLine")
    const cardEnd = cardBody.indexOf("Row {", processLineStart)
    const declaresPlainText = (from) => {
      const block = cardBody.slice(from, cardEnd > from ? cardEnd : cardBody.length)
      return /textFormat:\s*Text\.PlainText/.test(block)
    }
    check("#52 the card's appsLine Text is PlainText",
      appsLineStart !== -1 && declaresPlainText(appsLineStart))
    check("#52 the card's processLine Text is PlainText",
      processLineStart !== -1 && declaresPlainText(processLineStart))
  }
}

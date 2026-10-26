// ---- Model.releaseOrphanedRuns: a torn-down panel's run flags ---------------

import { Model, check } from "../core.mjs"

export function run() {
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

  // ---- display name: the version suffix and the md5 "version" trap ---------
  // A row's title is the backend's desktop Name; the meta line below it
  // already prints the version, so the suffix must not repeat it, and the
  // md5-hash fallback ("Terax (77efdf)") is garbage no one asked to see.
  const rowName = (name, currentVersion) => Model.parseListJson(JSON.stringify({
    schema_version: 1,
    installed: [{ name: name, current_version: currentVersion,
      desktop_id: "x.desktop", path: "/home/u/AppImages/x.appimage" }]
  })).items[0].name
  check("title drops the version the meta line already shows",
    rowName("Escrcpy (3.3.1)", "3.3.1") === "Escrcpy", rowName("Escrcpy (3.3.1)", "3.3.1"))
  check("title drops the md5 fallback when the image has no version",
    rowName("Terax (77efdf)", null) === "Terax", rowName("Terax (77efdf)", null))
  check("title keeps a parenthetical that is not the version",
    rowName("Foo (Fork)", "2.0") === "Foo (Fork)", rowName("Foo (Fork)", "2.0"))
  check("title keeps a version-shaped suffix when no version is reported",
    rowName("Orca (1.4.237)", null) === "Orca (1.4.237)")
  check("title drops a hash suffix even once a real version is known",
    rowName("App (abcdef)", "1.0") === "App", rowName("App (abcdef)", "1.0"))
  check("title keeps a parenthetical that is not hash-shaped",
    rowName("App (Fork)", "1.0") === "App (Fork)")
  check("title keeps a long hex parenthetical that is not the hash shape",
    rowName("App (abcdefa)", "1.0") === "App (abcdefa)")


  // ---- #42 run state: claims, expiry, waits -----------------------------------
  // One claim table for the engine: an owner, a token and a deadline. A claim
  // whose owner died (the shell unloads the panel and kills its Process) must
  // not outlive its deadline, and a second attempt must be a wait.
  Model.releaseUpdateCheck()
  check("#42 first claim wins", Model.claimUpdateCheck("panel") === true)
  check("#42 second claimant waits", Model.claimUpdateCheck("service") === false)
  const liveClaim = Model.state.claims.updates
  check("#42 claim records owner and deadline",
    liveClaim.owner === "panel" && liveClaim.deadlineMs > Date.now())
  // The owner died: its deadline passed, so anyone may take over.
  liveClaim.deadlineMs = Date.now() - 1
  check("#42 an expired claim is taken over",
    Model.claimUpdateCheck("service") === true
    && Model.state.updateCheckOwner === "service")
  Model.releaseUpdateCheck()
  check("#42 release frees the claim", Object.keys(Model.state.claims).length === 0)
  Model.releaseUpdateCheck()
  check("#42 double release is harmless", Object.keys(Model.state.claims).length === 0)

  // A stale handle must not free a claim that has since been re-taken.
  const staleHandle = Model.claimOperation("updates", "panel", 1000)
  Model.releaseOperation(staleHandle)
  const freshHandle = Model.claimOperation("updates", "panel", 1000)
  check("#42 a stale token cannot free a newer claim",
    Model.releaseOperation(staleHandle) === false
    && Model.state.claims.updates.token === freshHandle.token)
  Model.releaseOperation(freshHandle)

  // One snapshot, same shape for both surfaces.
  const opsSnapshot = Model.snapshot()
  check("#42 snapshot carries busy, claims and outcomes",
    opsSnapshot.busy.list === false && typeof opsSnapshot.claims === "object"
    && opsSnapshot.status === Model.state.status
    && opsSnapshot.lastError === Model.state.lastError)

  // The wait notice must never wipe the operation that is still running.
  Model.clearStatus()
  Model.setStatus(Model.strings.integrateWorking)
  Model.setTransientStatus(Model.strings.operationBusy)
  check("#42 a wait never wipes a live status",
    Model.state.status === Model.strings.integrateWorking)
  // #48 changed setStatus: it no longer displaces an error, so clear the one
  // the previous section left behind before asking what a wait does.
  Model.clearError()
  Model.clearStatus()
  Model.setTransientStatus(Model.strings.operationBusy)
  check("#42 a wait speaks when nothing else is showing",
    Model.state.status === Model.strings.operationBusy)
  check("#42 a wait is never an error", Model.state.lastError === "")
  Model.clearStatus()

  // The claim carries its owner, so a surface whose claim was taken over
  // cannot free the winner's: the panel's late onDone must leave the
  // service's sweep alone.
  Model.releaseUpdateCheck("panel")
  if (!Model.claimUpdateCheck("service")) throw new Error("service could not claim")
  check("#42 a non-owner cannot release the claim",
    Model.releaseUpdateCheck("panel") === false
    && Model.state.updateCheckOwner === "service")
  check("#42 the owner releases its own claim",
    Model.releaseUpdateCheck("service") !== false
    && Model.state.claims.updates === undefined)
}

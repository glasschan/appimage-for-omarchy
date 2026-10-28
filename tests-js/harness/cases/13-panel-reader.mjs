// -- #11 panel-reader additions: path rules + adoption plumbing ---------------

import { readFileSync } from "node:fs"

import { JobDocs, Model, cap, check } from "../core.mjs"
import { at, jobDoc } from "../jobs.mjs"

export function run() {
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

  // ---- the Quickshell 0.3.2 FileView hang (2026-10-28 dead-feedback bug) ----
  //
  // An async preload of a MISSING document never delivers onLoaded or
  // onLoadFailed on this Quickshell: the read hangs until the 5 s watchdog
  // retires it, so the panel's queue drained at 5 s per absent document
  // and no decision — progress, done or error — ever reached a row (an
  // update ran to completion while the panel showed nothing). The
  // service's old blockLoading + text() idiom was worse: text() handed
  // back the PREVIOUS file's content, which the desktopId guard turned
  // into silent skips. Both readers must therefore waitForJob() inline,
  // right after pointing the view at a document. Pinned from disk so
  // deleting either call re-arms the hang with every other check green;
  // the FileView canary in scripts/ci/qml-compile-check.sh guards the
  // Quickshell semantics themselves.
  {
    // Comment lines are stripped first: a commented-out call must not
    // satisfy the pin (plant-proven — the naive source match passed with
    // the real call commented out).
    const stripComments = (src) => src.split("\n")
      .filter((line) => !line.trim().startsWith("//")).join("\n")
    const panelJobsSrc = stripComments(readFileSync(
      new URL("../../../PanelJobs.qml", import.meta.url), "utf8"))
    const serviceSrc = stripComments(readFileSync(
      new URL("../../../Service.qml", import.meta.url), "utf8"))
    check("the panel job reader completes its FileView read inline",
      /jobFileView\.path = path[\s\S]{0,900}jobFileView\.waitForJob\(\)/.test(panelJobsSrc),
      "pumpJobReads must call jobFileView.waitForJob() right after the path assignment")
    check("the service job reader completes its FileView read inline",
      /jobView\.waitForJob\(\)[\s\S]{0,80}var text = String\(jobView\.text\(\) \|\| ""\)/.test(serviceSrc),
      "adoptJobDocument must waitForJob() before text()")
    check("the service job view preloads (blockLoading reads stale content)",
      /id: jobView[^}]*preload: true/.test(serviceSrc)
        && !/id: jobView[^}]*blockLoading/.test(serviceSrc))
  }
}

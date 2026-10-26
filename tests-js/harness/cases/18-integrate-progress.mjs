// ---- Ticket #60: the integrate status line shows the transfer --------------
//
// Model.integrateProgressText(event, strings) is the pure text derivation
// over a backend progress document (ticket #58 emitter shape:
// {"event":"progress","phase":...,"bytes":N,"total":T}):
//   - phase "downloading" with total > 0 → "Downloading <pct>%…", the
//     percentage floored and clamped to 100;
//   - phase "downloading" with total === 0 (no Content-Length) → byte form
//     "Downloading <mib> MiB…", one decimal below 10 MiB, integer from
//     10 MiB up (a GiB-scale transfer keeps counting in MiB — simple and
//     deterministic);
//   - phases "verifying" / "installing" → their phase strings;
//   - anything unrecognised → strings.integrateWorking.
// Model.noteIntegrateProgress(line) is the live wire: parse one stdout
// line (non-JSON and non-progress lines are ignored, never an error),
// derive the text, publish it as the store status AND remember it in
// state.integrateProgressStatus so releaseOrphanedRuns() (the v0.9.2
// self-heal) can clear a progress line the killed run left behind — a
// static runStatuses list could not, and a stuck "Downloading 45%…" would
// outlive the reloaded panel forever.

import { Model, check } from "../core.mjs"

export function run() {
  const S = Model.strings

  // ---- pure derivation -----------------------------------------------------
  check("downloading percent", Model.integrateProgressText(
    { phase: "downloading", bytes: 45, total: 100 }, S) === "Downloading 45%…")
  check("percent floors", Model.integrateProgressText(
    { phase: "downloading", bytes: 4599, total: 10000 }, S) === "Downloading 45%…")
  check("percent clamps to 100", Model.integrateProgressText(
    { phase: "downloading", bytes: 250, total: 100 }, S) === "Downloading 100%…")
  check("total zero: sub-10 MiB one decimal", Model.integrateProgressText(
    { phase: "downloading", bytes: 5.5 * 1024 * 1024, total: 0 }, S)
    === "Downloading 5.5 MiB…")
  check("total zero: from 10 MiB integer", Model.integrateProgressText(
    { phase: "downloading", bytes: 45 * 1024 * 1024, total: 0 }, S)
    === "Downloading 45 MiB…")
  check("total zero: GiB scale keeps counting in MiB",
    Model.integrateProgressText(
      { phase: "downloading", bytes: 1536 * 1024 * 1024, total: 0 }, S)
    === "Downloading 1536 MiB…")
  check("total zero: zero bytes", Model.integrateProgressText(
    { phase: "downloading", bytes: 0, total: 0 }, S) === "Downloading 0 MiB…")
  check("verifying phase", Model.integrateProgressText(
    { phase: "verifying" }, S) === S.integrateVerifying)
  check("installing phase", Model.integrateProgressText(
    { phase: "installing" }, S) === S.integrateInstalling)
  // unrecognised → the standing pre-first-event copy
  check("unknown phase falls back", Model.integrateProgressText(
    { phase: "teleporting" }, S) === S.integrateWorking)
  check("missing phase falls back", Model.integrateProgressText({}, S)
    === S.integrateWorking)
  check("null doc falls back", Model.integrateProgressText(null, S)
    === S.integrateWorking)
  check("downloading without bytes falls back", Model.integrateProgressText(
    { phase: "downloading", total: 0 }, S) === S.integrateWorking)
  check("downloading non-numeric bytes falls back",
    Model.integrateProgressText(
      { phase: "downloading", bytes: "many", total: 0 }, S)
    === S.integrateWorking)
  check("negative bytes falls back", Model.integrateProgressText(
    { phase: "downloading", bytes: -1, total: 0 }, S) === S.integrateWorking)

  // ---- live line wiring -----------------------------------------------------
  Model.clearStatus()
  Model.noteIntegrateProgress(
    '{"schema_version":3,"event":"progress","phase":"downloading","bytes":30,"total":100}')
  check("progress line becomes the status", Model.state.status === "Downloading 30%…"
    && Model.state.integrateProgressStatus === "Downloading 30%…",
    JSON.stringify(Model.state.status))
  Model.noteIntegrateProgress("this is not json")
  check("non-JSON line ignored", Model.state.status === "Downloading 30%…")
  Model.noteIntegrateProgress('{"event":"other"}')
  check("non-progress document ignored", Model.state.status === "Downloading 30%…")
  Model.noteIntegrateProgress(
    '{"event":"progress","phase":"verifying"}')
  check("verifying line replaces the download line",
    Model.state.status === S.integrateVerifying
    && Model.state.integrateProgressStatus === S.integrateVerifying)
  Model.noteIntegrateProgress('{"result":"integrated"}')
  check("result line is not a progress event",
    Model.state.status === S.integrateVerifying)

  // ---- v0.9.2 self-heal covers the progress status --------------------------
  // A killed run never reaches onDone, so the panel's reload path must
  // clear a progress line exactly like it clears "Integrating…" — and it
  // must still NOT clear a real outcome.
  Model.releaseOrphanedRuns()
  check("self-heal clears a progress status", Model.state.status === "")
  Model.noteIntegrateProgress(
    '{"event":"progress","phase":"downloading","bytes":1,"total":10}')
  Model.setStatus(S.statusIntegrated)
  Model.releaseOrphanedRuns()
  check("self-heal leaves a real outcome alone",
    Model.state.status === S.statusIntegrated)
  Model.clearStatus()
  check("progress memo cleared by self-heal",
    Model.state.integrateProgressStatus === "")
}

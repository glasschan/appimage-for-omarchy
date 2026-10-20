.pragma library

// Background update-job documents: tolerant reading + adoption decision.
//
// The backend's detached `--update --async` runner publishes one JSON job
// document per app at $XDG_STATE_HOME/io.github.glasschan.appimage/jobs/
// <desktop id stem>.json (backend/CONTRACT.md, "Job documents"; writer:
// backend/omarchy_appimage/jobs.py). This library is the QML-side half of
// that seam: parse → normalize → classify → adopt, all PURE functions —
// no Qt globals, no I/O — so tests-js/parse-harness.mjs can pin the whole
// decision table against real captures in tests-js/captures/job-*.json.
//
// The liveness rule is mirrored from jobs.py / CONTRACT.md: a job is
// ACTIVE iff its phase is non-terminal AND its heartbeat is fresher than
// STALE_AFTER_SECONDS (120 s) AND its pid is alive. `interrupted` is
// NEVER in a document — it is derived here for a non-terminal document
// that fails the rule (or that cannot be parsed at all: a corrupt or
// truncated document classifies the same way, per the contract's
// "unparsable documents classify the same way").
//
// The one thing JS cannot do is the pid probe (signal 0): classifyJob
// takes the pid-alive verdict as a caller-provided boolean. The panel
// currently has no backend call for it, so it passes `true` (undefined
// means the same) and lets the heartbeat rule decide alone; a dead pid
// with a still-fresh heartbeat therefore reads as active for at most the
// 120 s the heartbeat keeps refreshing. If a later backend call exposes
// jobs.active_job()'s verdict, feed its boolean straight in — the
// signature already accepts it.
//
// Deliberate divergence from jobs.classify_job (fail-safe, per the parse
// layer ticket): the backend classifies ANY unknown non-terminal phase by
// liveness alone, so a NEWER backend's new phase would read as active
// here with semantics the UI does not know. This reader refuses: an
// unknown or missing phase classifies `interrupted` regardless of
// liveness — a wrong "retry?" prompt is recoverable (the backend refuses
// a second job while one is active), a wrong progress bar is not.

// ---- contract constants (mirror jobs.py — keep in sync) ---------------------

var STALE_AFTER_SECONDS = 120
var ACTIVE_PHASES = ["starting", "downloading", "verifying", "replacing"]
var TERMINAL_PHASES = ["done", "error"]

// ---- small tolerant coercers -------------------------------------------------

// JSON numbers arrive as ints, floats or strings depending on the
// producer; null/absent fall back instead of coercing to 0 (same rule as
// Model.toInt — duplicated because a .pragma library stays self-contained
// and the harness loads each file in its own sandbox).
function toInt(value, fallback) {
  if (value === null || value === undefined || value === "") return fallback
  var n = Number(value)
  return isFinite(n) ? Math.round(n) : fallback
}

function knownPhase(phase) {
  var p = String(phase || "")
  return ACTIVE_PHASES.indexOf(p) !== -1 || TERMINAL_PHASES.indexOf(p) !== -1
}

// ---- parse -------------------------------------------------------------------

// Tolerant document parse (Model.parseDocument's strategy): strict first,
// then recover the outermost {…} if a stray line sneaks in. A job document
// is always an object; arrays pass Model's typeof-object check, so the
// shape gate lives in normalizeJob where the backend's isinstance(dict)
// rule is mirrored.
function parseJobDocument(text) {
  var raw = String(text || "").trim()
  if (raw === "") return { ok: false, doc: null, error: "Empty job document" }
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { doc = null }
  if (doc === null) {
    var start = raw.indexOf("{")
    var end = raw.lastIndexOf("}")
    if (start >= 0 && end > start) {
      try { doc = JSON.parse(raw.substring(start, end + 1)) } catch (e2) { doc = null }
    }
  }
  if (doc === null || typeof doc !== "object") {
    return { ok: false, doc: null, error: "Job document was not JSON" }
  }
  return { ok: true, doc: doc, error: "" }
}

// ---- normalize ---------------------------------------------------------------
//
// The one shape the UI consumes. Every field is defaulted, strings are
// stringified, numbers are clamped to their contract ranges; a document
// that is not a JSON object (garbage, truncated, array, empty file)
// normalizes to ok:false with the safe empty shape, so downstream
// classify/adopt yield `interrupted` instead of ever throwing.

function normalizeJob(doc) {
  var safe = {
    // false = the document was not a usable JSON object (the caller is
    // looking at a corrupt/empty/truncated file, not at a real job)
    ok: false,
    schemaVersion: 0,
    desktopId: "",
    phase: "",
    phaseKnown: false,
    terminal: false,
    status: "",
    // progress is the trustworthy live signal (CONTRACT.md: total_bytes is
    // wrong for .zsync sources mid-download); clamped to [0, 1]
    progress: 0,
    totalBytes: 0,
    downloadedBytes: 0,
    // 0 = no usable pid (the pid probe itself is the caller's business)
    pid: 0,
    started: 0,
    heartbeat: 0,
    message: "",
    newVersion: "",
    errorText: ""
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return safe
  safe.ok = true
  safe.schemaVersion = toInt(doc.schema_version, 0)
  safe.desktopId = String(doc.desktop_id || "")
  safe.phase = String(doc.phase || "")
  safe.phaseKnown = knownPhase(safe.phase)
  safe.terminal = TERMINAL_PHASES.indexOf(safe.phase) !== -1
  safe.status = String(doc.status || "")
  var progress = Number(doc.progress)
  if (!isFinite(progress)) progress = 0
  safe.progress = Math.min(1, Math.max(0, progress))
  safe.totalBytes = Math.max(0, toInt(doc.total_bytes, 0))
  safe.downloadedBytes = Math.max(0, toInt(doc.downloaded_bytes, 0))
  safe.pid = Math.max(0, toInt(doc.pid, 0))
  safe.started = Math.max(0, toInt(doc.started, 0))
  safe.heartbeat = Math.max(0, toInt(doc.heartbeat, 0))
  safe.message = String(doc.message || "")
  safe.newVersion = String(doc.new_version || "")
  safe.errorText = String(doc.error || "")
  return safe
}

// ---- heartbeat / elapsed helpers ----------------------------------------------
//
// Epoch seconds in the document, epoch millis on the JS side (Date.now()).

// Seconds since the last heartbeat; Number.POSITIVE_INFINITY when the
// document carries no usable heartbeat (that can never be fresh).
function heartbeatAgeSeconds(job, nowMs) {
  var j = job && typeof job === "object" ? job : normalizeJob(null)
  if (!(j.heartbeat > 0)) return Number.POSITIVE_INFINITY
  var age = nowMs / 1000 - j.heartbeat
  return isFinite(age) ? Math.max(0, age) : Number.POSITIVE_INFINITY
}

// The heartbeat half of the liveness rule: age <= STALE_AFTER_SECONDS
// (backend jobs.classify_job uses the same inclusive comparison).
function isHeartbeatFresh(job, nowMs) {
  return heartbeatAgeSeconds(job, nowMs) <= STALE_AFTER_SECONDS
}

// Seconds since the job started (0 when the document says nothing useful).
function elapsedSeconds(job, nowMs) {
  var j = job && typeof job === "object" ? job : normalizeJob(null)
  if (!(j.started > 0)) return 0
  var elapsed = nowMs / 1000 - j.started
  return isFinite(elapsed) ? Math.max(0, elapsed) : 0
}

// ---- classify ------------------------------------------------------------------
//
// 'active' | 'done' | 'error' | 'interrupted' — the mirror of
// jobs.classify_job with the pid probe replaced by the caller's word:
//   pidAlive === false  the pid is dead (a real probe said so)
//   pidAlive === true   the pid is alive
//   anything else       the caller cannot probe — lean on the heartbeat
//                       rule alone (the panel's default today)
// Terminal phases win unconditionally (a terminal document is its own
// verdict, heartbeat and pid irrelevant — same as the backend). An
// unknown or missing phase fails safe to 'interrupted' — see the header
// note on the deliberate divergence from jobs.classify_job.
function classifyJob(job, nowMs, pidAlive) {
  var j = job && typeof job === "object" ? job : normalizeJob(null)
  if (j.phase === "done") return "done"
  if (j.phase === "error") return "error"
  if (!j.phaseKnown) return "interrupted"
  if (!isHeartbeatFresh(j, nowMs)) return "interrupted"
  if (pidAlive === false) return "interrupted"
  return "active"
}

// ---- adopt -----------------------------------------------------------------------
//
// The UI decision: what the panel should show for this document right now.
//   active      progress + message (the live progress row)
//   done        newVersion + message + status (updated / up-to-date /
//               skipped-running — the row's terminal copy branches on it)
//   error       message + errorText (the failure row)
//   interrupted message + stale/pidDead (the retry affordance)
//   idle        only when handed null — no document exists at all; a
//               document that exists but cannot be parsed is NEVER idle,
//               it adopts 'interrupted' (the safe fallback)
// `stale`/`pidDead` say WHY an interruption happened so the retry copy can
// stay honest; `desktopId` lets the caller verify the document belongs to
// the app it read it for before adopting it.
function adoptJob(job, pidAlive, nowMs) {
  var j = job && typeof job === "object" ? job : null
  if (j === null) {
    return {
      state: "idle", desktopId: "", phase: "", progress: 0, message: "",
      newVersion: "", status: "", errorText: "", stale: false,
      pidDead: false, elapsedSeconds: 0
    }
  }
  var state = classifyJob(j, nowMs, pidAlive)
  var decision = {
    state: state,
    desktopId: j.desktopId,
    phase: j.phase,
    progress: j.progress,
    message: j.message,
    newVersion: j.newVersion,
    status: j.status,
    errorText: j.errorText,
    stale: state === "interrupted" && !isHeartbeatFresh(j, nowMs),
    pidDead: state === "interrupted" && pidAlive === false && isHeartbeatFresh(j, nowMs),
    elapsedSeconds: elapsedSeconds(j, nowMs)
  }
  if (state === "done") decision.progress = 1
  return decision
}

// One-call convenience for readers: parse the file text, then adopt.
// Empty, malformed and truncated text all come back as
// { parseError: <why>, decision.state: "interrupted" } — never an
// exception, never idle (idle is for "no document", which a caller that
// has file text in hand cannot claim).
function adoptDocument(text, pidAlive, nowMs) {
  var parsed = parseJobDocument(text)
  var job = normalizeJob(parsed.doc)
  return {
    parseError: parsed.ok ? "" : parsed.error,
    decision: adoptJob(job, pidAlive, nowMs)
  }
}

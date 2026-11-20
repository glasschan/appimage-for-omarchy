// Subprocess plumbing for the AppImage backend CLI.
//
// Quickshell's Process and Timer cannot be created from a .js resource
// (Qt.createQmlObject is unavailable outside QML documents), so the caller
// declares the two objects and hands them to run():
//
//   root.runCtx = Backend.run(backendProc, backendTimer, backendCommand,
//                             ["--list-installed", "--json"], {}, function(result) { ... })
//
// proc's stdout/stderr StdioCollectors must call
// Backend.feedStream(root.runCtx, "stdout", data) from onRead, and its
// onStarted/onExited must call markStarted/handleExit; the Timer's
// onTriggered must call handleTimeout. See Service.qml for a full example.
//
// The command is always an argv array (never a shell string), so paths and
// ids travel as single arguments and are never interpolated into a shell.

var DEFAULT_TIMEOUT_MS = 15000
// Extraction + copy of a large AppImage can take a while; --integrate calls
// pass a longer timeout via options.timeoutMs.
var MAX_ERROR_CHARS = 200
// Producer-side stream bound: every StdioCollector chunk is counted on
// arrival (onRead -> feedStream) and the child is killed as soon as one
// stream exceeds the cap, so a runaway backend buffers at most this much plus
// one chunk. clampStream is the second fence for whatever was already
// buffered at kill time.
var MAX_STREAM_CHARS = 8 * 1024 * 1024

// Build the argv for a backend call. `command` is the base argv (e.g.
// ["python3", "/path/to/backend/main.py"]); `args` are appended verbatim.
function command(command, args) {
  var base = Array.isArray(command) ? command.slice() : []
  var extra = Array.isArray(args) ? args : []
  for (var i = 0; i < extra.length; i++) base.push(String(extra[i]))
  return base
}

// Start `proc` with the built argv and arm `timer` as the watchdog. Returns a
// run context the caller stores and feeds back into handleExit/handleTimeout.
// onDone receives { ok, exitCode, stdout, stderr, json, timedOut, spawnFailed,
// error }. `options.maxStreamChars` (default MAX_STREAM_CHARS) bounds each
// output stream on the producer side; see feedStream.
function run(proc, timer, backendCommand, args, options, onDone) {
  var opts = options || {}
  var maxStreamChars = Number(opts.maxStreamChars)
  var ctx = {
    active: true,
    timedOut: false,
    // Flipped by markStarted (Process.onStarted) so handleExit can tell
    // "ran and failed" from "never ran at all" (e.g. python3 missing).
    started: false,
    proc: proc,
    timer: timer,
    onDone: typeof onDone === "function" ? onDone : function() {},
    startedMs: Date.now(),
    // Streamed output accounting (feedStream).
    stdoutLen: 0,
    stderrLen: 0,
    overflow: false,
    maxStreamChars: maxStreamChars > 0 ? maxStreamChars : MAX_STREAM_CHARS,
    // Line reader: options.onStdoutLine(line) fires for every COMPLETE stdout
    // line, trailing newline stripped; a line split across onRead chunks is
    // buffered until its newline arrives. Absent = no buffering.
    stdoutLineBuf: "",
    onStdoutLine: typeof opts.onStdoutLine === "function" ? opts.onStdoutLine : null
  }
  var argv = command(backendCommand, args)
  if (argv.length === 0) {
    ctx.active = false
    ctx.onDone({ ok: false, exitCode: -1, stdout: "", stderr: "", json: null, timedOut: false, error: "no-backend-command" })
    return ctx
  }
  proc.command = argv
  if (timer) {
    timer.interval = Math.max(1000, Number(opts.timeoutMs) || DEFAULT_TIMEOUT_MS)
    timer.restart()
  }
  proc.running = true
  return ctx
}

// Process.onStarted handler: proof the command actually spawned (python3
// exists, the script path resolves).
function markStarted(ctx) {
  if (ctx) ctx.started = true
}

// StdioCollector.onRead handler (`which` is "stdout" or "stderr"): account for
// a chunk the moment it ARRIVES, and once one stream runs past the cap kill
// the child. Quickshell's Process has no terminate()/kill(), so setting
// `running: false` IS the kill. The watchdog is stopped with the kill so a
// late handleTimeout cannot misattribute the failure as "timeout"; the real
// exit then reaches handleExit as error:"output-overflow".
function feedStream(ctx, which, chunk) {
  if (!ctx) return
  var text = String(chunk || "")
  if (text === "") return
  var key = which === "stderr" ? "stderrLen" : "stdoutLen"
  ctx[key] += text.length
  if (which !== "stderr" && ctx.onStdoutLine) dispatchLine(ctx, text)
  if (ctx[key] > ctx.maxStreamChars && !ctx.overflow) {
    ctx.overflow = true
    if (ctx.timer) ctx.timer.stop()
    if (ctx.proc) ctx.proc.running = false
  }
}

// The stdout half of the line reader: a JSON line may arrive split across any
// number of onRead chunks, so only the COMPLETE lines are dispatched.
function dispatchLine(ctx, chunk) {
  ctx.stdoutLineBuf += chunk
  for (;;) {
    var nl = ctx.stdoutLineBuf.indexOf("\n")
    if (nl < 0) break
    var line = ctx.stdoutLineBuf.substring(0, nl)
    ctx.stdoutLineBuf = ctx.stdoutLineBuf.substring(nl + 1)
    ctx.onStdoutLine(line)
  }
}

// Clamp a stream to MAX_STREAM_CHARS (the final fence applied to whatever a
// collector had already buffered when the child exited or was killed).
function clampStream(text) {
  var value = String(text || "")
  return value.length > MAX_STREAM_CHARS
    ? value.substring(0, MAX_STREAM_CHARS)
    : value
}

// Process.onExited handler. StdioCollector with waitForEnd guarantees both
// texts are complete by the time exited fires. The stdout JSON is parsed on
// failure exits too: the backend's error documents ({"result":"error",...})
// arrive with exit != 0 and carry the message the panel should show.
function handleExit(ctx, exitCode, stdout, stderr) {
  if (!ctx || !ctx.active) return
  ctx.active = false
  if (ctx.timer) ctx.timer.stop()
  var outText = clampStream(stdout)
  var errText = clampStream(stderr)
  if (ctx.timedOut) {
    ctx.onDone({ ok: false, exitCode: exitCode, stdout: outText, stderr: errText, json: null, timedOut: true, spawnFailed: false, error: "timeout" })
    return
  }
  // feedStream killed the child for exceeding a stream bound: report the
  // overflow instead of parsing output we already know is truncated garbage.
  if (ctx.overflow) {
    ctx.onDone({ ok: false, exitCode: exitCode, stdout: outText, stderr: errText, json: null, timedOut: false, spawnFailed: !ctx.started, error: "output-overflow" })
    return
  }
  ctx.onDone({
    ok: exitCode === 0,
    exitCode: exitCode,
    stdout: outText,
    stderr: errText,
    json: parseJson(outText),
    timedOut: false,
    spawnFailed: !ctx.started,
    error: exitCode === 0 ? "" : "exit-" + exitCode
  })
}

// Watchdog handler: report the timeout immediately and kill the process;
// a late onExited for the killed process finds ctx.active false and is
// ignored, so onDone fires exactly once either way.
function handleTimeout(ctx) {
  if (!ctx || !ctx.active) return
  ctx.timedOut = true
  ctx.active = false
  if (ctx.proc) ctx.proc.running = false
  ctx.onDone({ ok: false, exitCode: -1, stdout: "", stderr: "", json: null, timedOut: true, spawnFailed: false, error: "timeout" })
}

// A progress-event document is never a result, however the stream shaped it.
function isEventDocument(doc) {
  return doc !== null && typeof doc === "object" && !Array.isArray(doc)
    && typeof doc.event === "string" && doc.event !== ""
}

// Tolerant JSON parse for backend stdout: the CLI contract is "stdout is only
// JSON" (logging goes to stderr), but --integrate --json may stream NDJSON
// progress documents ahead of the result. Strategy: strict whole-text parse
// FIRST (a multi-line pretty-printed document must not be run through the line
// pass), then per line where the LAST complete valid non-event document wins,
// then the outermost {…}/[…] recovery. Returns null when nothing parses to a
// non-event document.
function parseJson(text) {
  var raw = String(text || "").trim()
  if (raw === "") return null
  try {
    var whole = JSON.parse(raw)
    if (!isEventDocument(whole)) return whole
    // an event document that parsed whole: never a result; fall through
    // so the line pass and recovery get their chance (both event-skip).
  } catch (e) { /* NDJSON stream or garbage */ }
  var lines = raw.split("\n")
  var doc = null
  if (lines.length > 1) {
    // NDJSON-shaped verdict: once any complete line parsed as JSON (even one
    // skipped as a progress event), the whole-text recovery below must not
    // resurrect a skipped event as the result.
    var sawLineDocument = false
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i].trim()
      if (line === "") continue
      var candidate = null
      try { candidate = JSON.parse(line) } catch (e) { /* progress or stray line */ }
      if (candidate !== null) sawLineDocument = true
      // An event document is skipped; a failed line never overwrites the
      // accumulator ("last valid document wins", not "last line wins").
      if (isEventDocument(candidate)) continue
      if (candidate !== null) doc = candidate
    }
    if (doc === null && sawLineDocument) return null
    if (doc !== null) return doc
  }
  var start = raw.indexOf("{")
  var startArray = raw.indexOf("[")
  if (startArray !== -1 && (start === -1 || startArray < start)) start = startArray
  var end = raw.lastIndexOf("}")
  var endArray = raw.lastIndexOf("]")
  if (endArray !== -1 && endArray > end) end = endArray
  if (start < 0 || end <= start) return null
  try {
    var recovered = JSON.parse(raw.substring(start, end + 1))
    return isEventDocument(recovered) ? null : recovered
  } catch (e2) { return null }
}

// Clamp a raw process output line to something a panel row can display.
function elide(text, max) {
  var limit = Number(max) || MAX_ERROR_CHARS
  var value = String(text || "").replace(/\s+/g, " ").trim()
  return value.length > limit ? value.substring(0, limit - 1) + "…" : value
}

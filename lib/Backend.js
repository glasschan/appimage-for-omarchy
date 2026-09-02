// Subprocess plumbing for the AppImage backend CLI.
//
// Quickshell's Process and Timer cannot be created from a .js resource
// (Qt.createQmlObject is unavailable outside QML documents), so the caller
// declares the two objects declaratively and hands them to run():
//
//   Process {
//     id: backendProc
//     stdout: StdioCollector {
//       id: backendStdout
//       waitForEnd: true
//       // Producer-side bound: StdioCollector extends DataStreamParser,
//       // whose read(string) signal fires per chunk — every chunk is
//       // counted as it ARRIVES and the child is killed once a stream
//       // runs past the cap (see feedStream), instead of letting the
//       // collector buffer an unbounded producer before any check.
//       onRead: function(data) { Backend.feedStream(root.runCtx, "stdout", data) }
//     }
//     stderr: StdioCollector {
//       id: backendStderr
//       waitForEnd: true
//       onRead: function(data) { Backend.feedStream(root.runCtx, "stderr", data) }
//     }
//     onExited: function(exitCode) {
//       Backend.handleExit(root.runCtx, exitCode, backendStdout.text, backendStderr.text)
//     }
//   }
//   Timer {
//     id: backendTimer
//     repeat: false
//     onTriggered: Backend.handleTimeout(root.runCtx)
//   }
//
//   root.runCtx = Backend.run(backendProc, backendTimer, backendCommand,
//                             ["--list-installed", "--json"], {}, function(result) { ... })
//
// The command is always an argv array (never a shell string), so paths and
// ids travel as single arguments and are never interpolated into a shell.
// See lib/Model.js for the shared store; the backend command itself comes
// from the caller (Panel.qml derives it from the plugin's manifest source
// dir or the `backendCommand` setting) — nothing is hardcoded here.

var DEFAULT_TIMEOUT_MS = 15000
// Extraction + copy of a large AppImage can legitimately take a while;
// --integrate calls pass a longer timeout via options.timeoutMs.
var MAX_ERROR_CHARS = 200
// Stream bound, enforced on the producer side: every StdioCollector chunk
// is counted on arrival (onRead -> feedStream) and the child is killed as
// soon as one stream exceeds the cap, so a runaway backend can make the
// shell buffer at most this much plus one chunk. clampStream stays as the
// second fence for whatever was already buffered at kill time.
var MAX_STREAM_CHARS = 8 * 1024 * 1024

// Build the argv for a backend call. `command` is the base argv (e.g.
// ["python3", "/path/to/backend/main.py"]); `args` are appended verbatim.
function command(command, args) {
  var base = Array.isArray(command) ? command.slice() : []
  var extra = Array.isArray(args) ? args : []
  for (var i = 0; i < extra.length; i++) base.push(String(extra[i]))
  return base
}

// Start `proc` with the built argv and arm `timer` as the watchdog.
// Returns a run context the caller stores (root.runCtx above) and feeds
// back into handleExit/handleTimeout. onDone receives a result object:
//   { ok, exitCode, stdout, stderr, json, timedOut, spawnFailed, error }
// `options.maxStreamChars` (default MAX_STREAM_CHARS) bounds each output
// stream on the producer side; see feedStream.
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
    // Streamed output accounting (feedStream): bytes seen per stream and
    // the once-only overflow kill switch.
    stdoutLen: 0,
    stderrLen: 0,
    overflow: false,
    maxStreamChars: maxStreamChars > 0 ? maxStreamChars : MAX_STREAM_CHARS
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
// exists, the script path resolves). A process that exits without ever
// starting is reported as spawnFailed instead of a plain exit code.
function markStarted(ctx) {
  if (ctx) ctx.started = true
}

// StdioCollector.onRead handler (which is "stdout" or "stderr"): account
// for a chunk the moment it ARRIVES, and once one stream runs past the
// cap kill the child instead of letting it keep producing into an
// unbounded collector buffer. Quickshell's Process has no terminate()/
// kill() — setting `running: false` IS the kill, exactly what
// handleTimeout does. The watchdog is stopped with the kill so a late
// handleTimeout can never misattribute the failure as "timeout": the
// process's real exit then reaches handleExit, which delivers
// error:"output-overflow". Safe no-op on a null ctx (e.g. a chunk racing
// the ctx teardown) and node-testable (no QML imports).
function feedStream(ctx, which, chunk) {
  if (!ctx) return
  var text = String(chunk || "")
  if (text === "") return
  var key = which === "stderr" ? "stderrLen" : "stdoutLen"
  ctx[key] += text.length
  if (ctx[key] > ctx.maxStreamChars && !ctx.overflow) {
    ctx.overflow = true
    if (ctx.timer) ctx.timer.stop()
    if (ctx.proc) ctx.proc.running = false
  }
}

// Clamp a stream to MAX_STREAM_CHARS (the final fence applied to whatever
// a collector had already buffered when the child exited or was killed;
// see the constant's comment above).
function clampStream(text) {
  var value = String(text || "")
  return value.length > MAX_STREAM_CHARS
    ? value.substring(0, MAX_STREAM_CHARS)
    : value
}

// Process.onExited handler. StdioCollector with waitForEnd guarantees both
// texts are complete by the time exited fires (the pattern the first-party
// dropbox plugin uses). The stdout JSON is parsed on failure exits too:
// the backend's error documents ({"result":"error",...}) arrive with
// exit != 0 and carry the message the panel should show.
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
  // overflow instead of parsing output we already know is truncated
  // garbage (clampStream remains the final fence on what is reported).
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

// Tolerant JSON parse for backend stdout: the CLI contract is "stdout is
// only JSON" (logging goes to stderr), but recover gracefully if a stray
// line sneaks in front of the payload. Returns null when nothing parses.
function parseJson(text) {
  var raw = String(text || "").trim()
  if (raw === "") return null
  try { return JSON.parse(raw) } catch (e) { /* fall through */ }
  var start = raw.indexOf("{")
  var startArray = raw.indexOf("[")
  if (startArray !== -1 && (start === -1 || startArray < start)) start = startArray
  var end = raw.lastIndexOf("}")
  var endArray = raw.lastIndexOf("]")
  if (endArray !== -1 && endArray > end) end = endArray
  if (start < 0 || end <= start) return null
  try { return JSON.parse(raw.substring(start, end + 1)) } catch (e2) { return null }
}

// Clamp a raw process output line to something a panel row can display.
function elide(text, max) {
  var limit = Number(max) || MAX_ERROR_CHARS
  var value = String(text || "").replace(/\s+/g, " ").trim()
  return value.length > limit ? value.substring(0, limit - 1) + "…" : value
}

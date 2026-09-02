// Subprocess plumbing for the AppImage backend CLI.
//
// Quickshell's Process and Timer cannot be created from a .js resource
// (Qt.createQmlObject is unavailable outside QML documents), so the caller
// declares the two objects declaratively and hands them to run():
//
//   Process {
//     id: backendProc
//     stdout: StdioCollector { id: backendStdout; waitForEnd: true }
//     stderr: StdioCollector { id: backendStderr; waitForEnd: true }
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
// Consumer-side bound: StdioCollector retains everything a child prints,
// so cap what reaches the parsers/log. A truncated answer parses as
// garbage (parseJson returns null) and lands in the existing failure
// path — the backend itself caps its subprocess output, this is the
// second fence.
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
function run(proc, timer, backendCommand, args, options, onDone) {
  var opts = options || {}
  var ctx = {
    active: true,
    timedOut: false,
    // Flipped by markStarted (Process.onStarted) so handleExit can tell
    // "ran and failed" from "never ran at all" (e.g. python3 missing).
    started: false,
    proc: proc,
    timer: timer,
    onDone: typeof onDone === "function" ? onDone : function() {},
    startedMs: Date.now()
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

// Clamp a stream to MAX_STREAM_CHARS (consumer-side memory fence; see
// the constant's comment above).
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

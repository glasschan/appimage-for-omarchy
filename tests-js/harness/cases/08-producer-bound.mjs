// ---- Backend.js streamed producer bound (security round 2) -----------------
// StdioCollector.onRead feeds every chunk through Backend.feedStream; past
// the per-stream cap the child is killed via proc.running = false (same
// kill path as handleTimeout — Quickshell Process has no terminate()).

import { Backend, check } from "../core.mjs"
import { fakeProc, fakeTimer } from "../fakes.mjs"

export function run() {
  const timer = fakeTimer()
  let doneResults = []
  const onDone = (r) => doneResults.push(r)

  // ctx accounting fields + options override
  {
    const proc = fakeProc()
    const ctx = Backend.run(proc, timer, ["python3", "main.py"], ["--json"], { maxStreamChars: 10 }, onDone)
    check("run ctx stream accounting", ctx.stdoutLen === 0 && ctx.stderrLen === 0
      && ctx.overflow === false, JSON.stringify(ctx))
    check("run maxStreamChars override", ctx.maxStreamChars === 10, String(ctx.maxStreamChars))
    const ctxDefault = Backend.run(fakeProc(), timer, ["python3"], [], {}, onDone)
    check("run maxStreamChars default", ctxDefault.maxStreamChars === Backend.MAX_STREAM_CHARS,
      String(ctxDefault.maxStreamChars))

    // chunks under the cap accumulate; no kill
    Backend.feedStream(ctx, "stdout", "aaaaaaaaaa") // exactly 10
    check("feedStream under cap no kill", ctx.stdoutLen === 10
      && ctx.overflow === false && proc.killCount === 0, JSON.stringify(ctx))
    // the chunk that pushes past the cap flips overflow, stops the
    // watchdog (so the timer can never fire handleTimeout and misattribute
    // the failure as "timeout") and kills the child exactly once
    Backend.feedStream(ctx, "stdout", "bbbb")
    check("feedStream past cap kills once + stops watchdog", ctx.overflow === true
      && proc.killCount === 1 && proc.running === false && timer.stopped === true,
      JSON.stringify(ctx))
    // later chunks never double-kill
    Backend.feedStream(ctx, "stderr", "err!!")
    check("feedStream overflow once only", proc.killCount === 1 && ctx.stderrLen === 5,
      JSON.stringify(ctx))
    // null ctx is a safe no-op
    let threw = false
    try { Backend.feedStream(null, "stdout", "ignored") } catch (e) { threw = true }
    check("feedStream null ctx no-op", threw === false)

    // with the watchdog already stopped, the real process exit reaches
    // handleExit, which attributes the failure to the overflow — not a
    // timeout — exactly once
    Backend.handleExit(ctx, 0, "truncated-garbage", "stderr-leftover")
    Backend.handleExit(ctx, 0, "truncated-garbage", "stderr-leftover") // late exit ignored
    const r = doneResults[0]
    check("handleExit overflow shape", r && r.ok === false && r.json === null
      && r.timedOut === false && r.spawnFailed === true && r.error === "output-overflow",
      JSON.stringify(r))
    check("handleExit overflow clamp + once", r.stdout === "truncated-garbage"
      && r.stderr === "stderr-leftover" && doneResults.length === 1,
      JSON.stringify(doneResults))
  }
}

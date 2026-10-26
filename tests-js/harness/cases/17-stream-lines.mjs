// ---- Ticket #59: the panel bridge reads backend stdout line by line --------
// feedStream dispatches complete stdout lines (newline stripped, partials
// buffered) to run()'s options.onStdoutLine; the producer-side cap and
// overflow kill are unchanged. Model's document parsers pick the LAST
// complete valid document from an NDJSON stream, so progress lines ahead
// of the result cannot break result parsing.

import { Backend, Model, check } from "../core.mjs"
import { fakeProc, fakeTimer } from "../fakes.mjs"

export function run() {
  const timer = fakeTimer()
  const onDone = () => {}

  // line reader: complete lines only, trailing newline stripped
  {
    const proc = fakeProc()
    let lines = []
    const ctx = Backend.run(proc, timer, ["python3", "main.py"], ["--json"],
      { onStdoutLine: (l) => lines.push(l) }, onDone)
    Backend.feedStream(ctx, "stdout", '{"event":"progress"')
    check("no line until newline", lines.length === 0, JSON.stringify(lines))
    Backend.feedStream(ctx, "stdout", "}\n{\"event\":\"d")
    check("split line dispatched once complete", lines.length === 1
      && lines[0] === '{"event":"progress"}', JSON.stringify(lines))
    Backend.feedStream(ctx, "stdout", "one\"}\n")
    check("split line dispatched once complete", lines.length === 2
      && lines[1] === '{"event":"done"}', JSON.stringify(lines))
    Backend.feedStream(ctx, "stdout", "\n")
    check("empty line dispatched", lines.length === 3 && lines[2] === "",
      JSON.stringify(lines))
    Backend.feedStream(ctx, "stdout", "trailing-partial")
    check("partial tail stays buffered", lines.length === 3, JSON.stringify(lines))
  }

  // no reader attached: a caller without onStdoutLine is unaffected
  {
    const proc = fakeProc()
    const ctx = Backend.run(proc, timer, ["python3"], [], {}, onDone)
    let threw = false
    try { Backend.feedStream(ctx, "stdout", "no reader here\n") } catch (e) { threw = true }
    check("feedStream without reader unaffected", threw === false
      && ctx.stdoutLen === 15, JSON.stringify(ctx))
  }

  // cap accounting + overflow kill keep working with a reader attached
  {
    const proc = fakeProc()
    let lines = []
    const ctx = Backend.run(proc, timer, ["python3"], [],
      { maxStreamChars: 15, onStdoutLine: (l) => lines.push(l) }, onDone)
    Backend.feedStream(ctx, "stdout", "aaaaaaaaaa\n")
    check("reader under cap no kill", ctx.overflow === false
      && proc.killCount === 0 && lines.length === 1, JSON.stringify(ctx))
    Backend.feedStream(ctx, "stdout", "bbbb\n")
    check("reader past cap kills once + stops watchdog", ctx.overflow === true
      && proc.killCount === 1 && proc.running === false && timer.stopped === true
      && lines.length === 2, JSON.stringify(ctx))
  }

  // Model.parseDocument: progress-then-result, last complete document wins
  {
    const result = { result: "integrated", app: { name: "Foo" } }
    const stream = '{"event":"progress","percent":10}\n'
      + '{"event":"progress","percent":90}\n'
      + JSON.stringify(result) + "\n"
    const parsed = Model.parseDocument(stream)
    check("parseDocument progress-then-result", parsed.ok === true
      && parsed.doc.result === "integrated" && parsed.doc.app.name === "Foo",
      JSON.stringify(parsed))

    const err = Model.parseDocument('{"event":"progress","percent":1}\n'
      + '{"result":"error","error":"download-failed"}\n')
    check("parseDocument error after progress", err.ok === true
      && err.doc.result === "error" && err.doc.error === "download-failed",
      JSON.stringify(err))

    const single = Model.parseDocument('{"result":"updated"}\n')
    check("parseDocument single document unchanged", single.ok === true
      && single.doc.result === "updated", JSON.stringify(single))

    const junk = Model.parseDocument('not json at all\n'
      + '{"result":"removed"}\n')
    check("parseDocument skips non-JSON lines", junk.ok === true
      && junk.doc.result === "removed", JSON.stringify(junk))

    const list = Model.parseDocument('{"event":"progress"}\n[{"name":"A"}]\n')
    check("parseDocument array result after progress", list.ok === true
      && Array.isArray(list.doc) && list.doc[0].name === "A", JSON.stringify(list))

    const bad = Model.parseDocument('{"event":"progress"}\n{broken\n')
    check("parseDocument nothing valid stays a failure", bad.ok === false,
      JSON.stringify(bad))
  }

  // parseActionJson over a progress stream: the action envelope comes from
  // the last valid document
  {
    const a = Model.parseActionJson('{"event":"progress","percent":50}\n'
      + '{"result":"integrated","message":"ok","app":{"name":"Foo","path":"/tmp/Foo.AppImage"}}\n')
    check("parseActionJson progress-then-result", a.ok === true
      && a.result === "integrated" && a.app && a.app.name === "Foo",
      JSON.stringify(a))
    const e = Model.parseActionJson('{"event":"progress"}\n'
      + '{"result":"error","error":"no-space"}\n')
    check("parseActionJson error after progress", e.ok === false
      && e.result === "error" && e.error === "no-space", JSON.stringify(e))
  }

  // Review round (spec #30 wave): "last valid document wins" must mean the
  // last VALID document — a stray unparseable line, a single-line event
  // stream and legacy recovery must never produce or erase a verdict.
  {
    const result = JSON.stringify({ result: "integrated", app: { name: "Foo" } })

    const stray = Model.parseDocument(result + '\n{broken\n')
    check("parseDocument result survives stray line", stray.ok === true
      && stray.doc.result === "integrated", JSON.stringify(stray))
    const strayB = Backend.parseJson(result + '\n{broken\n')
    check("parseJson result survives stray line", strayB !== null
      && strayB.result === "integrated", JSON.stringify(strayB))

    const pretty = Model.parseDocument('[\n 1,\n 2\n]')
    check("parseDocument multi-line pretty array", pretty.ok === true
      && Array.isArray(pretty.doc) && pretty.doc[0] === 1 && pretty.doc[1] === 2,
      JSON.stringify(pretty))
    const prettyB = Backend.parseJson('[\n 1,\n 2\n]')
    check("parseJson multi-line pretty array", Array.isArray(prettyB)
      && prettyB[0] === 1 && prettyB[1] === 2, JSON.stringify(prettyB))

    const singleEvent = Model.parseDocument('{"event":"progress","percent":40}')
    check("parseDocument single progress line is a failure", singleEvent.ok === false,
      JSON.stringify(singleEvent))
    check("parseJson single progress line is null",
      Backend.parseJson('{"event":"progress","percent":40}') === null)

    const recovered = Model.parseDocument('x\n{"event":"progress"}junk')
    check("parseDocument recovery skips event doc", recovered.ok === false,
      JSON.stringify(recovered))
  }
}

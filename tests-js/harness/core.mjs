// Shared harness core: loads the QML library scripts (lib/*.js) the way QML
// exposes them, owns the PASS/FAIL printer and its failure tally, reads the
// frozen e2e captures, and parses the fixtures more than one case module
// needs. Nothing here touches the store: the fixtures are pure parse results.
import { readFileSync } from "node:fs"
import vm from "node:vm"

export const load = (path) => {
  // .pragma library is QML-only; strip the pragma line and run the rest in
  // a sandbox so top-level function/var declarations land on the sandbox
  // object, mirroring how QML exposes a library script's members.
  const src = readFileSync(path, "utf8").replace(/^\.pragma library\s*/m, "")
  const sandbox = {}
  vm.runInNewContext(src, sandbox, { filename: path })
  return sandbox
}

export const Model = load(new URL("../../lib/Model.js", import.meta.url).pathname)
export const Backend = load(new URL("../../lib/Backend.js", import.meta.url).pathname)
export const JobDocs = load(new URL("../../lib/JobDocs.js", import.meta.url).pathname)

export const cap = (f) => readFileSync(new URL("../captures/" + f, import.meta.url), "utf8")

let failures = 0
export const check = (name, cond, detail) => {
  if (cond) console.log("PASS " + name)
  else { failures++; console.log("FAIL " + name + (detail ? " — " + detail : "")) }
}
export const failureCount = () => failures

// The frozen neovim listing and the two-app update sweep are each read by
// more than one case module, so they are parsed once here.
export const list = Model.parseListJson(cap("list.json"))
export const updates = Model.parseUpdatesJson(cap("updates.json"))

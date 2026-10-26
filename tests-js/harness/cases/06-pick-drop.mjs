// ---- F2 browse: --pick-file outcome documents (#19) -----------------------------
// Contract shapes (backend/CONTRACT.md "### --pick-file [--json]"): picked /
// cancelled / unavailable are exit-0 results, typed errors are exit-1
// documents with a reason key. No captures here — every variant needs a
// real dialog session (a portal reply, a user cancel), so they are inline
// literals, same as the offline fetch-updates case above.

import { Model, check } from "../core.mjs"

export function run() {
  const pickedPortal = Model.parsePickJson(
    '{"schema_version": 1, "result": "picked", "path": "/home/user/Downloads/nvim-linux-x86_64.appimage", "backend": "portal"}')
  check("pick picked ok", pickedPortal.ok === true && pickedPortal.result === "picked")
  check("pick picked path", pickedPortal.path === "/home/user/Downloads/nvim-linux-x86_64.appimage",
    pickedPortal.path)
  check("pick picked backend", pickedPortal.backend === "portal", pickedPortal.backend)

  const pickedZenity = Model.parsePickJson(
    '{"schema_version": 1, "result": "picked", "path": "/opt/x.AppImage", "backend": "zenity"}')
  check("pick zenity backend", pickedZenity.ok === true && pickedZenity.backend === "zenity"
    && pickedZenity.path === "/opt/x.AppImage", JSON.stringify(pickedZenity))

  const cancelled = Model.parsePickJson(
    '{"schema_version": 1, "result": "cancelled", "backend": "zenity"}')
  check("pick cancelled ok", cancelled.ok === true && cancelled.result === "cancelled")
  check("pick cancelled has no path", cancelled.path === "", cancelled.path)

  const unavailable = Model.parsePickJson(
    '{"schema_version": 1, "result": "unavailable", "message": '
    + '"no file chooser is available (neither the desktop portal nor zenity); '
    + 'type or paste the path instead"}')
  check("pick unavailable ok", unavailable.ok === true && unavailable.result === "unavailable")
  check("pick unavailable message surfaced", unavailable.message.includes("type or paste"),
    unavailable.message)

  const pickTimeout = Model.parsePickJson(
    '{"schema_version": 1, "result": "error", "reason": "timeout", '
    + '"error": "the file dialog did not answer in time"}')
  check("pick timeout error", pickTimeout.ok === false && pickTimeout.result === "error"
    && pickTimeout.reason === "timeout")
  check("pick timeout message surfaced", pickTimeout.error.includes("did not answer in time"),
    pickTimeout.error)

  const pickBackendFailed = Model.parsePickJson(
    '{"schema_version": 1, "result": "error", "reason": "backend-failed", '
    + '"error": "the file dialog crashed"}')
  check("pick backend-failed reason", pickBackendFailed.ok === false
    && pickBackendFailed.reason === "backend-failed")

  const pickNotLocal = Model.parsePickJson(
    '{"schema_version": 1, "result": "error", "reason": "not-local", '
    + '"error": "the file dialog returned a non-local location"}')
  check("pick not-local reason", pickNotLocal.ok === false && pickNotLocal.reason === "not-local")

  // Defensive mapping: unknown envelope keys are tolerated, unknown results
  // pass through like parseActionJson's (the QML layer routes on the named
  // results and treats everything else as a failure), and garbage is rejected.
  const pickUnknownKeys = Model.parsePickJson(
    '{"schema_version": 1, "result": "picked", "path": "/x/a.appimage", '
    + '"backend": "portal", "future_key": {"nested": true}}')
  check("pick unknown keys tolerated", pickUnknownKeys.ok === true
    && pickUnknownKeys.path === "/x/a.appimage", JSON.stringify(pickUnknownKeys))
  const pickUnknownResult = Model.parsePickJson(
    '{"schema_version": 1, "result": "something-new"}')
  check("pick unknown result passthrough", pickUnknownResult.ok === true
    && pickUnknownResult.result === "something-new")
  check("pick garbage rejected", Model.parsePickJson("zenity: not found\n").ok === false
    && Model.parsePickJson("zenity: not found\n").error !== "")

  // ---- Drop-to-integrate: dropUriToPath (#21) ----------------------------------
  // The bar widget's and the panel's DropAreas hand their URI lists through
  // this pure helper before anything touches the picker: only file:// URIs
  // are usable (URL-text drops are out of scope by the spec), the local path
  // is percent-decoded, a multi-URI selection picks the FIRST entry ending
  // in .AppImage (case-insensitive) and reports the rest as ignored, and
  // every unusable input is a typed rejection (reason "empty" / "not-file" /
  // "no-appimage") — a drop can never manufacture an install path.
  const drop = (uris) => Model.dropUriToPath(uris)

  const dropOk = drop(["file:///home/u/Tools/My.AppImage"])
  check("drop valid file URI", dropOk.ok === true
    && dropOk.path === "/home/u/Tools/My.AppImage"
    && dropOk.ignored === 0 && dropOk.reason === "", JSON.stringify(dropOk))

  const dropEncoded = drop(["file:///home/u/My%20Tools/App%20One.appimage"])
  check("drop URL-encoded path decodes", dropEncoded.ok === true
    && dropEncoded.path === "/home/u/My Tools/App One.appimage", JSON.stringify(dropEncoded))
  check("drop lowercase extension accepted", /\.appimage$/i.test(dropEncoded.path))

  const dropString = drop("file:///x/Solo.AppImage")
  check("drop single string input accepted", dropString.ok === true
    && dropString.path === "/x/Solo.AppImage", JSON.stringify(dropString))

  check("drop empty list → empty", JSON.stringify(drop([]))
    === JSON.stringify({ ok: false, path: "", ignored: 0, reason: "empty" }))
  check("drop empty string → empty", drop("").ok === false && drop("").reason === "empty")
  check("drop whitespace-only entries → empty",
    drop(["", "   "]).ok === false && drop(["", "   "]).reason === "empty")

  check("drop bare-root URI → no-appimage",
    drop(["file:///"]).ok === false && drop(["file:///"]).reason === "no-appimage")
  check("drop trailing-slash dir URI → no-appimage",
    drop(["file:///home/u/dir/"]).ok === false
    && drop(["file:///home/u/dir/"]).reason === "no-appimage")

  check("drop https rejected", drop(["https://example.com/app.AppImage"]).ok === false
    && drop(["https://example.com/app.AppImage"]).reason === "not-file")
  check("drop http rejected", drop(["http://example.com/app.AppImage"]).ok === false
    && drop(["http://example.com/app.AppImage"]).reason === "not-file")
  check("drop plain text garbage rejected", drop(["hello world"]).ok === false
    && drop(["hello world"]).reason === "not-file")
  check("drop file://host path rejected (not a local path)",
    drop(["file://server/share/App.AppImage"]).ok === false
    && drop(["file://server/share/App.AppImage"]).reason === "not-file")
  check("drop malformed percent rejected", drop(["file:///a/%zz.AppImage"]).ok === false
    && drop(["file:///a/%zz.AppImage"]).reason === "not-file")

  const dropMulti = drop([
    "file:///home/u/notes.txt",
    "file:///home/u/First%20App.AppImage",
    "file:///home/u/Second.AppImage",
    "https://example.com/third.AppImage"
  ])
  check("drop multi-URI first-AppImage policy", dropMulti.ok === true
    && dropMulti.path === "/home/u/First App.AppImage"
    && dropMulti.ignored === 3 && dropMulti.reason === "", JSON.stringify(dropMulti))

  const dropNone = drop(["file:///home/u/notes.txt", "file:///home/u/readme.md"])
  check("drop no-AppImage list typed", dropNone.ok === false
    && dropNone.reason === "no-appimage" && dropNone.path === ""
    && dropNone.ignored === 2, JSON.stringify(dropNone))
}

.pragma library

// Shared state store for the AppImage plugin.
//
// `.pragma library` makes this a single engine-wide instance, so the bar
// widget (badge counts) and every panel instance read and write the same
// state no matter which QML document imported the file. Plain QML/JS has
// no property-change notifications for library scripts, so mutations go
// through the functions below and are announced to `subscribe()`d
// listeners, which mirror the values into their own reactive properties.
//
// Real backend (M1b): items come from `backend/main.py --list-installed
// --json` (see backend/CONTRACT.md, schema_version 1). The store keeps
// the last response as the cache — panels render it instantly and call
// refresh() for a background update (PRD §8: never block the UI).

// ---- UI strings (single table; future i18n swaps this object) -------------

var strings = {
  barTooltip: "AppImages",
  barTooltipCount: "AppImages (%1 installed)",
  panelTitle: "AppImages",
  integrate: "Integrate",
  refresh: "Refresh",
  launch: "Launch",
  removeTooltip: "Remove",
  removeConfirm: "Remove?",
  dismiss: "Dismiss",
  loading: "Loading…",
  emptyTitle: "No AppImages integrated",
  emptyHint: "Download an AppImage, then press Integrate to add it to your app menu.",
  runningLabel: "running",
  itemsOne: "1 AppImage",
  itemsMany: "%1 AppImages",
  runningCount: "%1 running",
  timeout: "Backend timed out",
  backendFailed: "Backend call failed",
  // Covers "command could not start" (e.g. python3 missing) and watchdog
  // timeouts alike: both mean the backend never produced output.
  backendMissing: "Backend did not run — is python3 installed? Try: omarchy pkg add python",
  integrateWorking: "Integrating…",
  statusIntegrated: "%1 integrated",
  statusAlreadyIntegrated: "Already integrated",
  pickerTitle: "Integrate an AppImage",
  pickerPlaceholder: "Path to a .AppImage file…",
  pickerDownloads: "From Downloads",
  pickerDownloadsEmpty: "No AppImages found in Downloads",
  pickerGo: "Integrate",
  pickerCancel: "Cancel"
}

function formatCount(n, template) {
  return String(template).replace("%1", String(n))
}

// ---- state -----------------------------------------------------------------

var state = {
  // Normalized items: { id, name, version, path, running, iconBase, updateVersion }
  // `id` is the desktop_id (the stable identifier CONTRACT.md mandates for
  // --remove); `path` feeds F4's detached launch.
  items: [],
  lastSyncMs: 0,
  lastError: "",
  status: "",
  busy: { list: false, launch: false, remove: false, integrate: false },
  // argv array (e.g. ["python3", "<pluginDir>/backend/main.py"]); null until
  // configured or derived from the manifest source dir.
  backendCommand: null,
  mockMode: false,
  startupSyncClaimed: false
}

var subscribers = []
var revision = 0

function subscribe(fn) {
  subscribers.push(fn)
  return function() {
    var next = []
    for (var i = 0; i < subscribers.length; i++) {
      if (subscribers[i] !== fn) next.push(subscribers[i])
    }
    subscribers = next
  }
}

function changed() {
  revision++
  for (var i = 0; i < subscribers.length; i++) {
    try { subscribers[i]() } catch (e) { /* one broken listener must not break the rest */ }
  }
}

// ---- configuration ---------------------------------------------------------

function setMockMode(value) {
  state.mockMode = value === true
  changed()
}

function configureBackend(command) {
  state.backendCommand = Array.isArray(command) && command.length > 0
    ? command.slice()
    : state.backendCommand
  changed()
}

// One-shot guard for the bar widget's startup refresh: a bar surface exists
// per monitor, but a single backend probe at login is enough (PRD §2). The
// first caller wins; later callers get false.
function claimStartupSync() {
  if (state.startupSyncClaimed) return false
  state.startupSyncClaimed = true
  return true
}

// ---- derived counts --------------------------------------------------------

function installedCount() {
  return state.items.length
}

function runningCount() {
  var n = 0
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].running) n++
  }
  return n
}

function updateCount() {
  var n = 0
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].updateVersion) n++
  }
  return n
}

function findItem(id) {
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].id === id) return state.items[i]
  }
  return null
}

// ---- backend output normalization ------------------------------------------
//
// Shapes are fixed by backend/CONTRACT.md (schema_version 1):
//   --list-installed --json  → {"schema_version":1, "installed":[<app>,…]}
//   --integrate/--remove     → {"schema_version":1, "result":…, "app":…}
//   error (exit != 0)        → {"schema_version":1, "result":"error",
//                               "error":"<message>"}
// A bare array is also accepted for list responses so old captures keep
// parsing. Unknown keys (available_version, manager, …) are ignored.

function dirname(path) {
  var text = String(path || "")
  var slash = text.lastIndexOf("/")
  return slash > 0 ? text.substring(0, slash) : "."
}

function basenameNoExt(path) {
  var text = String(path || "")
  var slash = text.lastIndexOf("/")
  var base = slash >= 0 ? text.substring(slash + 1) : text
  var dot = base.lastIndexOf(".")
  return dot > 0 ? base.substring(0, dot) : base
}

// The managed icon lives next to the AppImage under .icons/<base>.<ext>
// (CONTRACT.md filesystem layout); the extension is not in the JSON, so we
// keep the extension-less base and let the view try png/svg/xpm in turn.
function iconBaseFor(app) {
  var path = String(app.path || "")
  if (path === "") return ""
  return dirname(path) + "/.icons/" + basenameNoExt(path)
}

function normalizeItem(entry) {
  if (!entry || typeof entry !== "object") return null
  // desktop_id is the stable id (same-named apps coexist per CONTRACT.md);
  // when the backend reports null, the absolute path is the fallback —
  // --remove resolves exact paths before desktop ids anyway.
  var id = entry.desktop_id || entry.desktopId || entry.path || entry.id || ""
  if (String(id) === "") return null
  var item = {
    id: String(id),
    name: String(entry.name || id),
    version: String(entry.current_version || entry.version || ""),
    path: String(entry.path || ""),
    running: entry.running === true,
    iconBase: "",
    updateVersion: String(entry.available_version || entry.update_version || "")
  }
  item.iconBase = iconBaseFor(item)
  return item
}

function parseListJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return parsed
  var list = Array.isArray(parsed.doc) ? parsed.doc : parsed.doc.installed
  if (!Array.isArray(list)) {
    return { ok: false, items: [], error: "Backend response had no installed list" }
  }
  var items = []
  for (var i = 0; i < list.length; i++) {
    var item = normalizeItem(list[i])
    if (item) items.push(item)
  }
  return { ok: true, items: items, error: "" }
}

// Parse an action response (--integrate / --remove): returns
// { ok, result, message, error, app } with result one of "integrated",
// "already-integrated", "removed" or "error" per CONTRACT.md.
function parseActionJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return { ok: false, result: "error", message: "", error: parsed.error, app: null }
  var doc = parsed.doc
  var result = String(doc.result || "")
  if (result === "") {
    return { ok: false, result: "error", message: "", error: "Backend response had no result field", app: null }
  }
  return {
    ok: result !== "error",
    result: result,
    message: String(doc.message || ""),
    error: String(doc.error || ""),
    app: normalizeItem(doc.app)
  }
}

// Tolerant document parse (shared with lib/Backend.js's strategy): strict
// first, then recover the outermost {…}/[…] if a stray line sneaks in.
function parseDocument(text) {
  var raw = String(text || "").trim()
  if (raw === "") return { ok: false, doc: null, error: "Empty backend response" }
  var doc = null
  try { doc = JSON.parse(raw) } catch (e) { doc = null }
  if (doc === null) {
    var start = raw.indexOf("{")
    var startArray = raw.indexOf("[")
    if (startArray !== -1 && (start === -1 || startArray < start)) start = startArray
    var end = raw.lastIndexOf("}")
    var endArray = raw.lastIndexOf("]")
    if (endArray !== -1 && endArray > end) end = endArray
    if (start >= 0 && end > start) {
      try { doc = JSON.parse(raw.substring(start, end + 1)) } catch (e2) { doc = null }
    }
  }
  if (doc === null || typeof doc !== "object") {
    return { ok: false, doc: null, error: "Backend response was not JSON" }
  }
  return { ok: true, doc: doc, error: "" }
}

// ---- mutators (each announces through changed()) ---------------------------

function setItems(items) {
  state.items = Array.isArray(items) ? items : []
  state.lastSyncMs = Date.now()
  changed()
}

function setBusy(key, value) {
  state.busy[key] = value === true
  changed()
}

function isBusy() {
  for (var key in state.busy) {
    if (state.busy[key]) return true
  }
  return false
}

function setError(message) {
  state.lastError = String(message || "")
  if (state.lastError !== "") state.status = ""
  changed()
}

function clearError() {
  if (state.lastError === "") return
  state.lastError = ""
  changed()
}

function setStatus(message) {
  state.status = String(message || "")
  if (state.status !== "") state.lastError = ""
  changed()
}

function clearStatus() {
  if (state.status === "") return
  state.status = ""
  changed()
}

function markRunning(id, running) {
  var item = findItem(id)
  if (!item || item.running === (running === true)) return
  item.running = running === true
  changed()
}

function removeById(id) {
  var next = []
  for (var i = 0; i < state.items.length; i++) {
    if (state.items[i].id !== id) next.push(state.items[i])
  }
  if (next.length === state.items.length) return
  state.items = next
  changed()
}

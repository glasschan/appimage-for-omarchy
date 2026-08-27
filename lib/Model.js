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
  pickerCancel: "Cancel",
  // F6–F9 updates (M2).
  checkUpdates: "Check updates",
  checkingUpdates: "Checking updates…",
  updatesAvailable: "%1 updates available",
  updatesNone: "No updates available",
  updateAvailable: "⬆ %1 available",
  updateAction: "Update",
  updating: "%1 updating…",
  updated: "Updated to %1",
  upToDate: "Up to date",
  skippedRunning: "Skipped — the app is running",
  // Settings card: update-check cadence (mirrors --settings/--set-setting).
  settingsTitle: "Settings",
  settingsUpdateChecks: "Update checks",
  checkIntervalMinutes: "Check interval (minutes)",
  firstCheckDelayMinutes: "First check delay (minutes)",
  enabledLabel: "Enabled",
  disabledLabel: "Disabled",
  notificationsOnNewUpdates: "New updates are announced with a desktop notification.",
  settingsSave: "Save",
  settingsSaved: "Settings saved",
  // Per-app update source (F9, --set-update-source). sourceFieldLabels is
  // keyed by manager config key; manager display names resolve through
  // MANAGERS' labelKey so both stay central for i18n.
  sourceSection: "Update source",
  sourceEmbedded: "%1 (embedded)",
  sourceCustom: "Custom source",
  sourceNone: "No update source",
  sourceManagerLabel: "Manager",
  sourceFieldLabels: {
    url: "URL",
    repo: "Repository",
    repo_url: "Repository URL",
    repo_filename: "Release filename",
    allow_prereleases: "Allow prereleases"
  },
  sourceEditTooltip: "Edit source",
  sourceSave: "Save",
  sourceUnset: "Unset",
  sourceSaved: "Update source saved",
  sourceUnsetDone: "Update source removed",
  managerStatic: "Static URL",
  managerGithub: "GitHub releases",
  managerGitlab: "GitLab releases",
  managerCodeberg: "Codeberg releases",
  managerForgejo: "Forgejo releases"
}

function formatCount(n, template) {
  return String(template).replace("%1", String(n))
}

// ---- state -----------------------------------------------------------------

var state = {
  // Normalized items: { id, name, version, path, running, iconBase,
  //   updateVersion, hasUpdate, manager, embeddedSource, downloadSize }
  // `id` is the desktop_id (the stable identifier CONTRACT.md mandates for
  // --remove); `path` feeds F4's detached launch.
  items: [],
  lastSyncMs: 0,
  lastError: "",
  status: "",
  // busy.update holds the desktop_id of the in-flight --update ("" when
  // idle) rather than a boolean, so list rows can tell which row is busy;
  // it stays truthy/falsy for isBusy() like the rest.
  busy: { list: false, launch: false, remove: false, integrate: false,
          updates: false, update: "" },
  // argv array (e.g. ["python3", "<pluginDir>/backend/main.py"]); null until
  // configured or derived from the manifest source dir.
  backendCommand: null,
  mockMode: false,
  startupSyncClaimed: false,
  // M2 updates: `settings` mirrors the backend's --settings document and
  // `settingsRevision` is the service's reschedule signal (bumped by
  // applySettings — a JS library has no property notifications, so the
  // revision IS the notification). lastUpdatesCheckMs timestamps the last
  // finished sweep; updatesChecking mirrors busy.updates for callers that
  // read the flag by name.
  settings: { enabled: true, intervalMinutes: 360, delayMinutes: 5 },
  settingsRevision: 0,
  lastUpdatesCheckMs: 0,
  updatesChecking: false
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

// Engine-wide single flight for update checks (F6/F8): the panel's
// --list-updates and the service's --fetch-updates both go through here so
// a sweep can never stack on top of another one. Returns false when a
// check is already running; the caller's onDone path must releaseUpdateCheck().
function claimUpdateCheck() {
  if (state.busy.updates || state.updatesChecking) return false
  state.busy.updates = true
  state.updatesChecking = true
  changed()
  return true
}

function releaseUpdateCheck() {
  state.busy.updates = false
  state.updatesChecking = false
  changed()
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
    if (state.items[i].hasUpdate) n++
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
//   --list-updates/--fetch-updates → {"schema_version":1, "updates":[<app>,…]}
//   --settings                     → {"schema_version":1, "settings":{…}}
//   error (exit != 0)        → {"schema_version":1, "result":"error",
//                               "error":"<message>"}
// A bare array is also accepted for list responses so old captures keep
// parsing. Older captures missing the update keys (manager, download_size,
// …) normalize to their empty values.

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
    updateVersion: String(entry.available_version || entry.update_version || ""),
    // hasUpdate marks "the last completed sweep reported this app" and is
    // what updateCount() badges on — a static-source update carries no
    // version string, so the marker alone cannot tint the badge.
    // --list-installed knows nothing about pending updates, so it starts
    // false; applyUpdates owns the flag.
    hasUpdate: false,
    // Update plumbing (M2): manager names the configured custom source
    // ("" when none), embeddedSource marks an AppImage that ships its own
    // updater, downloadSize is bytes (0 = unknown/null).
    manager: String(entry.manager || ""),
    embeddedSource: entry.embedded_source === true,
    downloadSize: typeof entry.download_size === "number" && entry.download_size > 0
      ? entry.download_size : 0
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

// Parse an --list-updates / --fetch-updates response: the same normalized
// items as the installed list. notified/offline only ride on
// --fetch-updates (--list-updates leaves them false); offline:true means
// the sweep never reached the network, so callers must not treat its empty
// list as "everything is up to date".
function parseUpdatesJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) {
    return { ok: false, updates: [], offline: false, notified: false, error: parsed.error }
  }
  var list = Array.isArray(parsed.doc) ? parsed.doc : parsed.doc.updates
  if (!Array.isArray(list)) {
    return { ok: false, updates: [], offline: false, notified: false,
             error: "Backend response had no updates list" }
  }
  var updates = []
  for (var i = 0; i < list.length; i++) {
    var item = normalizeItem(list[i])
    if (item) updates.push(item)
  }
  return {
    ok: true,
    updates: updates,
    offline: parsed.doc.offline === true,
    notified: parsed.doc.notified === true,
    error: ""
  }
}

// Parse a --settings / --set-setting response into the store's shape.
// Tolerant defaults mirror the backend contract (360-minute cadence, first
// check 5 minutes after login, checks on); the 15-minute floor matches
// --set-setting's own minimum so a bad document cannot tighten the loop.
function parseSettingsJson(text) {
  var parsed = parseDocument(text)
  if (!parsed.ok) return { ok: false, settings: null, error: parsed.error }
  var doc = parsed.doc.settings && typeof parsed.doc.settings === "object"
    ? parsed.doc.settings : parsed.doc
  return {
    ok: true,
    settings: {
      enabled: doc.update_check_enabled !== false,
      intervalMinutes: Math.max(15, toInt(doc.update_check_interval_minutes, 360)),
      delayMinutes: Math.max(0, toInt(doc.update_check_delay_minutes, 5))
    },
    error: ""
  }
}

// JSON numbers arrive as ints, floats or strings depending on how careful
// the producer was; null/absent fall back instead of coercing to 0.
function toInt(value, fallback) {
  if (value === null || value === undefined || value === "") return fallback
  var n = Number(value)
  return isFinite(n) ? Math.round(n) : fallback
}

// Parse an action response (--integrate / --remove / --update): returns
// { ok, result, message, error, app } with result one of "integrated",
// "already-integrated", "removed" ("updated", "up-to-date",
// "skipped-running" for --update) or "error" per CONTRACT.md — any
// non-error result passes through as ok.
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

// Merge a finished update sweep into the installed list without a rescan.
// The sweep result is authoritative: every id it reports is marked
// hasUpdate (even static sources whose available_version is null — their
// update is real but versionless), ids the sweep no longer reports fall
// back to no update. Reported apps also refresh their source metadata
// (manager/embeddedSource/downloadSize — sweep data is fresher than the
// list cache); absent apps keep theirs, because a source existing without
// a pending update is legitimate (a source removed externally is reset by
// the panel's --list-installed refresh instead). Only update metadata
// moves — names, paths and running state still belong to --list-installed.
function applyUpdates(updates) {
  if (!Array.isArray(updates)) updates = []
  var byId = {}
  for (var u = 0; u < updates.length; u++) byId[updates[u].id] = updates[u]
  var touched = false
  for (var i = 0; i < state.items.length; i++) {
    var item = state.items[i]
    var upd = byId[item.id]
    var hasUpdate = !!upd
    var version = upd ? upd.updateVersion : ""
    if (item.hasUpdate !== hasUpdate) {
      item.hasUpdate = hasUpdate
      touched = true
    }
    if (item.updateVersion !== version) {
      item.updateVersion = version
      touched = true
    }
    if (upd) {
      if (item.manager !== upd.manager) {
        item.manager = upd.manager
        touched = true
      }
      if (item.embeddedSource !== upd.embeddedSource) {
        item.embeddedSource = upd.embeddedSource
        touched = true
      }
      if (item.downloadSize !== upd.downloadSize) {
        item.downloadSize = upd.downloadSize
        touched = true
      }
    }
  }
  state.lastUpdatesCheckMs = Date.now()
  if (touched) changed()
}

// Store parsed settings and bump settingsRevision so the service's store
// subscription reschedules its check timer.
function applySettings(settings) {
  if (!settings || typeof settings !== "object") return
  state.settings.enabled = settings.enabled !== false
  state.settings.intervalMinutes = Math.max(15, toInt(settings.intervalMinutes, 360))
  state.settings.delayMinutes = Math.max(0, toInt(settings.delayMinutes, 5))
  state.settingsRevision++
  changed()
}

// Single in-flight --update, keyed by desktop_id ("" when idle): list rows
// compare it to dim every Update button and spin the updating one.
function setUpdatingId(id) {
  var next = String(id || "")
  if (state.busy.update === next) return
  state.busy.update = next
  changed()
}

// ---- update-source managers (settings UI metadata) --------------------------
//
// Mirror of the managers --set-update-source accepts (CONTRACT.md): labelKey
// resolves through `strings` so display names stay central for i18n, and
// `fields` drives the per-app source editor — type "string" renders a text
// field, type "bool" a switch (serialized as true/false). Keep in sync with
// the backend's manager registry.

var MANAGERS = [
  { name: "StaticFileUpdater", labelKey: "managerStatic",
    fields: [{ key: "url", type: "string" }] },
  { name: "GithubUpdater", labelKey: "managerGithub",
    fields: [{ key: "repo", type: "string" },
             { key: "repo_filename", type: "string" },
             { key: "allow_prereleases", type: "bool" }] },
  { name: "GitlabUpdater", labelKey: "managerGitlab",
    fields: [{ key: "repo_url", type: "string" },
             { key: "repo_filename", type: "string" }] },
  { name: "CodebergUpdater", labelKey: "managerCodeberg",
    fields: [{ key: "repo", type: "string" },
             { key: "repo_filename", type: "string" },
             { key: "allow_prereleases", type: "bool" }] },
  { name: "ForgejoUpdater", labelKey: "managerForgejo",
    fields: [{ key: "repo_url", type: "string" },
             { key: "repo_filename", type: "string" },
             { key: "allow_prereleases", type: "bool" }] }
]

function managersByName() {
  var map = {}
  for (var i = 0; i < MANAGERS.length; i++) map[MANAGERS[i].name] = MANAGERS[i]
  return map
}

// Label for a manager config key in the source editor; unknown keys fall
// back to the raw key so a newer backend never blanks the form.
function fieldLabel(key) {
  return strings.sourceFieldLabels[key] || String(key)
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

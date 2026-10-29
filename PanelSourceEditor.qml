import QtQuick
import "lib/Backend.js" as Backend

// Per-app update-source editor controller for Panel.qml (the F9 inline
// editor under each row). Non-visual wiring — the editor's state
// (sourceEditId/Name/Manager/Values, sourceBusy, textFieldFocusCount)
// stays on the panel; this controller owns the open/close/rebuild/save
// flow. All calls ride the panel's shared callBackend pair.
Item {
  id: sourceEditor

  // The owning panel (untyped on purpose: no import cycle).
  property var panel

  // Seed the manager dropdown (empty = no source configured — the user
  // must pick before Save) and blank field values, then pre-fill the
  // stored source asynchronously via --get-update-source (--list-installed
  // carries the manager name only, never its config). Clicking the same
  // row's gear again toggles the editor closed — the unsaved working
  // values die with it (closeSourceEditor drops them); a different row's
  // gear keeps the switch-over behaviour below.
  function openSourceEditor(item) {
    if (!item) return
    if (panel.sourceEditId === item.id) {
      closeSourceEditor()
      return
    }
    // Name/manager first: the per-row editor's Dropdown `value` binding
    // derives from panel.sourceEditManager, and its visible binding from
    // sourceEditId — so managers and values must be live before the row's
    // editor becomes visible, and sourceEditId lands last.
    panel.sourceEditName = item.name
    panel.sourceEditManager = item.manager
    rebuildSourceEditValues()
    panel.sourceEditId = item.id
    // Switched rows empty the previous row's Repeater model, which
    // Loader-destroys its fields (see closeSourceEditor for why the count
    // must be zeroed by hand).
    panel.textFieldFocusCount = 0
    // Coarse reveal: the editor may sit below the fold of a long list.
    Qt.callLater(function() {
      if (panel.sourceEditId !== "" && panel.listFlickRef) {
        panel.listFlickRef.contentY = Math.max(0,
          panel.listFlickRef.contentHeight - panel.listFlickRef.height)
      }
    })
    // Async pre-fill: the editor rendered blank instantly; fill it from the
    // stored config once the backend answers. Bail out when the user closed
    // the editor or switched rows while the call was in flight (a stale
    // answer must never touch the current editor's state) or when the call
    // failed; "no-source" and unparseable answers keep the blanks.
    panel.callBackend(["--get-update-source", item.id, "--json"], null,
        function(result) {
      if (panel.sourceEditId !== item.id || !result.ok) return
      var doc = Backend.parseJson(result.stdout)
      if (!doc || doc.result !== "ok") return
      // Same ordering as the instant path above: manager live before the
      // values rebuild so the dropdown never binds a half-set state.
      if (doc.manager) panel.sourceEditManager = doc.manager
      rebuildSourceEditValues()
      var config = doc.config || {}
      for (var key in config) {
        // Plain key writes only — the map object is never swapped under a
        // live field binding (see setSourceValue).
        panel.sourceEditValues[key] = String(config[key])
      }
      // Key writes carry no change signal by design, so the fields would
      // keep showing the blanks the rebuild just published; republish the
      // filled map as one replacement to make every binding re-read.
      var filled = {}
      for (var k in panel.sourceEditValues) filled[k] = panel.sourceEditValues[k]
      panel.sourceEditValues = filled
    })
  }

  function closeSourceEditor() {
    panel.sourceEditId = ""
    panel.sourceEditName = ""
    panel.sourceEditManager = ""
    panel.sourceEditValues = {}
    // Loader-destroyed fields may never deliver their final
    // activeFocusChanged(false); zero the count so the key catcher cannot
    // stay blocked forever.
    panel.textFieldFocusCount = 0
  }

  function rebuildSourceEditValues() {
    var mgr = panel.modelApi.managersByName()[panel.sourceEditManager]
    var values = {}
    if (mgr) {
      for (var i = 0; i < mgr.fields.length; i++) {
        values[mgr.fields[i].key] = mgr.fields[i].type === "bool" ? "false" : ""
      }
    }
    // Reset before the model swap re-creates the Loader-held fields, for
    // the same skipped-leave reason as closeSourceEditor above.
    panel.textFieldFocusCount = 0
    panel.sourceEditValues = values
  }

  function setSourceValue(key, value) {
    // Storage only: mutate without reassigning so a field's text binding
    // never re-evaluates mid-typing. rebuildSourceEditValues and the async
    // pre-fill in openSourceEditor are the only writers that replace the
    // object (both outside a typing session).
    panel.sourceEditValues[key] = value
  }

  // Persist the edited source. argv keeps every token a single literal —
  // ids, keys and values never meet a shell. Empty string fields are
  // skipped; the backend validates what its manager needs and answers
  // exit 2 with a clean {"result":"error"} document (--json) otherwise.
  function saveSource() {
    if (panel.sourceBusy || panel.sourceEditId === "" || panel.sourceEditManager === "") return
    var args = ["--set-update-source", panel.sourceEditId, "--manager",
                panel.sourceEditManager, "--json"]
    var mgr = panel.modelApi.managersByName()[panel.sourceEditManager]
    if (mgr) {
      for (var i = 0; i < mgr.fields.length; i++) {
        var field = mgr.fields[i]
        var value = panel.sourceEditValues[field.key]
        if (value === undefined) continue
        var text = field.type === "bool"
          ? (value === "true" ? "true" : "false")
          : String(value).trim()
        if (text === "") continue
        args.push(field.key + "=" + text)
      }
    }
    panel.sourceBusy = true
    panel.modelApi.clearError()
    panel.syncFromModel()
    panel.callBackend(args, null, function(result) {
      panel.sourceBusy = false
      if (result.ok) {
        panel.modelApi.setStatus(panel.strings.sourceSaved)
        panel.modelApi.clearError()
        closeSourceEditor()
      } else {
        panel.reportFailure(result, "set-update-source")
      }
      panel.syncFromModel()
      // Manager names ride on --list-installed; refresh the rows.
      panel.refresh()
    })
  }

  function unsetSource() {
    if (panel.sourceBusy || panel.sourceEditId === "") return
    panel.sourceBusy = true
    panel.modelApi.clearError()
    panel.syncFromModel()
    panel.callBackend(["--set-update-source", panel.sourceEditId, "--unset", "--json"], null,
        function(result) {
      panel.sourceBusy = false
      if (result.ok) {
        panel.modelApi.setStatus(panel.strings.sourceUnsetDone)
        panel.modelApi.clearError()
        closeSourceEditor()
      } else {
        panel.reportFailure(result, "set-update-source")
      }
      panel.syncFromModel()
      panel.refresh()
    })
  }
}

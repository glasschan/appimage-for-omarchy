import QtQuick

// Integrate controller for Panel.qml: the picker/preview/drop cluster.
// Non-visual wiring: the picker card itself stays in the panel's visual tree
// and every flag it renders stays on the panel. Browse rides the panel's
// shared callBackend pair.
Item {
  id: integrateCtl

  // The owning panel (untyped on purpose: no import cycle).
  property var panel

  function openPicker() {
    // The settings gear and Integrate close each other: two inline cards
    // would fight for width and keyboard focus.
    panel.settingsOpen = false
    panel.closeSourceEditor()
    // A preview computed for a previous session is stale.
    clearPreview()
    panel.pickerOpen = true
  }

  function closePicker() {
    // A browse in flight owns the card: its answer lands in pickerPath, so
    // the card must not be dismissed under the pending dialog.
    if (panel.modelApi.state.busy.pick) return
    panel.pickerOpen = false
    clearPreview()
  }

  // Preview lifecycle. clearPreview resets all card state and never calls the
  // backend or touches the store: dismissing has zero side effects.
  function clearPreview() {
    panel.previewOpen = false
    panel.busyPreview = false
    panel.previewDoc = null
    panel.previewInput = ""
  }

  // Step 1 of the flow: ask the backend what --integrate WOULD do. The preview
  // verb is offline and advisory; failure lands in the error banner and the
  // picker stays usable.
  function requestPreview(path) {
    var trimmed = String(path || "").trim()
    if (trimmed === "") return
    if (panel.modelApi.state.busy.integrate || panel.busyPreview) return
    if (panel.backendCommand.length === 0) {
      panel.modelApi.setError(panel.strings.backendMissing)
      panel.syncFromModel()
      return
    }
    // Same input, card already showing it: nothing to refresh.
    if (panel.previewOpen && panel.previewInput === trimmed) return
    panel.modelApi.clearError()
    panel.busyPreview = true
    panel.syncFromModel()
    panel.callBackend(["--preview-integrate", trimmed, "--json"],
        { timeoutMs: panel.previewTimeoutMs }, function(result) {
      panel.busyPreview = false
      // The path moved on while the call ran: the answer is stale, drop it
      // silently. Otherwise the card could preview one path while Confirm
      // installs another.
      if (String(panel.pickerPath).trim() !== trimmed) {
        panel.syncFromModel()
        return
      }
      if (result.ok) {
        var parsed = panel.modelApi.parsePreviewJson(result.stdout)
        if (parsed.ok) {
          panel.previewDoc = parsed.preview
          panel.previewInput = trimmed
          panel.previewOpen = true
          panel.modelApi.clearError()
        } else {
          clearPreview()
          panel.modelApi.setError(parsed.error)
        }
      } else {
        clearPreview()
        panel.reportFailure(result, "preview")
      }
      panel.syncFromModel()
      // The confirm step owns the keyboard (Enter confirms, Escape
      // dismisses): pull focus back to the panel-level key catcher.
      Qt.callLater(function() {
        if (panel.opened && panel.keyCatcherRef) panel.keyCatcherRef.forceActiveFocus()
      })
    })
  }

  // Step 2: the explicit confirmation. integratePath re-classifies and
  // re-verifies from scratch since the preview document was advisory. The card
  // is dropped first so a failure leaves the plain picker with the banner.
  function confirmPreview() {
    if (!panel.previewOpen || panel.busyPreview) return
    // The pick-busy invariant applies to the keyboard's confirm path too.
    if (panel.modelApi.state.busy.integrate || panel.modelApi.state.busy.pick) return
    clearPreview()
    panel.integratePath(panel.pickerPath)
  }

  // Esc / Cancel: back to the picker, nothing was run, nothing changed.
  function dismissPreview() {
    if (!panel.previewOpen) return
    clearPreview()
    // Hand focus back to the path field so the user can edit what they
    // typed (formEditing re-blocks the key catcher while it holds focus).
    Qt.callLater(function() {
      if (panel.pickerOpen) panel.pickerCardRef.focusField()
    })
  }

  // Preview card display lines. An empty appName means the backend could not
  // know yet, so the copy says so instead of showing blank.
  function previewAppLine() {
    if (!panel.previewDoc) return ""
    if (panel.previewDoc.appName !== "") return panel.previewDoc.appName
    return panel.previewDoc.sourceKind === "local"
      ? panel.strings.previewAppUnknown : panel.strings.previewAppUnknownUrl
  }

  function previewDestinationLine() {
    if (!panel.previewDoc) return ""
    if (panel.previewDoc.destinationPath !== "") return panel.previewDoc.destinationPath
    return panel.previewDoc.destinationFolder !== ""
      ? panel.previewDoc.destinationFolder + " — " + panel.strings.previewDestinationLater
      : panel.strings.previewDestinationLater
  }

  // Browse: run --pick-file through the shared watchdog plumbing. The backend
  // runs a crash-isolated native dialog (portal first, zenity fallback) under
  // its own 600 s deadline. A busy browse is a no-op, so the dialog can never
  // stack.
  function browseForFile() {
    if (panel.modelApi.state.busy.pick) return
    if (panel.modelApi.state.busy.integrate) return
    if (panel.backendCommand.length === 0) {
      panel.modelApi.setError(panel.strings.backendMissing)
      panel.syncFromModel()
      return
    }
    panel.modelApi.clearError()
    panel.modelApi.setBusy("pick", true)
    panel.modelApi.setStatus(panel.strings.pickerBrowsing)
    panel.syncFromModel()
    panel.callBackend(["--pick-file", "--json"], { timeoutMs: panel.pickTimeoutMs },
        function(result) {
      panel.modelApi.setBusy("pick", false)
      if (result.ok) {
        var pick = panel.modelApi.parsePickJson(result.stdout)
        if (pick.result === "picked" && pick.path !== "") {
          panel.modelApi.clearStatus()
          panel.pickerPath = pick.path
          // A preview computed for the previous path is stale the moment the
          // fill lands: drop it so Confirm can never install anything other
          // than what is on screen. The picked path then rides the same
          // preview flow as a drop or a Downloads quick-pick.
          if (panel.previewOpen && pick.path.trim() !== panel.previewInput) {
            dismissPreview()
          }
          panel.syncFromModel()
          focusPickerField()
          requestPreview(pick.path)
          return
        }
        if (pick.result === "cancelled") {
          panel.modelApi.setStatus(panel.strings.pickCanceled)
        } else if (pick.result === "unavailable") {
          // The backend's own copy wins; the local string is the fallback.
          // Status, not error: the path field keeps working as before.
          panel.modelApi.setStatus(pick.message !== "" ? pick.message
            : panel.strings.pickerUnavailable)
        } else {
          panel.modelApi.setError(pick.result === "error" && pick.error !== ""
            ? pick.error : panel.strings.pickerPickFailed)
        }
      } else {
        panel.reportFailure(result, "pick-file")
      }
      panel.syncFromModel()
    })
  }

  // Put focus into the picker's path field so a picked path can be integrated
  // with Enter right away.
  function focusPickerField() {
    panel.pickerCardRef.focusField()
  }

  // ---- drop-to-integrate ------------------------------------------------------
  //
  // Two entries, one flow. The panel window's DropArea (live drop) and the bar
  // widget's summon-payload handoff both end in seedDropPreview: seed the
  // field, request the offline preview. A drop never integrates directly.

  // True while the shared backend process or a picker-owning flow is busy. A
  // drop racing any of them is IGNORED with a status line, never queued: a
  // field fill mid-call could only produce an answer the stale-drop logic
  // would throw away.
  function dropBusy() {
    return panel.modelApi.isBusy() || panel.busyPreview || panel.settingsBusy || panel.sourceBusy
  }

  // The shared handoff. openPicker() closes the settings/source cards and
  // drops a stale preview first, so a drop works from any panel state.
  function seedDropPreview(path, ignored) {
    openPicker()
    panel.pickerPath = path
    if (ignored > 0) {
      panel.modelApi.setStatus(ignored === 1 ? panel.strings.dropIgnoredOne
        : panel.modelApi.formatCount(ignored, panel.strings.dropIgnoredMany))
    }
    requestPreview(path)
  }

  // Panel DropArea entry: convert the dropped URI list, report rejections as a
  // status with the picker untouched, refuse busy races.
  function handleDropUris(uris) {
    var conv = panel.modelApi.dropUriToPath(uris)
    if (!conv.ok) {
      // reason "empty" means nothing usable was dropped at all — silent.
      if (conv.reason === "not-file") panel.modelApi.setStatus(panel.strings.dropNotFile)
      else if (conv.reason === "no-appimage") panel.modelApi.setStatus(panel.strings.dropNoAppImage)
      panel.syncFromModel()
      return
    }
    if (dropBusy()) {
      panel.modelApi.setStatus(panel.strings.dropBusy)
      panel.syncFromModel()
      return
    }
    seedDropPreview(conv.path, conv.ignored)
  }

  // Summon-payload parser (bar-widget handoff). Only BarWidget's own drop
  // handler writes this shape ({drop_path, drop_ignored}); every other summon
  // payload leaves the pending pair empty. The path is picker-field data, never
  // a shell argument.
  function readDropPayload(payloadJson) {
    panel.pendingDropPath = ""
    panel.pendingDropIgnored = 0
    if (!payloadJson || payloadJson === "{}") return
    var payload = null
    try { payload = JSON.parse(String(payloadJson)) } catch (e) { payload = null }
    if (!payload || typeof payload !== "object") return
    var dropped = String(payload.drop_path || "").trim()
    if (dropped === "") return
    panel.pendingDropPath = dropped
    var extras = payload.drop_ignored
    panel.pendingDropIgnored = typeof extras === "number" && extras > 0
      ? Math.round(extras) : 0
  }

  // Consume the parked bar-widget drop. Runs on every store sync (and once more
  // from open() via callLater as the idle-store backstop): while any backend
  // call is in flight the drop stays pending and the sync after the last busy
  // flag clears runs it. Every busy flag is watchdog-released, and closing the
  // panel cancels the drop outright. The gesture happened before the panel
  // existed, so ignoring it would kill the only reason the panel opened.
  function tryConsumePendingDrop() {
    if (panel.pendingDropPath === "" || !panel.opened) return
    if (dropBusy()) return
    var dropped = panel.pendingDropPath
    var ignored = panel.pendingDropIgnored
    panel.pendingDropPath = ""
    panel.pendingDropIgnored = 0
    seedDropPreview(dropped, ignored)
  }
}

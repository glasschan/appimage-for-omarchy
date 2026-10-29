import QtQuick

// Integrate controller for Panel.qml: the picker/preview/drop cluster.
// Non-visual wiring — the picker card itself stays in the panel's visual
// tree and every flag it renders stays on the panel (pickerOpen,
// pickerPath, previewOpen, previewDoc, busy*). Browse rides the panel's
// shared callBackend pair; nothing spawns a Process of its own.
Item {
  id: integrateCtl

  // The owning panel (untyped on purpose: no import cycle).
  property var panel

  function openPicker() {
    // The two inline cards would fight for width and keyboard focus; the
    // settings gear and Integrate close each other.
    panel.settingsOpen = false
    panel.closeSourceEditor()
    // A preview computed for a previous session is stale: re-entering the
    // picker starts at the plain path field.
    clearPreview()
    panel.pickerOpen = true
  }

  function closePicker() {
    // A browse in flight owns the card: its answer lands in pickerPath, so
    // the card must not be dismissed out from under the pending dialog.
    // Idempotent-open paths (openPicker, settings toggling back) are
    // unaffected; the dialog itself is bounded by the pick watchdog.
    if (panel.modelApi.state.busy.pick) return
    panel.pickerOpen = false
    clearPreview()
  }

  // Preview lifecycle. clearPreview resets all card state; it never calls
  // the backend or touches the store — dismissing has zero side effects,
  // by contract.
  function clearPreview() {
    panel.previewOpen = false
    panel.busyPreview = false
    panel.previewDoc = null
    panel.previewInput = ""
  }

  // Step 1 of the flow: ask the backend what --integrate WOULD do. The
  // preview verb is offline (URL classification by shape, local entries
  // read in place) and advisory — nothing here integrates anything. Failure
  // lands in the error banner and the picker stays usable.
  function requestPreview(path) {
    var trimmed = String(path || "").trim()
    if (trimmed === "") return
    if (panel.modelApi.state.busy.integrate || panel.busyPreview) return
    if (panel.backendCommand.length === 0) {
      panel.modelApi.setError(panel.strings.backendMissing)
      panel.syncFromModel()
      return
    }
    // Same input, card already showing it: nothing to refresh (this is
    // also what a second Enter on the focused field lands on).
    if (panel.previewOpen && panel.previewInput === trimmed) return
    panel.modelApi.clearError()
    panel.busyPreview = true
    panel.syncFromModel()
    panel.callBackend(["--preview-integrate", trimmed, "--json"],
        { timeoutMs: panel.previewTimeoutMs }, function(result) {
      panel.busyPreview = false
      // The path moved on while the call ran (kept typing, quick-pick,
      // picker closed): the answer is stale — drop it silently. Without
      // this, the card could preview one path while Confirm installs
      // another. The field's next Enter re-previews the new text.
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
      // dismisses): pull focus back to the panel-level key catcher so the
      // SafeTextField stops blocking it.
      Qt.callLater(function() {
        if (panel.opened && panel.keyCatcherRef) panel.keyCatcherRef.forceActiveFocus()
      })
    })
  }

  // Step 2: the explicit confirmation. Runs the existing integrate command
  // unchanged (integratePath re-classifies and re-verifies from scratch —
  // the preview document was advisory). The card is dropped first so a
  // failure leaves the plain picker with the banner, not a stale preview.
  function confirmPreview() {
    if (!panel.previewOpen || panel.busyPreview) return
    // A browse in flight owns the backend the integrate command would
    // share (the pick-busy invariant, applied to the keyboard's confirm
    // path too — the Go button is already disabled during a pick).
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

  // Preview card display lines. An empty appName means the backend could
  // not know yet (URL sources download nothing; a local file without an
  // embedded desktop entry stays unreadable in place) — the copy says so
  // instead of showing blank, per CONTRACT.md's "the preview stays valid".
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

  // Browse: run --pick-file through the shared watchdog plumbing. The
  // backend orchestrates a crash-isolated native dialog (portal first,
  // zenity fallback) under its own 600 s whole-operation deadline; result
  // mapping per CONTRACT.md — picked (exit 0 + path) fills the field,
  // cancelled/unavailable are exit-0 statuses, typed errors (reason key)
  // and a watchdog overrun surface through reportFailure like every other
  // backend failure. A busy browse is a no-op (and the Go/Downloads paths
  // are disabled meanwhile), so the dialog can never stack.
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
          // A preview computed for the previous path is stale the moment
          // the fill lands (confirm-before-integrate, browse flavor):
          // drop it so Confirm can never install anything other than what
          // is on screen. The picked path then rides the same preview flow
          // as a drop or a Downloads quick-pick — seedDropPreview's
          // mechanics minus openPicker (the card is already open): fill
          // the field, request the offline preview, Confirm stays the only
          // install path. Same-trimmed fills keep the shown card via
          // requestPreview's own guard, matching the field's onEdited rule.
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
          // Status, not error: the path field keeps working exactly as
          // before (this is today's UX, not a failure).
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

  // Put focus into the picker's path field so a picked path can be
  // integrated with Enter right away. Defensive: a browse that outlived
  // the card is a quiet store update.
  function focusPickerField() {
    panel.pickerCardRef.focusField()
  }

  // ---- drop-to-integrate ------------------------------------------------------
  //
  // Two entries, one flow. The panel window's DropArea (live drop) and the
  // bar widget's summon-payload handoff both end in seedDropPreview, which
  // is exactly the Downloads quick-pick mechanics plus openPicker(): seed
  // the field, request the offline preview — a drop NEVER integrates
  // directly, Confirm stays the only install path.

  // True while the shared backend process or a picker-owning flow is busy —
  // the same states during which Go/Browse/Downloads are disabled. A drop
  // racing any of them is IGNORED with a status line, never queued: a
  // field fill mid-call could only produce an answer the stale-drop logic
  // would have to throw away (the same reasoning that disables Browse
  // during a preview), and every flag here is watchdog-bounded, so
  // "drop again" is always short-lived advice.
  function dropBusy() {
    return panel.modelApi.isBusy() || panel.busyPreview || panel.settingsBusy || panel.sourceBusy
  }

  // The shared handoff. openPicker() closes the settings/source cards and
  // drops a stale preview first, so a drop works from any panel state and
  // the card that appears is always computed from the dropped file. An
  // ignored-extras report is a status (accent banner, 5 s auto-clear) set
  // before the preview request so both notices coexist.
  function seedDropPreview(path, ignored) {
    openPicker()
    panel.pickerPath = path
    if (ignored > 0) {
      panel.modelApi.setStatus(ignored === 1 ? panel.strings.dropIgnoredOne
        : panel.modelApi.formatCount(ignored, panel.strings.dropIgnoredMany))
    }
    requestPreview(path)
  }

  // Panel DropArea entry: convert the dropped URI list (typed result —
  // see panel.modelApi.dropUriToPath), report rejections as a status with the
  // picker untouched, refuse busy races, otherwise run the flow.
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
  // handler writes this shape ({drop_path, drop_ignored}); every other
  // summon payload ("{}", garbage) leaves the pending pair empty. The path
  // is picker-field DATA exactly like a typed path — it reaches nothing
  // but the preview flow, never a shell.
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

  // Consume the parked bar-widget drop. Runs on every store sync (and once
  // more from open() via callLater as the idle-store backstop): while any
  // backend call is in flight the drop stays pending — a preview request
  // started now would only die on the shared process's single-flight
  // guard — and the sync after the last busy flag clears runs it. The
  // wait is bounded (every busy flag is watchdog-released) and closing
  // the panel cancels the drop outright (onOpenedChanged). Deferral here
  // is transport, not queueing: the gesture happened before the panel
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

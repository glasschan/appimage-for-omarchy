import QtQuick
import QtQuick.Controls
import Quickshell.Io
import qs.Commons

// Clipboard-safe single-line text field for plugin forms (qs.Ui.TextField
// drop-in with a value/edited pair instead of a text/onTextChanged pair).
//
// Review #11415 round 3: Qt 6.12 automatically retrieves foreign clipboard
// text on every clipboard change for any QQuickTextInput with readOnly:
// false — no paste action, no aggregate byte limit, no overall deadline —
// so a hostile clipboard provider can stall or exhaust the shared Omarchy
// shell process while any editable input is alive, even inside an open
// card. `readOnly` is the only switch for that hook (omarchy#15083), so
// this field is PERMANENTLY readOnly and layers its own editing on top:
//
//   - typing/Backspace/Delete mutate `text` programmatically (allowed on
//     a readOnly TextInput) and report through `edited()`;
//   - paste runs a BOUNDED, fail-closed wl-paste: text/plain MIME only,
//     1 s timeout, streamed byte cap (pasteMaxChars) that kills the
//     helper mid-stream; a non-zero exit or an oversize payload inserts
//     nothing at all;
//   - navigation/selection/copy (arrows, Home/End, Ctrl+A, Ctrl+C) keep
//     the native readOnly behavior; cut and middle-click paste are
//     no-ops on a readOnly input.
//
// Accepted tradeoffs (recorded): no IME preedit/dead keys (readOnly
// disables the input-method path), no native cut, no multi-line paste
// (embedded newlines paste as spaces).
//
// Consumers bind `value` — never written by the field — and write their
// model from `edited(value)`; the value → text sync then re-asserts the
// display on external model changes. This mirrors the kit NumberField's
// value/modified pair and keeps declarative `value:` bindings alive
// (programmatic edits only ever touch the internal `text`, which nothing
// outside binds).
TextField {
  id: root

  // The QTBUG-149610 hook arms on readOnly: false only. Never bind, never
  // clear: this is the whole reason the type exists.
  readOnly: true

  // Model-facing value. Consumer-owned: bind it; the field only reads it.
  property string value: ""

  // Fired after every key-driven text change, with the new value.
  signal edited(string value)

  // Paste guard rails: hard byte cap, one paste in flight at a time.
  property int pasteMaxChars: 4096
  readonly property bool pasteInFlight: pasteProc.running

  Keys.priority: Keys.BeforeItem
  Keys.onPressed: function(event) {
    if (!root.enabled) return
    var mods = event.modifiers
    if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
      root.accepted()
      event.accepted = true
      return
    }
    if (event.key === Qt.Key_Paste
        || ((mods & Qt.ControlModifier) && event.key === Qt.Key_V)
        || ((mods & Qt.ShiftModifier) && event.key === Qt.Key_Insert)) {
      _pasteFromClipboard()
      event.accepted = true
      return
    }
    // Focus navigation must never leak into the field as a tab character.
    if (event.key === Qt.Key_Tab || event.key === Qt.Key_Backtab) return
    // Other modifier combos keep the native readOnly behavior (arrows,
    // Home/End, Ctrl+A select-all, Ctrl+C copy); cut and native paste are
    // already no-ops under readOnly.
    if (mods & (Qt.ControlModifier | Qt.MetaModifier | Qt.AltModifier)) return
    if (event.key === Qt.Key_Backspace) { _backspace(); event.accepted = true; return }
    if (event.key === Qt.Key_Delete) { _deleteForward(); event.accepted = true; return }
    if (event.text.length === 1) { _replaceSelection(event.text); event.accepted = true; return }
  }

  onValueChanged: {
    // External model change re-asserts the display; the cursor rides to
    // the end because this is a programmatic sync, not typing.
    if (text !== value) {
      text = value
      cursorPosition = text.length
    }
  }
  Component.onCompleted: {
    text = value
    cursorPosition = text.length
  }

  // ---- key-driven editing (readOnly-safe programmatic writes) -------------

  function _selectionSpan() {
    if (selectionStart < 0 || selectionEnd < 0) {
      var at = Math.max(0, Math.min(cursorPosition, text.length))
      return [at, at]
    }
    return [Math.min(selectionStart, selectionEnd), Math.max(selectionStart, selectionEnd)]
  }

  function _replaceSelection(insert) {
    var span = _selectionSpan()
    var nt = text.substring(0, span[0]) + insert + text.substring(span[1])
    _applyEdit(nt, span[0] + insert.length)
  }

  function _backspace() {
    var span = _selectionSpan()
    if (span[0] !== span[1]) { _replaceSelection(""); return }
    if (span[0] === 0) return
    _applyEdit(text.substring(0, span[0] - 1) + text.substring(span[1]), span[0] - 1)
  }

  function _deleteForward() {
    var span = _selectionSpan()
    if (span[0] !== span[1]) { _replaceSelection(""); return }
    if (span[1] >= text.length) return
    _applyEdit(text.substring(0, span[0]) + text.substring(span[1] + 1), span[0])
  }

  function _applyEdit(nt, nc) {
    text = nt
    cursorPosition = Math.max(0, Math.min(nc, nt.length))
    deselect()
    edited(text)
  }

  // ---- bounded, fail-closed clipboard paste -------------------------------

  property int _pasteBytes: 0

  function _pasteFromClipboard() {
    if (pasteProc.running) return
    _pasteBytes = 0
    // Absolute paths only (house rule). /usr/bin/timeout bounds the helper
    // to 1 s; --type text/plain fails closed on non-text clipboards
    // (MIME prefix match, no fallback type), --no-newline avoids the
    // trailing-newline rewrite of plain `wl-paste`.
    pasteProc.command = ["/usr/bin/timeout", "1",
                         "/usr/bin/wl-paste", "--no-newline", "--type", "text/plain"]
    pasteProc.running = true
  }

  Process {
    id: pasteProc

    stdout: StdioCollector {
      id: pasteStdout
      waitForEnd: true
      onRead: function(data) {
        // Streamed fail-close: kill the helper the moment the payload
        // exceeds the cap instead of buffering an unbounded clipboard.
        root._pasteBytes += data.length
        if (root._pasteBytes > root.pasteMaxChars) pasteProc.running = false
      }
    }

    onExited: function(exitCode) {
      // Fail closed on every abnormal path: timeout kill, missing helper,
      // non-text clipboard (wl-paste exit != 0), or a stream we killed at
      // the cap. A clean empty payload inserts nothing.
      if (exitCode !== 0) return
      if (root._pasteBytes > root.pasteMaxChars) return
      var out = pasteStdout.text
      if (out === "" || out.length > root.pasteMaxChars) return
      // Single-line field: embedded newlines become spaces.
      root._replaceSelection(out.replace(/[\r\n]+/g, " "))
    }
  }
}

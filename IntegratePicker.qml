import QtQuick
import Qt.labs.folderlistmodel
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model

// Integrate picker (F2 fallback: no Quickshell file dialog) — extracted
// from Panel.qml (ticket #55). Pure view: the panel owns the picker state
// (open/path/busy flags), the preview flow and every backend call; this
// card renders it and reports the user's intent through signals. The
// Downloads quick-pick sources and their folder models live here (they
// feed nothing but the picker's own list).
//
// The URL field is Loader-gated on the card's visibility and is a
// SafeTextField: no native editable input may exist even while the card
// is open (review #11415 round 3) — the field type carries the guarantee;
// the Loader keeps dead instances from piling up. `value` mirrors the
// panel's pickerPath; Enter previews the current path (#18
// confirm-before-integrate).
BorderSurface {
  id: pickerCard

  // Rendered state (owned by Panel.qml).
  property bool pickerOpen: false
  property string pickerPath: ""
  property bool busyPick: false
  property bool busyIntegrate: false
  property bool busyPreview: false
  property bool previewOpen: false
  property var previewDoc: null
  // Combined picker-owning busy state (the panel's pickerBusy): Browse,
  // Go and the Downloads quick-picks all disable on exactly this.
  property bool pickerBusy: false
  // Injected HOME (the panel's homeDir) — the quick-pick sources hang off
  // it.
  property string homeDir: ""
  // The preview card's derived lines (the panel's
  // previewAppLine()/previewDestinationLine()).
  property string previewAppLine: ""
  property string previewDestinationLine: ""
  property var strings
  property color foreground
  property color dim
  // Declared for the embedded PreviewCard (it binds `urgent:
  // pickerCard.urgent`); the panel passes root.urgent. Missing after the
  // component extraction, which left the preview card's urgent colour
  // undefined at runtime.
  property color urgent
  property string fontFamily
  property real controlHeight

  // The path field's text was edited (the panel stores it and drops a
  // stale preview — same-trimmed edits keep the shown card).
  signal pathEdited(string value)
  // Enter on the path field: preview the current path.
  signal previewRequested()
  // Browse button: the backend's crash-isolated --pick-file dialog.
  signal browseClicked()
  // Go button: preview a fresh path, or confirm the shown preview.
  signal goClicked()
  // A Downloads quick-pick was pressed: seed the field with its path and
  // preview (never integrate directly).
  signal quickPicked(string path)
  // Confirm/Esc/Cancel on the embedded preview card.
  signal previewConfirmed()
  signal previewDismissed()
  // Focus deltas from the Loader-created field (the key catcher's focus
  // count — the panel accumulates across every editable field).
  signal fieldFocusChanged(int delta)

  // Whether the Loader-created field holds focus (Panel's formEditing
  // reads this instead of the loader item it used before the extraction).
  readonly property bool fieldFocused:
    fieldLoader.item ? fieldLoader.item.activeFocus : false

  // Put focus into the path field (a picked path should be confirmable
  // with Enter right away). Defensive: a browse that outlived the card is
  // a quiet store update.
  function focusField() {
    var field = fieldLoader.item
    if (field) field.forceActiveFocus()
  }

  width: parent.width
  visible: pickerOpen
  implicitHeight: visible ? pickerCol.implicitHeight + Style.spacing.sm * 2 : 0
  color: Util.alpha(Color.accent, 0.06)
  borderSpec: Border.flat(Util.alpha(Color.accent, 0.35), Math.max(1, Style.normalBorderWidth))
  radius: Style.cornerRadius

  // Rebuild the Downloads quick-picks whenever the card opens and when a
  // folder listing changes (the panel's openPicker()/rebuildPickerFiles
  // pair before the extraction).
  onPickerOpenChanged: if (pickerOpen) rebuildPickerFiles()

  // FolderListModel.filePath comes back as a plain path on some Qt builds
  // and as a file:// URL on others; normalize to a local path.
  function pickerUrlToPath(value) {
    var s = String(value || "")
    if (s.indexOf("file://") === 0) {
      s = s.substring("file://".length)
      s = s.split("/").map(decodeURIComponent).join("/")
    }
    return s
  }

  function rebuildPickerFiles() {
    var picks = []
    var models = [downloadsUpper, downloadsLower]
    for (var m = 0; m < models.length; m++) {
      var count = models[m].count
      for (var i = 0; i < count; i++) {
        try {
          var path = pickerUrlToPath(models[m].get(i, "filePath"))
          var name = String(models[m].get(i, "fileName"))
          if (path !== "") picks.push({ name: name, path: path })
        } catch (e) { /* unreadable entry — skip it */ }
      }
    }
    pickerFiles = picks
  }

  // AppImages found in ~/Downloads and ~/downloads — both spellings
  // Omarchy users end up with.
  property var pickerFiles: []

  FolderListModel {
    id: downloadsUpper
    folder: Util.fileUrl(pickerCard.homeDir + "/Downloads")
    nameFilters: ["*.AppImage", "*.appimage", "*.Appimage"]
    showDirs: false
    showDotAndDotDot: false
    sortField: FolderListModel.Name
    onCountChanged: pickerCard.rebuildPickerFiles()
    onFolderChanged: pickerCard.rebuildPickerFiles()
  }

  FolderListModel {
    id: downloadsLower
    folder: Util.fileUrl(pickerCard.homeDir + "/downloads")
    nameFilters: ["*.AppImage", "*.appimage", "*.Appimage"]
    showDirs: false
    showDotAndDotDot: false
    sortField: FolderListModel.Name
    onCountChanged: pickerCard.rebuildPickerFiles()
    onFolderChanged: pickerCard.rebuildPickerFiles()
  }

  Column {
    id: pickerCol
    width: parent.width - Style.spacing.sm * 2
    anchors.centerIn: parent
    spacing: Style.spacing.xs

    Text {
      text: pickerCard.strings.pickerTitle
      color: pickerCard.foreground
      font.family: pickerCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    Row {
      width: parent.width
      spacing: Style.spacing.xs

      Loader {
        id: fieldLoader
        active: pickerCard.pickerOpen
        width: parent.width - pickerBrowseButton.width
          - pickerGoButton.width - Style.spacing.xs * 2
        sourceComponent: pickerFieldComp
      }

      Button {
        id: pickerBrowseButton
        text: pickerCard.busyPick ? pickerCard.strings.pickerBrowsing
          : pickerCard.strings.pickerBrowse
        // Locked with Go/Downloads while a preview runs (#18):
        // a fill mid-preview-call could only produce an answer
        // the stale-drop has to throw away.
        enabled: !pickerCard.pickerBusy
        anchors.verticalCenter: parent.verticalCenter
        foreground: pickerCard.foreground
        accent: Color.accent
        fontFamily: pickerCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: pickerCard.controlHeight
        onClicked: pickerCard.browseClicked()
      }

      // The row's primary action tracks the flow's step (#18):
      // Go previews a fresh path, Confirm confirms the shown
      // preview; both show their in-flight copy while busy.
      Button {
        id: pickerGoButton
        text: pickerCard.busyIntegrate ? pickerCard.strings.integrateWorking
          : (pickerCard.busyPreview ? pickerCard.strings.previewChecking
            : (pickerCard.previewOpen ? pickerCard.strings.previewConfirm
              : pickerCard.strings.pickerGo))
        enabled: !pickerCard.pickerBusy && pickerCard.pickerPath.trim() !== ""
        anchors.verticalCenter: parent.verticalCenter
        foreground: pickerCard.foreground
        accent: Color.accent
        fontFamily: pickerCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: pickerCard.controlHeight
        onClicked: pickerCard.goClicked()
      }
    }

    // ---- Preview card (#18) — PreviewCard.qml, ticket #55 ----------
    PreviewCard {
      previewOpen: pickerCard.previewOpen
      previewDoc: pickerCard.previewDoc
      busyIntegrate: pickerCard.busyIntegrate
      busyPreview: pickerCard.busyPreview
      appLine: pickerCard.previewAppLine
      destinationLine: pickerCard.previewDestinationLine
      strings: pickerCard.strings
      foreground: pickerCard.foreground
      dim: pickerCard.dim
      urgent: pickerCard.urgent
      fontFamily: pickerCard.fontFamily
      controlHeight: pickerCard.controlHeight
      onConfirmed: pickerCard.previewConfirmed()
      onDismissed: pickerCard.previewDismissed()
    }

    Text {
      visible: pickerCard.pickerFiles.length > 0
      text: pickerCard.strings.pickerDownloads
      color: pickerCard.dim
      font.family: pickerCard.fontFamily
      font.pixelSize: Style.font.caption
    }

    Flickable {
      visible: pickerCard.pickerFiles.length > 0
      width: parent.width
      height: Math.min(pickCol.implicitHeight, Style.space(132))
      contentWidth: width
      contentHeight: pickCol.implicitHeight
      clip: true
      boundsBehavior: Flickable.StopAtBounds
      interactive: contentHeight > height

      Column {
        id: pickCol
        width: parent.width
        spacing: Style.spacing.xxs

        Repeater {
          id: quickPickRepeater
          model: pickerCard.pickerFiles

          Button {
            // Plain property + explicit assignment — required delegate
            // properties are not auto-initialized here (see the comment
            // on AppRow's modelData).
            property var modelData: quickPickRepeater.model[index]
            width: pickCol.width
            text: modelData.name
            foreground: pickerCard.foreground
            accent: Color.accent
            fontFamily: pickerCard.fontFamily
            fontSize: Style.font.caption
            enabled: !pickerCard.pickerBusy
            // Quick-picks ride the same confirm flow: seed the
            // field so the preview (and a later Confirm) name
            // the exact file, then preview — never integrate
            // directly.
            onClicked: pickerCard.quickPicked(modelData.path)
          }
        }
      }
    }

    Text {
      visible: pickerCard.pickerFiles.length === 0
      text: pickerCard.strings.pickerDownloadsEmpty
      color: pickerCard.dim
      font.family: pickerCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }
  }

  // The integrate-picker URL field (F2): Loader-created so no text input
  // exists while the card is hidden (defense in depth — see the
  // SafeTextField note above for the round-3 rule: no native editable
  // input may exist even while the card is open, so the field type itself
  // carries the guarantee; the Loader gates keep dead instances from
  // piling up at all). The picker field is a SafeTextField: `value`
  // mirrors pickerPath, `edited` writes it back, Enter previews the
  // current path (#18 confirm-before-integrate).
  Component {
    id: pickerFieldComp

    SafeTextField {
      width: parent ? parent.width : 0
      placeholderText: pickerCard.strings.pickerPlaceholder
      foreground: pickerCard.foreground
      accent: Color.accent
      font.family: pickerCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      implicitHeight: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2)
      enabled: !pickerCard.busyIntegrate && !pickerCard.busyPreview
      value: pickerCard.pickerPath
      onEdited: function(v) { pickerCard.pathEdited(v) }
      onAccepted: pickerCard.previewRequested()
      // Same count as the editor fields: the key catcher stays blocked
      // while this field types, whatever card is on screen (see
      // textFieldFocusCount in Panel.qml).
      onActiveFocusChanged: pickerCard.fieldFocusChanged(activeFocus ? 1 : -1)
    }
  }
}

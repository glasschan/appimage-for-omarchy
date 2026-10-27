import QtQuick
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model

// One installed-app row plus its inline per-app source editor. Pure view:
// the listing item arrives as `modelData`, every rendered flag is a
// property, and every action is reported as a signal the panel handles.
//
// Row meta is printed on TWO lines: the identity line and the status
// line, each rendered as ONE bounded, elided Text whose StyledText spans
// metaText() assembles through Model.escapeHtml. A Row of separate Texts
// is forbidden here — it has no width bound and Text does not clip, so
// the fragments would paint over the action buttons.
//
// The inline editor's field templates live at the bottom of this file.
// Text entry goes through SafeTextField: no native editable input may
// exist even while its card is open — the Qt 6.12 clipboard hook arms on
// every readOnly: false QQuickTextInput.
// The Repeater's model is row-gated (empty unless THIS row's editor is
// open), so a hidden editor card never instantiates a field at all.
Column {
  id: rowOuter

  // The listing item. NOT a `required` property: this component is the
  // Repeater's delegate and this Qt does not auto-initialize required
  // delegate properties — every row fails to instantiate without the
  // explicit assignment.
  property var modelData

  // Rendered state (owned by Panel.qml). jobActive is THIS row's active
  // background job (per-app single flight); progressLabel is the live
  // job-progress line for THIS row ("").
  property bool jobActive: false
  property string progressLabel: ""
  // desktop_id whose Remove button is armed for the second click.
  property string confirmRemoveId: ""
  property bool busyRemove: false
  // F9 editor state (panel-owned working copies).
  property string sourceEditId: ""
  property string sourceEditName: ""
  property string sourceEditManager: ""
  property var sourceEditValues: ({})
  property bool sourceBusy: false
  property var managerOptions: []
  property var strings
  property color foreground
  property color dim
  property color urgent
  property string fontFamily
  property real controlHeight

  // Row actions.
  signal launchRequested()
  signal updateRequested()
  signal removeRequested()
  signal sourceEditRequested()
  // F9 editor actions.
  signal sourceSaveRequested()
  signal sourceUnsetRequested()
  // A field value changed (key, string value — bools travel as
  // "true"/"false").
  signal sourceValueChanged(string key, string value)
  // The manager dropdown picked a value (the panel swaps the field set).
  signal managerSelected(string value)
  // The dropdown's popup opened/closed (the panel mirrors it so the key
  // catcher stays blocked while the popup — outside this item tree — owns
  // keys).
  signal popupOpenChanged(bool open)
  // Focus deltas from the Loader-created fields (the key catcher's focus
  // count — the panel accumulates across every editable field).
  signal fieldFocusChanged(int delta)

  width: parent.width
  spacing: Style.spacing.xs

  // Assemble one row line's segments into a single StyledText: every
  // dynamic fragment is escaped (Model.escapeHtml) and the accent
  // segments carry the accent colour. One bounded, elided Text per line —
  // a Row of separate Texts has no width bound and would paint over the
  // row's action buttons.
  function metaText(segs) {
    var parts = []
    for (var i = 0; i < segs.length; i++) {
      parts.push('<span style="color:'
        + (segs[i].accent ? Color.accent : rowOuter.dim)
        + '">'
        + Model.escapeHtml(segs[i].text)
        + "</span>")
    }
    return parts.join(" ")
  }

  BorderSurface {
    id: itemRow
    width: parent.width
    implicitHeight: rowContent.implicitHeight + Style.spacing.sm * 2
    radius: Style.cornerRadius
    color: Style.normalFillFor(rowOuter.foreground, Color.accent, rowOuter.urgent)
    borderSpec: Border.none()

    // Clicking anywhere on the card (outside the action
    // buttons, which stack above) launches the app (F4).
    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton
      onClicked: rowOuter.launchRequested()
    }

    Row {
      id: rowContent
      anchors.left: parent.left
      anchors.right: parent.right
      anchors.verticalCenter: parent.verticalCenter
      anchors.leftMargin: Style.spacing.sm
      anchors.rightMargin: Style.spacing.sm
      spacing: Style.spacing.sm

      // Extracted icon only when the listing proved it sanitised by the
      // CURRENT sanitiser (`icon_safe`, ticket #62 — the shell must never
      // open an unproved file), png then svg next to the AppImage under
      // .icons/ (CONTRACT.md filesystem layout), tabler cube-unfolded
      // icon otherwise.
      Image {
        id: appIcon
        property int iconIndex: 0
        readonly property var iconCandidates: {
          var list = []
          var base = rowOuter.modelData.iconBase
          if (base && rowOuter.modelData.iconSafe === true) {
            var exts = [".png", ".svg"]
            for (var i = 0; i < exts.length; i++) {
              list.push(Util.fileUrl(base + exts[i]))
            }
          }
          return list
        }
        anchors.verticalCenter: parent.verticalCenter
        visible: iconIndex < iconCandidates.length
        source: visible ? iconCandidates[iconIndex] : ""
        width: Style.font.body * 1.7
        height: width
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        onStatusChanged: function(status) {
          if (status === Image.Error) iconIndex++
        }
      }

      ThemeIcon {
        visible: !appIcon.visible || appIcon.status === Image.Error
        anchors.verticalCenter: parent.verticalCenter
        name: "cube-unfolded"
        size: Style.font.body * 1.7
        color: rowOuter.dim
      }

      Column {
        spacing: Style.spacing.xxs
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width - actionsRow.width - Style.space(34) - Style.spacing.sm * 2

        Text {
          text: rowOuter.modelData.name
          color: rowOuter.foreground
          font.family: rowOuter.fontFamily
          font.pixelSize: Style.font.body
          font.bold: true
          elide: Text.ElideRight
          width: parent.width
        }

        // The row's IDENTITY line: the version and the running dot.
        // ONE bounded, elided Text — separate Texts in a Row have no
        // width bound and would paint over the action buttons.
        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.StyledText
          elide: Text.ElideRight
          color: rowOuter.dim
          font.family: rowOuter.fontFamily
          font.pixelSize: Style.font.caption
          text: rowOuter.metaText(
            Model.rowIdentityMeta(rowOuter.modelData))
        }

        // The row's STATUS line: the pending release (or "cannot verify")
        // and the live job progress. It has its own line so the update
        // status never reads as part of the version — and it stays ONE
        // bounded, elided Text, never a Row of Texts.
        Text {
          width: parent.width
          visible: text !== ""
          textFormat: Text.StyledText
          elide: Text.ElideRight
          color: rowOuter.dim
          font.family: rowOuter.fontFamily
          font.pixelSize: Style.font.caption
          text: rowOuter.metaText(Model.rowStatusMeta(
            rowOuter.modelData,
            rowOuter.jobActive ? rowOuter.progressLabel : ""))
        }

        // F9: configured update source under the row's meta.
        Text {
          visible: rowOuter.modelData.manager !== ""
          text: rowOuter.modelData.embeddedSource
            ? Model.formatCount(rowOuter.modelData.manager, rowOuter.strings.sourceEmbedded)
            : rowOuter.modelData.manager
          color: rowOuter.dim
          font.family: rowOuter.fontFamily
          font.pixelSize: Style.font.caption
          elide: Text.ElideRight
          width: parent.width
        }
      }

      Row {
        id: actionsRow
        spacing: Style.spacing.xxs
        anchors.verticalCenter: parent.verticalCenter

        // Launch as an icon-only action with a tooltip.
        PanelActionButton {
          id: launchButton
          tooltipText: rowOuter.strings.launch
          foreground: rowOuter.foreground
          hoverColor: Color.accent
          anchors.verticalCenter: parent.verticalCenter

          ThemeIcon {
            anchors.centerIn: parent
            name: "player-play"
            size: launchButton.fontSize
            strokeWidth: 1.75
            color: launchButton.enabled
              ? (launchButton._hot ? launchButton.hoverColor : launchButton.foreground)
              : Qt.darker(launchButton.foreground, 2.0)
          }

          onClicked: rowOuter.launchRequested()
        }

        // One-click update, shown only once a pending release is known
        // for the row. Per-app single flight: only THIS row's button dims
        // and loops its arrow while its job runs — other rows keep
        // launching/updating in parallel, and the state behind the flag
        // is the app's job document, not a click.
        PanelActionButton {
          id: updateButton
          // Appears exactly when the row's pending verdict says a newer
          // release is known — the same rule the badge counts on, so the
          // marker, the badge and this button always agree. A source that
          // cannot prove its artifact offers no update action: the
          // backend would refuse the install, so the row shows "cannot
          // verify" instead.
          visible: rowOuter.modelData.hasUpdate === true
            && rowOuter.modelData.installable !== false
          enabled: !rowOuter.jobActive
          tooltipText: rowOuter.strings.updateAction
          foreground: rowOuter.foreground
          hoverColor: Color.accent
          fontFamily: rowOuter.fontFamily
          anchors.verticalCenter: parent.verticalCenter

          ThemeIcon {
            id: updateIcon
            anchors.centerIn: parent
            name: "arrow-up"
            size: updateButton.fontSize
            strokeWidth: 1.75
            color: updateButton.enabled
              ? (updateButton._hot ? updateButton.hoverColor : updateButton.foreground)
              : Qt.darker(updateButton.foreground, 2.0)

            // Slide+fade loop: the arrow floats out the top, reappears
            // below and slides back into place — "moving up" without a
            // spin (which reads as "loading"). The icon is anchored, so
            // the shift rides a Translate transform instead of y.
            transform: Translate {
              id: updateIconShift
              y: 0
            }

            SequentialAnimation {
              running: rowOuter.jobActive
              loops: Animation.Infinite

              ParallelAnimation {
                NumberAnimation {
                  target: updateIconShift
                  property: "y"
                  from: 0
                  to: -updateIcon.height * 0.6
                  duration: 220
                  easing.type: Easing.InQuad
                }
                NumberAnimation {
                  target: updateIcon
                  property: "opacity"
                  from: 1
                  to: 0
                  duration: 220
                  easing.type: Easing.InQuad
                }
              }

              PropertyAction {
                target: updateIconShift
                property: "y"
                value: updateIcon.height * 0.6
              }

              ParallelAnimation {
                NumberAnimation {
                  target: updateIconShift
                  property: "y"
                  from: updateIcon.height * 0.6
                  to: 0
                  duration: 220
                  easing.type: Easing.OutQuad
                }
                NumberAnimation {
                  target: updateIcon
                  property: "opacity"
                  from: 0
                  to: 1
                  duration: 220
                  easing.type: Easing.OutQuad
                }
              }

              PauseAnimation { duration: 120 }

              // The update can finish mid-loop: park the icon back at
              // rest when the loop stops.
              onRunningChanged: if (!running) {
                updateIconShift.y = 0
                updateIcon.opacity = 1
              }
            }
          }

          onClicked: rowOuter.updateRequested()
        }

        // Open this row's inline source editor.
        PanelActionButton {
          id: sourceRowButton
          tooltipText: rowOuter.strings.sourceEditTooltip
          foreground: rowOuter.foreground
          hoverColor: Color.accent
          anchors.verticalCenter: parent.verticalCenter

          ThemeIcon {
            anchors.centerIn: parent
            name: "settings"
            size: sourceRowButton.fontSize
            strokeWidth: 1.75
            color: sourceRowButton.enabled
              ? (sourceRowButton._hot ? sourceRowButton.hoverColor : sourceRowButton.foreground)
              : Qt.darker(sourceRowButton.foreground, 2.0)
          }

          onClicked: rowOuter.sourceEditRequested()
        }

        // Two-click inline remove: first click arms "Remove?" on this
        // row only, second click runs it. Blocked while this row's update
        // job runs: the job is installing over the AppImage, so a remove
        // would delete the target under it.
        Button {
          visible: rowOuter.confirmRemoveId === rowOuter.modelData.id
          text: rowOuter.strings.removeConfirm
          anchors.verticalCenter: parent.verticalCenter
          foreground: rowOuter.urgent
          accent: rowOuter.urgent
          fontFamily: rowOuter.fontFamily
          fontSize: Style.font.caption
          implicitHeight: rowOuter.controlHeight
          enabled: !rowOuter.busyRemove && !rowOuter.jobActive
          onClicked: rowOuter.removeRequested()
        }

        PanelActionButton {
          id: removeButton
          visible: rowOuter.confirmRemoveId !== rowOuter.modelData.id
          // same per-row gate as the update button: only THIS row dims
          // while ITS job runs (other rows stay removable)
          enabled: !rowOuter.jobActive
          tooltipText: rowOuter.strings.removeTooltip
          foreground: rowOuter.foreground
          hoverColor: rowOuter.urgent
          fontFamily: rowOuter.fontFamily
          anchors.verticalCenter: parent.verticalCenter

          ThemeIcon {
            anchors.centerIn: parent
            name: "trash"
            size: removeButton.fontSize
            strokeWidth: 1.75
            color: removeButton.enabled
              ? (removeButton._hot ? removeButton.hoverColor : removeButton.foreground)
              : Qt.darker(removeButton.foreground, 2.0)
          }

          onClicked: rowOuter.removeRequested()
        }
      }
    }
  }

  // Inline editor under the row whose gear was clicked: the app name
  // fixes context, the manager dropdown swaps the field set from
  // Model.MANAGERS.
  BorderSurface {
    width: parent.width
    visible: rowOuter.sourceEditId === rowOuter.modelData.id
    implicitHeight: visible ? sourceEditCol.implicitHeight + Style.spacing.xl * 2 : 0
    color: Util.alpha(Color.accent, 0.06)
    borderSpec: Border.flat(Util.alpha(Color.accent, 0.25), Math.max(1, Style.normalBorderWidth))
    radius: Style.cornerRadius

    Column {
      id: sourceEditCol
      width: parent.width - Style.spacing.xl * 2
      anchors.centerIn: parent
      spacing: Style.spacing.xs

      Text {
        text: rowOuter.sourceEditName
        color: rowOuter.foreground
        font.family: rowOuter.fontFamily
        font.pixelSize: Style.font.bodySmall
        font.bold: true
        elide: Text.ElideRight
        width: parent.width
      }

      Dropdown {
        id: sourceManagerDropdown
        label: rowOuter.strings.sourceManagerLabel
        width: parent.width
        options: rowOuter.managerOptions
        foreground: rowOuter.foreground
        accent: Color.accent
        fontFamily: rowOuter.fontFamily
        enabled: !rowOuter.sourceBusy
        onPopupOpenChanged: rowOuter.popupOpenChanged(sourceManagerDropdown.popupOpen)
        onChanged: function(value) {
          rowOuter.managerSelected(value)
        }

        // selectCurrent() writes `value` directly, which would sever a
        // plain value binding on the persisted delegate; a one-way
        // Binding survives.
        Binding {
          target: sourceManagerDropdown
          property: "value"
          value: rowOuter.sourceEditManager
        }
      }

      Repeater {
        id: sourceFieldsRepeater
        // The model is global (the panel's sourceEditManager), so
        // without the row-visibility gate every other row's hidden
        // editor card would instantiate the same editable inputs — no
        // field may exist while its card is hidden.
        model: {
          if (rowOuter.sourceEditId !== rowOuter.modelData.id) return []
          var mgr = Model.managersByName()[rowOuter.sourceEditManager]
          return mgr ? mgr.fields : []
        }

        Loader {
          // Plain property + explicit assignment (see the modelData note
          // on rowOuter): required delegate properties are never
          // auto-initialized here.
          property var modelData: sourceFieldsRepeater.model[index]
          width: sourceEditCol.width
          sourceComponent: modelData.type === "bool"
            ? sourceBoolFieldComp : sourceStringFieldComp
          onLoaded: item.fieldKey = modelData.key
        }
      }

      Row {
        spacing: Style.spacing.xs

        Button {
          id: sourceSaveButton
          text: rowOuter.strings.sourceSave
          enabled: !rowOuter.sourceBusy && rowOuter.sourceEditManager !== ""
          foreground: rowOuter.foreground
          accent: Color.accent
          fontFamily: rowOuter.fontFamily
          fontSize: Style.font.caption
          implicitHeight: rowOuter.controlHeight
          onClicked: rowOuter.sourceSaveRequested()
        }

        Button {
          id: sourceUnsetButton
          text: rowOuter.strings.sourceUnset
          enabled: !rowOuter.sourceBusy
          foreground: rowOuter.foreground
          accent: Color.accent
          fontFamily: rowOuter.fontFamily
          fontSize: Style.font.caption
          implicitHeight: rowOuter.controlHeight
          onClicked: rowOuter.sourceUnsetRequested()
        }
      }
    }
  }

  // Source editor field templates: one Loader per MANAGERS field in the
  // editor above, keyed by `fieldKey` into the panel's sourceEditValues.
  // Bool values are "true"/"false" strings so the Save argv passes them
  // through verbatim; the focus deltas tell the key catcher when a field
  // is typing (see formEditing in Panel.qml).
  //
  // Text entry goes through SafeTextField: no native editable input may
  // exist even while its card is open — the Qt 6.12 clipboard hook arms
  // on every readOnly: false QQuickTextInput. `value` mirrors
  // sourceEditValues; `edited` writes it back storage-only (the panel's
  // setSourceValue never reassigns the object, so the value binding never
  // re-evaluates mid-typing).
  Component {
    id: sourceStringFieldComp

    SafeTextField {
      property string fieldKey: ""
      placeholderText: Model.fieldLabel(fieldKey)
      foreground: rowOuter.foreground
      accent: Color.accent
      font.family: rowOuter.fontFamily
      font.pixelSize: Style.font.bodySmall
      implicitHeight: Math.max(Style.spacing.controlHeight, Style.font.body + Style.spacing.controlPaddingY * 2)
      width: parent ? parent.width : 0
      enabled: !rowOuter.sourceBusy
      value: rowOuter.sourceEditValues[fieldKey] || ""
      onEdited: function(v) { rowOuter.sourceValueChanged(fieldKey, v) }
      onActiveFocusChanged: rowOuter.fieldFocusChanged(activeFocus ? 1 : -1)
    }
  }

  Component {
    id: sourceBoolFieldComp

    Row {
      property string fieldKey: ""
      width: parent ? parent.width : 0
      spacing: Style.spacing.sm

      Text {
        text: Model.fieldLabel(fieldKey)
        color: rowOuter.foreground
        font.family: rowOuter.fontFamily
        font.pixelSize: Style.font.bodySmall
        anchors.verticalCenter: parent.verticalCenter
      }

      ToggleSwitch {
        checked: rowOuter.sourceEditValues[fieldKey] === "true"
        foreground: rowOuter.foreground
        accent: Color.accent
        enabled: !rowOuter.sourceBusy
        anchors.verticalCenter: parent.verticalCenter
        onToggled: rowOuter.sourceValueChanged(fieldKey,
          rowOuter.sourceEditValues[fieldKey] === "true" ? "false" : "true")
      }
    }
  }
}

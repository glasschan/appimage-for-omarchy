import QtQuick
import qs.Commons
import qs.Ui

// "Update all" consent card. Pure view: the panel owns the pinned app
// list, the derived wording (Model.updateAllSummary) and the batch spawn
// queue; this card renders the ask and reports the two buttons.
//
// Opened by the header's Update-all action; it starts NOTHING by itself.
// The card names the apps the batch would update and the processes it
// would close (the listing's `running_pids`; a count that is not
// determinable keeps the count-free sentence). Enter confirms, Esc cancels
// (PanelKeyCatcher wiring in Panel.qml); Cancel spawns nothing and closes
// no app.
BorderSurface {
  id: updateAllCard

  // Rendered state (owned by Panel.qml). appsLine/processLine are the
  // Model.updateAllSummary lines for the pinned app list.
  property bool updateAllOpen: false
  property string appsLine: ""
  property string processLine: ""
  // Confirm is enabled exactly while the batch still has pending apps
  // (the panel's pendingIds): the card can outlive a slow spawn — never
  // stack a second job for an app that is already updating.
  property bool confirmEnabled: false
  property var strings
  property color foreground
  property color dim
  property color urgent
  property string fontFamily
  property real controlHeight

  // Enter on the key catcher → confirmed(); Esc/Cancel → dismissed().
  signal confirmed()
  signal dismissed()

  width: parent.width
  visible: updateAllOpen
  implicitHeight: visible ? updateAllCol.implicitHeight + Style.spacing.sm * 2 : 0
  color: Util.alpha(urgent, 0.09)
  borderSpec: Border.flat(Util.alpha(urgent, 0.45), Math.max(1, Style.normalBorderWidth))
  radius: Style.cornerRadius

  Column {
    id: updateAllCol
    width: parent.width - Style.spacing.sm * 2
    anchors.centerIn: parent
    spacing: Style.spacing.xs

    Text {
      text: updateAllCard.strings.updateAllTitle
      color: updateAllCard.foreground
      font.family: updateAllCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    Text {
      text: updateAllCard.appsLine
      // desktop-entry names are attacker-controlled: never let
      // Text rich-detect them
      textFormat: Text.PlainText
      color: updateAllCard.foreground
      font.family: updateAllCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Text {
      text: updateAllCard.processLine
      textFormat: Text.PlainText
      color: updateAllCard.foreground
      font.family: updateAllCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Row {
      spacing: Style.spacing.xs

      Button {
        text: updateAllCard.strings.updateAllConfirm
        // The card can outlive a slow spawn: never stack a
        // second job for an app that is already updating.
        enabled: updateAllCard.confirmEnabled
        foreground: updateAllCard.foreground
        accent: updateAllCard.urgent
        fontFamily: updateAllCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: updateAllCard.controlHeight
        onClicked: updateAllCard.confirmed()
      }

      Button {
        text: updateAllCard.strings.pickerCancel
        foreground: updateAllCard.foreground
        accent: Color.accent
        fontFamily: updateAllCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: updateAllCard.controlHeight
        onClicked: updateAllCard.dismissed()
      }
    }

    Text {
      text: updateAllCard.strings.updateAllKeysHint
      color: updateAllCard.dim
      font.family: updateAllCard.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}

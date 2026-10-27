import QtQuick
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model

// Force-quit confirm card. Pure view: the panel owns the pinned trio
// (id, name, pid count), the confirm handler and the --kill-running
// grant; this card renders the ask and reports the two buttons.
//
// Shown instead of spawning when Update lands on a row the last listing
// said runs. It names the app and how many processes the confirm will
// close (the listing's `running_pids`; a count of 0 — unknown — keeps the
// count-free sentence). Enter confirms, Esc cancels (PanelKeyCatcher
// wiring in Panel.qml); Cancel spawns nothing and leaves the row as it
// was.
BorderSurface {
  id: forceQuitCard

  // Rendered state (owned by Panel.qml).
  property string forceQuitId: ""
  property string forceQuitName: ""
  property int forceQuitPids: 0
  // Whether THIS app's background job is active (the panel's jobActive()
  // on the pinned id): the confirm button never stacks a second job.
  property bool jobActive: false
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
  visible: forceQuitId !== ""
  implicitHeight: visible ? forceQuitCol.implicitHeight + Style.spacing.sm * 2 : 0
  color: Util.alpha(urgent, 0.09)
  borderSpec: Border.flat(Util.alpha(urgent, 0.45), Math.max(1, Style.normalBorderWidth))
  radius: Style.cornerRadius

  Column {
    id: forceQuitCol
    width: parent.width - Style.spacing.sm * 2
    anchors.centerIn: parent
    spacing: Style.spacing.xs

    Text {
      text: forceQuitCard.strings.forceQuitTitle
      color: forceQuitCard.foreground
      font.family: forceQuitCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    Text {
      text: Model.forceQuitBody(forceQuitCard.forceQuitName,
        forceQuitCard.forceQuitPids)
      color: forceQuitCard.foreground
      font.family: forceQuitCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Row {
      spacing: Style.spacing.xs

      Button {
        text: forceQuitCard.strings.forceQuitConfirm
        // The confirm card can outlive its row's job-start reply
        // (a slow backend): never stack a second job by hand.
        enabled: !forceQuitCard.jobActive
        foreground: forceQuitCard.foreground
        accent: forceQuitCard.urgent
        fontFamily: forceQuitCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: forceQuitCard.controlHeight
        onClicked: forceQuitCard.confirmed()
      }

      Button {
        text: forceQuitCard.strings.pickerCancel
        foreground: forceQuitCard.foreground
        accent: Color.accent
        fontFamily: forceQuitCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: forceQuitCard.controlHeight
        onClicked: forceQuitCard.dismissed()
      }
    }

    Text {
      text: forceQuitCard.strings.forceQuitKeysHint
      color: forceQuitCard.dim
      font.family: forceQuitCard.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}

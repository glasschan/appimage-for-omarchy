import QtQuick
import qs.Commons
import qs.Ui

// Python dependency card (authorized install) — extracted from Panel.qml
// (ticket #55). Pure view: the panel owns the probe/install state and the
// backend bridge; this card renders it and reports the button press.
//
// Shown only after the probe positively confirmed python3 is absent. The
// button never installs anything by itself: it opens omarchy's floating
// terminal presenting `/usr/bin/omarchy pkg add python` so the user watches
// and authorizes every step (hyprmoncfg's plugin-managed-dependency
// pattern).
BorderSurface {
  id: pythonCard

  // Rendered state (owned by Panel.qml).
  property bool pythonVisible: false
  property bool installingPython: false
  property var strings
  property color foreground
  property color dim
  property string fontFamily
  property real controlHeight

  // The panel's installPython() (verify-then-start) — nothing runs until
  // the user presses this.
  signal installRequested()

  width: parent.width
  visible: pythonVisible
  implicitHeight: visible ? pythonCol.implicitHeight + Style.spacing.sm * 2 : 0
  color: Util.alpha(Color.urgent, 0.06)
  borderSpec: Border.flat(Util.alpha(Color.urgent, 0.35), Math.max(1, Style.normalBorderWidth))
  radius: Style.cornerRadius

  Column {
    id: pythonCol
    width: parent.width - Style.spacing.sm * 2
    anchors.centerIn: parent
    spacing: Style.spacing.xs

    Text {
      text: pythonCard.strings.pythonMissingTitle
      color: pythonCard.foreground
      font.family: pythonCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    Text {
      text: pythonCard.strings.pythonMissingHint
      color: pythonCard.dim
      font.family: pythonCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Button {
      width: parent.width
      text: pythonCard.installingPython
        ? pythonCard.strings.installingPython : pythonCard.strings.installPython
      enabled: !pythonCard.installingPython
      foreground: pythonCard.foreground
      accent: Color.accent
      fontFamily: pythonCard.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: pythonCard.controlHeight
      onClicked: pythonCard.installRequested()
    }
  }
}

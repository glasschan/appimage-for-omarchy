import QtQuick
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model

// Confirm-before-integrate preview card (#18) — extracted from Panel.qml
// (ticket #55). Rendered inside IntegratePicker.qml (it lives in the
// picker card's column); the panel owns the preview state and the
// confirm/dismiss handlers.
//
// Rendered from Model.parsePreviewJson's defensive mapping (unknown keys
// tolerated, null app/destination normalize to empty). The
// reduced-assurance line is deliberately prominent — bold and
// urgent-colored: informing BEFORE the download is the whole point of the
// preview. Enter confirms, Escape goes back (PanelKeyCatcher wiring in
// Panel.qml).
BorderSurface {
  id: previewCard

  // Rendered state (owned by Panel.qml). appLine/destinationLine are the
  // panel's previewAppLine()/previewDestinationLine() results — an empty
  // appName means the backend could not know yet, and the panel's copy
  // says so instead of showing blank.
  property bool previewOpen: false
  property var previewDoc: null
  property bool busyIntegrate: false
  property bool busyPreview: false
  property string appLine: ""
  property string destinationLine: ""
  property var strings
  property color foreground
  property color dim
  property color urgent
  property string fontFamily
  property real controlHeight

  // Enter/Confirm → confirmed(); Esc/Cancel → dismissed(). Both disabled
  // while either flow leg is in flight.
  signal confirmed()
  signal dismissed()

  width: parent.width
  visible: previewOpen
  implicitHeight: visible ? previewCol.implicitHeight + Style.spacing.sm * 2 : 0
  color: Util.alpha(Color.accent, 0.09)
  borderSpec: Border.flat(Util.alpha(Color.accent, 0.45), Math.max(1, Style.normalBorderWidth))
  radius: Style.cornerRadius

  Column {
    id: previewCol
    width: parent.width - Style.spacing.sm * 2
    anchors.centerIn: parent
    spacing: Style.spacing.xs

    Text {
      text: previewCard.strings.previewTitle
      color: previewCard.foreground
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    Text {
      text: previewCard.appLine
      color: previewCard.foreground
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      elide: Text.ElideRight
      width: parent.width
    }

    Text {
      visible: previewCard.previewDoc && previewCard.previewDoc.appVersion !== ""
      text: previewCard.previewDoc
        ? Model.formatCount(previewCard.previewDoc.appVersion,
            previewCard.strings.previewVersion) : ""
      color: previewCard.dim
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      text: previewCard.destinationLine
      color: previewCard.dim
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Text {
      visible: previewCard.previewDoc && previewCard.previewDoc.reducedAssurance
      text: previewCard.strings.previewReducedAssurance
      color: previewCard.urgent
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
      wrapMode: Text.WordWrap
      width: parent.width
    }

    // The backend's own sentence, rendered verbatim (the
    // contract designates `note` as panel-copy).
    Text {
      text: previewCard.previewDoc ? previewCard.previewDoc.verificationNote : ""
      color: previewCard.dim
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Text {
      visible: previewCard.previewDoc && previewCard.previewDoc.alreadyIntegrated
      text: previewCard.strings.previewAlreadyIntegrated
      color: previewCard.foreground
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Text {
      visible: previewCard.previewDoc && previewCard.previewDoc.nameConflict
      text: previewCard.strings.previewNameConflict
      color: previewCard.foreground
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Row {
      spacing: Style.spacing.xs

      Button {
        text: previewCard.strings.previewConfirm
        enabled: !previewCard.busyIntegrate && !previewCard.busyPreview
        foreground: previewCard.foreground
        accent: Color.accent
        fontFamily: previewCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: previewCard.controlHeight
        onClicked: previewCard.confirmed()
      }

      Button {
        text: previewCard.strings.pickerCancel
        enabled: !previewCard.busyIntegrate && !previewCard.busyPreview
        foreground: previewCard.foreground
        accent: Color.accent
        fontFamily: previewCard.fontFamily
        fontSize: Style.font.bodySmall
        implicitHeight: previewCard.controlHeight
        onClicked: previewCard.dismissed()
      }
    }

    Text {
      text: previewCard.strings.previewKeysHint
      color: previewCard.dim
      font.family: previewCard.fontFamily
      font.pixelSize: Style.font.caption
    }
  }
}

import QtQuick
import qs.Commons
import qs.Ui

// Header: title, counts, actions — extracted from Panel.qml (ticket #55).
// Pure view: every flag it renders and every action it reports stay owned
// by Panel.qml.
//
// The shell Button has no image slot (its iconText is a Nerd Font glyph),
// so each action supplies the tabler icon in an inner Row while Button
// keeps owning every state visual (hover, focus, fills); the width
// override mirrors Button's own label-based sizing math over that row.
// The caption lives in the tooltip, like the icon-only row buttons.
Item {
  id: cardHeader

  // Rendered state (owned by Panel.qml).
  property bool pickerOpen: false
  property bool busyList: false
  property bool busyUpdates: false
  // The apps a batch update would touch: the Update-all button is enabled
  // exactly while this is not empty.
  property var pendingIds: []
  property bool pinned: false
  property string subtitleText: ""
  property var strings
  property color foreground
  property color dim
  property string fontFamily
  property real controlHeight

  // The [Integrate] button toggles the picker (open or cancel);
  // refresh/check-updates run their verbs; update-all opens the consent
  // card; pin flips the overlay into a floating window; settings toggles
  // the settings card. All handled by the panel.
  signal integrateClicked()
  signal refreshClicked()
  signal checkUpdatesClicked()
  signal updateAllClicked()
  signal pinToggled()
  signal settingsClicked()

  width: parent.width
  implicitHeight: headerRow.implicitHeight

  Row {
    id: headerRow
    anchors.left: parent.left
    anchors.right: parent.right
    spacing: Style.spacing.controlGap

    Column {
      id: headerText
      spacing: Style.spacing.xxs
      anchors.verticalCenter: parent.verticalCenter

      Text {
        text: cardHeader.strings.panelTitle
        color: cardHeader.foreground
        font.family: cardHeader.fontFamily
        font.pixelSize: Style.font.heading
        font.bold: true
      }

      Text {
        text: cardHeader.subtitleText
        color: cardHeader.dim
        font.family: cardHeader.fontFamily
        font.pixelSize: Style.font.caption
      }
    }

    Item { width: 1; height: 1; anchors.verticalCenter: parent.verticalCenter }

    Button {
      id: integrateButton
      text: ""
      anchors.verticalCenter: parent.verticalCenter
      foreground: cardHeader.foreground
      accent: Color.accent
      fontFamily: cardHeader.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: cardHeader.controlHeight
      tooltipText: cardHeader.pickerOpen ? cardHeader.strings.pickerCancel : cardHeader.strings.integrate
      implicitWidth: integrateRow.implicitWidth
        + integrateButton.horizontalPadding * 2
        + integrateButton._reservedBorderLeft
        + integrateButton._reservedBorderRight

      Row {
        id: integrateRow
        anchors.centerIn: parent
        spacing: Style.spacing.xs

        ThemeIcon {
          name: "plus"
          size: Style.font.icon
          strokeWidth: 1.75
          color: integrateButton.foreground
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      onClicked: cardHeader.integrateClicked()
    }

    Button {
      id: refreshButton
      text: ""
      enabled: !cardHeader.busyList
      anchors.verticalCenter: parent.verticalCenter
      foreground: cardHeader.foreground
      accent: Color.accent
      fontFamily: cardHeader.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: cardHeader.controlHeight
      tooltipText: cardHeader.strings.refresh
      implicitWidth: refreshRow.implicitWidth
        + refreshButton.horizontalPadding * 2
        + refreshButton._reservedBorderLeft
        + refreshButton._reservedBorderRight

      Row {
        id: refreshRow
        anchors.centerIn: parent
        spacing: Style.spacing.xs

        ThemeIcon {
          name: "refresh"
          size: Style.font.icon
          strokeWidth: 1.75
          color: refreshButton.foreground
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      onClicked: cardHeader.refreshClicked()
    }

    // F6 as an icon-only header button (tooltip carries the
    // caption the row buttons would show); same Button+ThemeIcon
    // row pattern as Integrate/Refresh above.
    Button {
      id: checkUpdatesButton
      text: ""
      enabled: !cardHeader.busyUpdates
      anchors.verticalCenter: parent.verticalCenter
      foreground: cardHeader.foreground
      accent: Color.accent
      fontFamily: cardHeader.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: cardHeader.controlHeight
      tooltipText: cardHeader.strings.checkUpdates
      implicitWidth: checkUpdatesRow.implicitWidth
        + checkUpdatesButton.horizontalPadding * 2
        + checkUpdatesButton._reservedBorderLeft
        + checkUpdatesButton._reservedBorderRight

      Row {
        id: checkUpdatesRow
        anchors.centerIn: parent
        spacing: Style.spacing.xs

        ThemeIcon {
          name: "cloud-download"
          size: Style.font.icon
          strokeWidth: 1.75
          color: checkUpdatesButton.foreground
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      onClicked: cardHeader.checkUpdatesClicked()
    }

    // "Update all" (issue #52): one action for every pending
    // app, next to the check-updates action and reusing the
    // row's arrow-up icon. The click only OPENS the consent
    // card — nothing is spawned until the user accepts it. The
    // button is enabled exactly while at least one app has a
    // pending release the backend would accept.
    Button {
      id: updateAllButton
      text: ""
      enabled: cardHeader.pendingIds.length > 0
      anchors.verticalCenter: parent.verticalCenter
      foreground: cardHeader.foreground
      accent: Color.accent
      fontFamily: cardHeader.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: cardHeader.controlHeight
      tooltipText: cardHeader.strings.updateAllTooltip
      implicitWidth: updateAllRow.implicitWidth
        + updateAllButton.horizontalPadding * 2
        + updateAllButton._reservedBorderLeft
        + updateAllButton._reservedBorderRight

      Row {
        id: updateAllRow
        anchors.centerIn: parent
        spacing: Style.spacing.xs

        ThemeIcon {
          name: "arrow-up"
          size: Style.font.icon
          strokeWidth: 1.75
          color: updateAllButton.enabled
            ? updateAllButton.foreground
            : cardHeader.dim
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      onClicked: cardHeader.updateAllClicked()
    }

    // Pin toggle: flips the overlay into a compact floating
    // window (session-only) so other apps stay clickable.
    Button {
      id: pinButton
      text: ""
      anchors.verticalCenter: parent.verticalCenter
      foreground: cardHeader.foreground
      accent: Color.accent
      fontFamily: cardHeader.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: cardHeader.controlHeight
      tooltipText: cardHeader.pinned ? cardHeader.strings.pinTooltipUnpin : cardHeader.strings.pinTooltip
      implicitWidth: pinRow.implicitWidth
        + pinButton.horizontalPadding * 2
        + pinButton._reservedBorderLeft
        + pinButton._reservedBorderRight

      Row {
        id: pinRow
        anchors.centerIn: parent
        spacing: Style.spacing.xs

        ThemeIcon {
          name: "pin"
          size: Style.font.icon
          strokeWidth: 1.75
          color: pinButton.foreground
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      onClicked: cardHeader.pinToggled()
    }

    // Settings gear: toggles the settings card below.
    Button {
      id: settingsButton
      text: ""
      anchors.verticalCenter: parent.verticalCenter
      foreground: cardHeader.foreground
      accent: Color.accent
      fontFamily: cardHeader.fontFamily
      fontSize: Style.font.bodySmall
      implicitHeight: cardHeader.controlHeight
      tooltipText: cardHeader.strings.settingsTitle
      implicitWidth: settingsRow.implicitWidth
        + settingsButton.horizontalPadding * 2
        + settingsButton._reservedBorderLeft
        + settingsButton._reservedBorderRight

      Row {
        id: settingsRow
        anchors.centerIn: parent
        spacing: Style.spacing.xs

        ThemeIcon {
          name: "settings"
          size: Style.font.icon
          strokeWidth: 1.75
          color: settingsButton.foreground
          anchors.verticalCenter: parent.verticalCenter
        }
      }

      onClicked: cardHeader.settingsClicked()
    }
  }
}

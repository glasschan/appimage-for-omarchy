import QtQuick
import qs.Commons
import qs.Ui

// Settings card (update-check cadence). Pure view: the working-copy
// state (editEnabled/editInterval/editDelay), the --set-setting save
// chain and the store re-read stay in Panel.qml; this card renders them
// and reports the toggle and the Save press.
//
// The cadence number fields stay Loader-gated on the card's visibility
// (defense in depth for the no-native-editable-input rule): the inner
// SpinBox TextInput exists only while the card is open, and its SpinBox
// field is hard-flipped to `editable: false` on load — value changes go
// through the spin buttons only.
BorderSurface {
  id: settingsCard

  // Rendered state (owned by Panel.qml).
  property bool settingsOpen: false
  property bool settingsBusy: false
  property bool editEnabled: true
  property var strings
  property color foreground
  property color dim
  property string fontFamily
  property real controlHeight

  // The enable toggle flipped (the panel flips its working copy).
  signal enableToggled()
  // Save pressed.
  signal saveRequested()

  // Whether either cadence field's inner editor holds focus — Panel's
  // key-catcher guard (formEditing) reads this instead of the loader item
  // it used before the extraction.
  readonly property bool anyFieldFocused:
    (settingsNumbersLoader.item
     ? settingsNumbersLoader.item.anyFieldFocused : false)

  width: parent.width
  visible: settingsOpen
  implicitHeight: visible ? settingsCol.implicitHeight + Style.spacing.xl * 2 : 0
  color: Util.alpha(Color.accent, 0.06)
  borderSpec: Border.flat(Util.alpha(Color.accent, 0.35), Math.max(1, Style.normalBorderWidth))
  radius: Style.cornerRadius

  Column {
    id: settingsCol
    width: parent.width - Style.spacing.xl * 2
    anchors.centerIn: parent
    spacing: Style.spacing.xs

    Text {
      text: settingsCard.strings.settingsTitle
      color: settingsCard.foreground
      font.family: settingsCard.fontFamily
      font.pixelSize: Style.font.bodySmall
      font.bold: true
    }

    // ---- update-check cadence ---------------------------------------
    PanelSectionHeader {
      text: settingsCard.strings.settingsUpdateChecks
      foreground: settingsCard.foreground
      width: parent.width
    }

    Row {
      width: parent.width
      spacing: Style.spacing.sm

      Text {
        text: settingsCard.editEnabled ? settingsCard.strings.enabledLabel
          : settingsCard.strings.disabledLabel
        color: settingsCard.foreground
        font.family: settingsCard.fontFamily
        font.pixelSize: Style.font.bodySmall
        elide: Text.ElideRight
        width: parent.width - settingsToggle.width - Style.spacing.sm
        anchors.verticalCenter: parent.verticalCenter
      }

      // Caller-owned value: `checked` binds to the working copy
      // and toggled() flips it (ToggleSwitch's own contract).
      ToggleSwitch {
        id: settingsToggle
        checked: settingsCard.editEnabled
        foreground: settingsCard.foreground
        accent: Color.accent
        enabled: !settingsCard.settingsBusy
        anchors.verticalCenter: parent.verticalCenter
        onToggled: settingsCard.enableToggled()
      }
    }

    Loader {
      id: settingsNumbersLoader
      active: settingsCard.settingsOpen
      width: parent.width
      sourceComponent: settingsNumbersComp
    }

    Text {
      text: settingsCard.strings.notificationsOnNewUpdates
      color: settingsCard.dim
      font.family: settingsCard.fontFamily
      font.pixelSize: Style.font.caption
      wrapMode: Text.WordWrap
      width: parent.width
    }

    Button {
      id: settingsSaveButton
      text: settingsCard.strings.settingsSave
      enabled: !settingsCard.settingsBusy
      foreground: settingsCard.foreground
      accent: Color.accent
      fontFamily: settingsCard.fontFamily
      fontSize: Style.font.caption
      implicitHeight: settingsCard.controlHeight
      onClicked: settingsCard.saveRequested()
    }
  }

  // The cadence fields: Loader-created so no text input exists while the
  // card is hidden (defense in depth — no native editable input may exist
  // even while the card is open; the Loader gates keep dead instances
  // from piling up at all). The SpinBox contentItem is a native
  // TextInput that arms the Qt 6.12 clipboard hook while editable —
  // `editable: false` is the one switch that flips it readOnly; value
  // changes go through the spin buttons only.
  Component {
    id: settingsNumbersComp

    Row {
      width: parent ? parent.width : 0
      spacing: Style.spacing.sm

      readonly property bool anyFieldFocused:
        (intervalField.field.contentItem
         ? intervalField.field.contentItem.activeFocus : false)
        || (delayField.field.contentItem
            ? delayField.field.contentItem.activeFocus : false)

      NumberField {
        id: intervalField
        label: settingsCard.strings.checkIntervalMinutes
        value: settingsCard.editInterval
        from: 15
        to: 10080
        enabled: !settingsCard.settingsBusy
        foreground: settingsCard.foreground
        accent: Color.accent
        fontFamily: settingsCard.fontFamily
        Component.onCompleted: field.editable = false
        onModified: function(v) { settingsCard.editInterval = v }
      }

      NumberField {
        id: delayField
        label: settingsCard.strings.firstCheckDelayMinutes
        value: settingsCard.editDelay
        from: 0
        to: 1440
        enabled: !settingsCard.settingsBusy
        foreground: settingsCard.foreground
        accent: Color.accent
        fontFamily: settingsCard.fontFamily
        Component.onCompleted: field.editable = false
        onModified: function(v) { settingsCard.editDelay = v }
      }
    }
  }
}

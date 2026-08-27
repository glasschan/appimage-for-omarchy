import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "lib/Model.js" as Model
import "lib/Backend.js" as Backend

// Bar entry for the AppImage plugin: a tabler package icon plus the
// installed count, and an urgent tint once updates are pending.
//
// This plugin declares both "bar-widget" and "panel" kinds, so the panel
// is owned by the shell's panel loader (summon path) rather than hosted
// inside this widget — the same shape omarchy.menu uses. The button asks
// the shell to summon/hide it, and the lifecycle members below keep the
// bar's widget shape contract (Bar.findPanelWidget and the popout
// coordinator read open/close/opened/popoutSwitchClosing off this root).
BarWidget {
  id: root
  moduleName: "io.github.glasschan.appimage"

  // Tabler icon name for the bar mark (icons/package.svg via ThemeIcon);
  // the icon follows the bar's foreground/urgent colors, so no theme
  // knowledge lives here.

  // Mirrored from the shared Model store; Model has no property-change
  // notifications (it is a plain JS library), so a subscription copies
  // the counts into reactive properties whenever the store changes.
  property int installedCount: 0
  property int updateCount: 0
  property var unsubscribe: null

  // Current Backend.run context for the one-shot startup sync (null
  // while idle); the Process/Timer handlers below feed it back into
  // lib/Backend.js.
  property var syncCtx: null

  property bool popoutSwitchClosing: false

  // Badge urgency follows the theme's urgent color; nothing is hardcoded.
  readonly property color badgeColor: updateCount > 0 && bar && bar.urgent
    ? bar.urgent
    : (bar ? bar.barForeground : Color.foreground)

  // The bar host injects no manifest, so the canonical third-party plugin
  // install location (~/.config/omarchy/plugins/<id>, PluginRegistry.qml)
  // is the best guess for the backend script at login. The panel later
  // reconfigures the store from its injected manifest.__sourceDir, which
  // is authoritative; this only seeds the badge count early.
  readonly property string homeDir: Quickshell.env("HOME") || ""
  readonly property string backendScript: homeDir + "/.config/omarchy/plugins/"
    + moduleName + "/backend/main.py"

  function shellHost() {
    return bar && bar.shell ? bar.shell : null
  }

  function syncFromModel() {
    installedCount = Model.installedCount()
    updateCount = Model.updateCount()
  }

  // ---- panel lifecycle forwarding (to the shell's panel loader) ----------

  readonly property bool opened: {
    var host = shellHost()
    return host && typeof host.isPluginOpen === "function"
      ? host.isPluginOpen(moduleName) === true
      : false
  }

  function open() {
    var host = shellHost()
    if (host) host.summon(moduleName, "{}")
  }

  function close() {
    var host = shellHost()
    if (host) host.hide(moduleName)
  }

  function togglePanel() {
    var host = shellHost()
    if (host) host.toggle(moduleName, "{}")
  }

  // The bar prefers this over plain close() when swapping popouts; the
  // flag mirrors Ui.Panel's contract for whoever reads it back off us.
  function closeForPopoutSwitch() {
    popoutSwitchClosing = true
    close()
    Qt.callLater(function() { root.popoutSwitchClosing = false })
  }

  // ---- one-shot startup sync -----------------------------------------------
  //
  // A bar surface exists per monitor but a single backend probe at login
  // is enough (claimStartupSync is engine-wide): it fills the store so
  // the badge starts truthful and the first panel open renders from
  // cache. Failures are silent here — the panel's refresh owns error
  // reporting (with the python3 hint), and a dev checkout that is not
  // installed under ~/.config/omarchy/plugins must not raise alarms.

  function startupSync() {
    if (!Model.claimStartupSync()) return
    var ctx = Backend.run(syncProc, syncTimer, ["python3", backendScript],
      ["--list-installed", "--json"], {}, function(result) {
        if (result.ok) {
          var parsed = Model.parseListJson(result.stdout)
          if (parsed.ok) Model.setItems(parsed.items)
        }
      })
    syncCtx = ctx && ctx.active ? ctx : null
  }

  Component.onCompleted: {
    syncFromModel()
    unsubscribe = Model.subscribe(syncFromModel)
    startupSync()
  }

  Component.onDestruction: {
    if (unsubscribe) unsubscribe()
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // The tabler package icon and the count render in barContent below, so
    // the built-in text label is off; the width follows that row with the
    // same margins WidgetButton would give a label, and hasVisualContent
    // stays true now that `text` is empty.
    text: ""
    labelVisible: false
    hasVisualContent: true
    implicitWidth: Math.max(12, barContent.implicitWidth + button.scaledHorizontalMargin * 2)
    // WidgetButton paints `active` in bar.urgent, which is exactly the
    // "updates pending" badge treatment the PRD asks for.
    active: root.updateCount > 0
    useActiveColor: root.updateCount > 0
    horizontalMargin: 8.5
    // Tooltip mirrors the badge's urgency: once M2 update sweeps fill the
    // store, the actionable number (pending updates) wins over the
    // installed count.
    tooltipText: root.updateCount > 0
      ? Model.formatCount(root.updateCount, Model.strings.updatesAvailable)
      : (root.installedCount > 0
        ? Model.formatCount(root.installedCount, Model.strings.barTooltipCount)
        : Model.strings.barTooltip)

    Row {
      id: barContent
      anchors.centerIn: parent
      spacing: Style.spacing.xs

      ThemeIcon {
        name: "package"
        size: Style.bar.iconCanvas
        strokeWidth: 1.75
        color: button.active && button.useActiveColor ? button.activeColor : button.foreground
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        visible: root.installedCount > 0
        text: root.installedCount
        color: button.active && button.useActiveColor ? button.activeColor : button.foreground
        font.family: root.bar && root.bar.fontFamily ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.body
        renderType: Text.NativeRendering
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    onPressed: function(buttonCode) {
      root.togglePanel()
    }
  }

  Process {
    id: syncProc
    command: []
    stdout: StdioCollector {
      id: syncStdout
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: syncStderr
      waitForEnd: true
    }
    onStarted: Backend.markStarted(root.syncCtx)
    onExited: function(exitCode) {
      Backend.handleExit(root.syncCtx, exitCode, syncStdout.text, syncStderr.text)
    }
  }

  Timer {
    id: syncTimer
    repeat: false
    onTriggered: Backend.handleTimeout(root.syncCtx)
  }
}

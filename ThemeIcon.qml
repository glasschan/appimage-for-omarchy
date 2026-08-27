import QtQuick
import Quickshell.Io

// Tabler icon (icons/<name>.svg, MIT — see icons/LICENSE-TABLER.md) drawn
// in any theme color.
//
// Qt's SVG rasterizer paints `currentColor` as black and has no hook to
// hand it the surrounding QML color, so the bundled file is read at
// runtime (FileView, blocking — the icons are a few hundred bytes each),
// `currentColor` is swapped for `color`, and the result is fed to a plain
// Image as a utf8 data URL. The icon stays vector until rasterization (at
// `size * sourceScale`, so small bar strokes stay crisp) and re-tints
// automatically whenever the bound color changes — no shader, no cached
// per-color files, no light/dark icon twins.
//
// `strokeWidth` optionally overrides the SVG's stroke-width (tabler ships
// 2 on a 24-unit canvas); 1.5–1.75 reads better below ~20px and at display
// sizes than the default 2.
Item {
  id: root

  // Tabler icon name, i.e. icons/<name>.svg.
  property string name: ""
  // Stroke color; bind it to the theme or state color the icon must follow.
  property color color: "#000000"
  // Painted canvas size. Tabler art fills the box edge to edge, so this
  // is the visual size, not a font line box.
  property real size: 16
  // Stroke width on the SVG's 24-unit canvas; values <= 0 keep the file's.
  property real strokeWidth: 0
  // Rasterization scale over the painted size.
  property real sourceScale: 2

  implicitWidth: size
  implicitHeight: size

  FileView {
    id: file
    path: Qt.resolvedUrl("icons/" + root.name + ".svg")
    // Synchronous first read: the bar widget loads at login and must not
    // flash an empty slot while a tiny file loads.
    blockLoading: true
    watchChanges: false
    printErrors: false
  }

  // Raw SVG text (empty until the first read; `name` is constant per site
  // in practice, but the reload keeps a dynamic name correct anyway).
  property string svgText: ""

  function reload() {
    svgText = file.text()
  }

  onNameChanged: reload()
  Component.onCompleted: reload()

  readonly property string iconSource: {
    if (svgText === "") return ""
    var svg = svgText.replace(/currentColor/g, String(root.color))
    if (root.strokeWidth > 0) {
      svg = svg.replace(/stroke-width="[0-9.]+"/g, 'stroke-width="' + root.strokeWidth + '"')
    }
    return "data:image/svg+xml;utf8," + encodeURIComponent(svg)
  }

  Image {
    anchors.fill: parent
    fillMode: Image.PreserveAspectFit
    source: root.iconSource
    sourceSize.width: Math.max(1, Math.ceil(root.width * root.sourceScale))
    sourceSize.height: Math.max(1, Math.ceil(root.height * root.sourceScale))
    mipmap: true
  }
}

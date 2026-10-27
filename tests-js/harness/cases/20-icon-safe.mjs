// ---- #62 the icon display gate comes from the backend -----------------------
// The listing's additive `icon_safe` key is the only proof that
// `.icons/<base>.{png,svg}` passed the CURRENT sanitiser; the QML layer
// must keep the built-in glyph unless it is exactly true. The mapping is
// pinned here; the real backend's verdict is pinned in the Python suite.

import { Model, check } from "../core.mjs"

export function run() {
  const listed = (entry) => Model.parseListJson(JSON.stringify({
    schema_version: 1,
    installed: [Object.assign({
      name: "Icon App", desktop_id: "icon.desktop",
      path: "/x/icon.appimage", current_version: "1.0",
      running: false, running_pids: 0, exec_arguments: ""
    }, entry)]
  })).items[0]

  check("#62 icon_safe true lands on the item",
    listed({ icon_safe: true }).iconSafe === true)

  // Anything but exactly true is false — a refusal and a hostile shape
  // both cost the glyph, never the file.
  check("#62 icon_safe false lands as false",
    listed({ icon_safe: false }).iconSafe === false)
  check("#62 a non-boolean icon_safe is not trusted",
    listed({ icon_safe: "yes" }).iconSafe === false)

  // An older backend (no key) or a frozen capture shows the glyph: the
  // absence of proof is proof of nothing.
  check("#62 an older backend reports no icon proof",
    listed({}).iconSafe === false)

  // The extension-less base is still derived (the view probes
  // png then svg) — the gate decides whether the URL is ever loaded.
  const based = listed({ icon_safe: true })
  check("#62 iconBase is still derived alongside the gate",
    based.iconBase === "/x/.icons/icon" && based.iconSafe === true,
    JSON.stringify({ base: based.iconBase, safe: based.iconSafe }))
}

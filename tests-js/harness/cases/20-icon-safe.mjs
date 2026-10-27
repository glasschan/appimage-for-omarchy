// ---- #62/#63 the icon display gate comes from the backend -------------------
// The listing's additive `icon_safe` key is the only proof that
// `.icons/<base>.{png,svg}` passed the CURRENT sanitiser. Since #63 it is a
// PER-CANDIDATE verdict dict (`{"png": bool, "svg": bool}`, one key per
// existing file): a .png that fails to DECODE makes the row's Image fall
// through to the .svg, so each candidate is gated on its own verdict. The
// mapping is pinned here; the real backend's verdict is pinned in the
// Python suite.

import { readFileSync } from "node:fs"
import { Model, check } from "../core.mjs"

export function run() {
  // Pin the ENFORCING expression too: without this, deleting AppRow's
  // per-candidate gates would show unproved icons with every other check
  // green. The candidate ORDER matters as well — the panel probes .png
  // first, then .svg (the same order the backend verdicts).
  const appRowSrc = readFileSync(
    new URL("../../../AppRow.qml", import.meta.url), "utf8")
  const listed = (entry) => Model.parseListJson(JSON.stringify({
    schema_version: 1,
    installed: [Object.assign({
      name: "Icon App", desktop_id: "icon.desktop",
      path: "/x/icon.appimage", current_version: "1.0",
      running: false, running_pids: 0, exec_arguments: ""
    }, entry)]
  })).items[0]

  // A verdict dict lands on the item verbatim — AppRow reads each
  // candidate's own bool. (The entry goes through a JSON round-trip, so
  // compare by value, not identity.)
  const dict = { png: true, svg: false }
  const mapped = listed({ icon_safe: dict }).iconSafe
  check("#63 a per-candidate verdict dict lands on the item",
    mapped && mapped.png === true && mapped.svg === false,
    JSON.stringify(mapped))
  check("#63 a partial verdict dict lands as-is",
    listed({ icon_safe: { svg: true } }).iconSafe.svg === true)

  // Anything that is not that object — a legacy boolean, a hostile scalar,
  // a bare truthy — is null: no per-candidate proof, glyph, fail closed.
  check("#63 a legacy boolean icon_safe is not trusted",
    listed({ icon_safe: true }).iconSafe === null)
  check("#63 a non-object icon_safe is not trusted",
    listed({ icon_safe: "yes" }).iconSafe === null)

  // An older backend (no key) or a frozen capture shows the glyph: the
  // absence of proof is proof of nothing.
  check("#63 an older backend reports no icon proof",
    listed({}).iconSafe === null)

  // The extension-less base is still derived (the view probes
  // png then svg) — the gate decides whether the URL is ever loaded.
  const based = listed({ icon_safe: { png: true, svg: true } })
  check("#63 iconBase is still derived alongside the gate",
    based.iconBase === "/x/.icons/icon" && based.iconSafe.png === true,
    JSON.stringify({ base: based.iconBase, safe: based.iconSafe }))

  // ---- pin: the enforcing expression in AppRow.qml --------------------
  // Both failures below must FAIL the case (test_security_coverage.py
  // reads this file, so the pin itself is CI-covered).
  check("#63 AppRow gates the .png candidate on png === true",
    appRowSrc.includes("verdicts.png === true"),
    "the png === true gate is missing from AppRow.qml")
  check("#63 AppRow gates the .svg candidate on svg === true",
    appRowSrc.includes("verdicts.svg === true"),
    "the svg === true gate is missing from AppRow.qml")
  const pngAt = appRowSrc.indexOf('verdicts.png === true')
  const svgAt = appRowSrc.indexOf('verdicts.svg === true')
  check("#63 AppRow probes .png before .svg",
    pngAt !== -1 && svgAt !== -1 && pngAt < svgAt,
    JSON.stringify({ pngAt, svgAt }))
}

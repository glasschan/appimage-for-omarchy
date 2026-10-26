// ---- #32 the launch argv comes from the backend -----------------------------
// The panel used to assemble `env DESKTOPINTEGRATION=1 <path> <args>` in QML;
// the ONE launch-command builder (omarchy_appimage.launch) owns that assembly
// now and the listing reports it as the additive `launch_argv` key. The QML
// mapping is pinned here; the real backend's output is pinned in case 10.

import { Model, check } from "../core.mjs"

export function run() {
  const listed = (entry) => Model.parseListJson(JSON.stringify({
    schema_version: 1,
    installed: [Object.assign({
      name: "Launch App", desktop_id: "launch.desktop",
      path: "/x/launch.appimage", current_version: "1.0",
      running: false, running_pids: 0, exec_arguments: ""
    }, entry)]
  })).items[0]

  // The backend's argv rides the item untouched.
  const withArgv = listed({
    exec_arguments: "--no-sandbox %U",
    launch_argv: ["env", "DESKTOPINTEGRATION=1", "/x/launch.appimage",
      "--no-sandbox"] })
  check("#32 the listing's launch_argv lands on the item",
    Array.isArray(withArgv.launchArgv) && withArgv.launchArgv.length === 4
      && withArgv.launchArgv[0] === "env"
      && withArgv.launchArgv[1] === "DESKTOPINTEGRATION=1"
      && withArgv.launchArgv[2] === "/x/launch.appimage"
      && withArgv.launchArgv[3] === "--no-sandbox",
    JSON.stringify(withArgv.launchArgv))

  // An older backend (no key) is null, not an assembled fallback.
  const withoutArgv = listed({})
  check("#32 an older backend reports no launch argv",
    withoutArgv.launchArgv === null, JSON.stringify(withoutArgv.launchArgv))

  // A corrupt shape is null, never partially trusted.
  check("#32 a non-array launch_argv is not trusted",
    listed({ launch_argv: "env DESKTOPINTEGRATION=1 /x" }).launchArgv === null)

  // ---- #35 the scale-aware launch tail comes from the backend -----------
  // The panel spawns [backendCommand..., "--exec-with-scale", ...tail];
  // the tail is the listing's additive `launch_scale_argv` key. DESKTOP-
  // INTEGRATION=1 comes from the backend verb (the detached spawn cannot
  // carry env), so no env prefix belongs in the tail.
  const withTail = listed({
    exec_arguments: "--no-sandbox %U",
    launch_scale_argv: ["/x/launch.appimage", "--no-sandbox"] })
  check("#35 the listing's launch_scale_argv lands on the item",
    Array.isArray(withTail.launchScaleArgv)
      && withTail.launchScaleArgv.length === 2
      && withTail.launchScaleArgv[0] === "/x/launch.appimage"
      && withTail.launchScaleArgv[1] === "--no-sandbox",
    JSON.stringify(withTail.launchScaleArgv))

  // An older backend (no key) is null, not an assembled fallback.
  check("#35 an older backend reports no scale launch tail",
    listed({}).launchScaleArgv === null)

  // A corrupt shape is null, never partially trusted.
  check("#35 a non-array launch_scale_argv is not trusted",
    listed({ launch_scale_argv: "/x/launch.appimage" })
      .launchScaleArgv === null)

  // Both keys can coexist on one item without interfering.
  const both = listed({
    launch_argv: ["env", "DESKTOPINTEGRATION=1", "/x/launch.appimage",
      "--no-sandbox"],
    launch_scale_argv: ["/x/launch.appimage", "--no-sandbox"] })
  check("#35 launch_argv and launch_scale_argv coexist untouched",
    both.launchArgv.length === 4 && both.launchArgv[0] === "env"
      && both.launchScaleArgv.length === 2
      && both.launchScaleArgv[0] === "/x/launch.appimage",
    JSON.stringify([both.launchArgv, both.launchScaleArgv]))
}

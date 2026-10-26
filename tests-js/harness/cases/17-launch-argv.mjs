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
}

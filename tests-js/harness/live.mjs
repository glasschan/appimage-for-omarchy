// Throwaway XDG sandbox for the live-backend case: the REAL CLI runs against
// it. Built once, lazily, and removed by cleanupLive() after every case has
// reported.
import { spawnSync } from "node:child_process"
import { createHash } from "node:crypto"
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"

const MAIN_PY = new URL("../../backend/main.py", import.meta.url).pathname
// ELF magic + the AppImage type-2 magic: enough for the listing to accept
// the file (nothing is ever executed).
const FAKE_APPIMAGE = Buffer.concat([
  Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01, 0x01, 0x00, 0x41, 0x49, 0x02]),
  Buffer.from("padding")
])
// Minimal type-2 AppImage for the preview verb (--preview-integrate runs
// the ELF arch probe, so the header must be a full ELF64 header with the
// AppImage magic at offset 8). No embedded squashfs/desktop entry — exactly
// the contract's "app is null, the preview stays valid" case.
const PREVIEW_APPIMAGE = (() => {
  const header = Buffer.alloc(64)
  Buffer.from([0x7f, 0x45, 0x4c, 0x46, 0x02, 0x01, 0x01, 0x00]).copy(header, 0)
  header[8] = 0x41; header[9] = 0x49; header[10] = 0x02
  header.writeUInt16LE(0x3e, 0x12) // e_machine = x86-64
  return Buffer.concat([header, Buffer.from("no-desktop-entry")])
})()

let fixture = null

const build = () => {
  const sandbox = mkdtempSync(join(tmpdir(), "appimage-harness-"))
  const home = join(sandbox, "home")
  const dataHome = join(sandbox, "data")
  const configHome = join(sandbox, "config")
  const managed = join(home, "AppImages")
  const applications = join(dataHome, "applications")
  mkdirSync(join(managed, ".icons"), { recursive: true })
  mkdirSync(applications, { recursive: true })

  const writeApp = (name, execArgs) => {
    const appimage = join(managed, name + ".appimage")
    writeFileSync(appimage, FAKE_APPIMAGE)
    const icon = join(managed, ".icons", name + ".png")
    writeFileSync(icon, Buffer.from([0x89, 0x50, 0x4e, 0x47]))
    const args = execArgs ? " " + execArgs : ""
    writeFileSync(join(applications, name + ".desktop"),
      `[Desktop Entry]\nName=${name}\n`
      + `Exec=env DESKTOPINTEGRATION=1 ${appimage}${args}\n`
      + `TryExec=${appimage}\nIcon=${icon}\nType=Application\nTerminal=false\n`
      + `X-AppImage-Version=1.0\n`)
    return { appimage, icon }
  }

  writeApp("biscuit", "--no-sandbox %U")
  writeApp("plain", "")
  // a stale record: the .desktop file is gone, the AppImage is not
  const stale = writeApp("stale", "--no-sandbox %U")
  const staleDesktop = join(applications, "stale.desktop")
  rmSync(staleDesktop)

  const configDir = join(configHome, "io.github.glasschan.appimage")
  mkdirSync(configDir, { recursive: true })
  const appsIni = join(configDir, "apps.ini")
  writeFileSync(appsIni,
    `[app.${createHash("md5").update(stale.appimage).digest("hex")}]\n`
    + `default_exec_arguments = --no-sandbox %U\n`
    + `appimage_path = ${stale.appimage}\n`
    + `desktop_file_path = ${staleDesktop}\n`
    + `icon_path = ${stale.icon}\n`
    + `name = Stale App\n`
    + `file_path = ${stale.appimage}\n`)

  const sandboxEnv = Object.assign({}, process.env, {
    HOME: home, XDG_DATA_HOME: dataHome, XDG_CONFIG_HOME: configHome
  })
  const runBackend = (args) => spawnSync("python3", [MAIN_PY].concat(args),
    { env: sandboxEnv, encoding: "utf8" })

  const listRun = runBackend(["--list-installed", "--json"])
  const liveOk = !listRun.error

  return { sandbox, home, dataHome, appsIni, stale, staleDesktop,
    PREVIEW_APPIMAGE, runBackend, listRun, liveOk }
}

// Build once, return the same fixture object on every call.
export const ensureLive = () => fixture || (fixture = build())

// Remove the sandbox; a no-op when the live case never ran.
export const cleanupLive = () => {
  if (!fixture) return
  rmSync(fixture.sandbox, { recursive: true, force: true })
  fixture = null
}

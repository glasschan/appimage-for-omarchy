// ---- F1: --list-installed --json ------------------------------------------

import { Model, check, list } from "../core.mjs"

export function run() {
  check("list ok", list.ok === true)
  check("list count", list.items.length === 1, JSON.stringify(list.items))
  const nvim = list.items[0]
  check("list id=desktop_id", nvim.id === "neovim.desktop", nvim.id)
  check("list name", nvim.name === "Neovim", nvim.name)
  check("list version", nvim.version === "v0.11.3", nvim.version)
  check("list path", nvim.path === "/tmp/appimage-e2e/home/AppImages/neovim.appimage", nvim.path)
  check("list running", nvim.running === false)
  check("list iconBase", nvim.iconBase === "/tmp/appimage-e2e/home/AppImages/.icons/neovim", nvim.iconBase)
  check("list updateVersion null→empty", nvim.updateVersion === "")

  const empty = Model.parseListJson('{"schema_version": 1, "installed": []}')
  check("empty list ok", empty.ok === true && empty.items.length === 0)

  const garbage = Model.parseListJson("python3: not found\n")
  check("garbage rejected", garbage.ok === false && garbage.error !== "")

  // ---- runtime block on the installed-list envelope (fuse2 probe) -------------
  // CONTRACT.md (--list-installed): every successful JSON envelope carries an
  // additive `runtime` block, { fuse2: "present" | "missing" | "unknown" }.
  // The mapping is defensive: absent or non-object blocks and foreign values
  // all normalize to "unknown" (the contract's "no claim" — never an error),
  // and unknown keys anywhere are tolerated and ignored. The frozen capture
  // above predates the block, so it doubles as the absence case.
  check("runtime block absent → unknown", !!list.runtime
    && list.runtime.fuse2 === "unknown", JSON.stringify(list.runtime))
  const runtimeOf = (doc) => Model.parseListJson(doc).runtime.fuse2
  check("runtime present", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":{"fuse2":"present"}}') === "present")
  check("runtime missing", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":{"fuse2":"missing"}}') === "missing")
  check("runtime unknown passthrough", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":{"fuse2":"unknown"}}') === "unknown")
  check("runtime foreign value → unknown", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":{"fuse2":"banana"}}') === "unknown")
  check("runtime block not an object → unknown", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":"present"}') === "unknown")
  check("runtime block null → unknown", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":null}') === "unknown")
  check("runtime array form → unknown", runtimeOf(
    '{"schema_version":1,"installed":[],"runtime":["present"]}') === "unknown")
  check("runtime unknown keys tolerated", runtimeOf(
    '{"schema_version":1,"installed":[],'
    + '"runtime":{"fuse2":"missing","probe":"ldconfig","extra":[1,2]}}') === "missing")
  check("envelope unknown keys tolerated", runtimeOf(
    '{"schema_version":1,"installed":[],'
    + '"runtime":{"fuse2":"present"},"future_key":{"x":1}}') === "present")
  check("bare array envelope → runtime unknown", runtimeOf('[{"name":"X"}]') === "unknown")

  // The store keeps the latest listing's verdict, from every listing surface.
  // A listing without the block (older backend) resets it, so a stale
  // "missing" can never survive into a hint.
  Model.setItems(list.items, Model.parseListJson(
    '{"schema_version":1,"installed":[],"runtime":{"fuse2":"missing"}}').runtime)
  check("setItems stores runtime", Model.state.runtime.fuse2 === "missing")
  Model.setItems(list.items, list.runtime)
  check("setItems keeps unknown runtime", Model.state.runtime.fuse2 === "unknown")

  // ---- launch-failure banner (fuse2 hint) --------------------------------------
  // A launch is only known to have failed when the post-launch settle listing
  // says the app never came up (execDetached cannot report the AppImage
  // runtime's own death — a type-2 AppImage without fuse2 exits right after
  // spawn). Model.launchFailureBanner is that banner's pure text mapping:
  // the fuse2 hint line is appended ONLY for fuse2 "missing" — present,
  // unknown and any foreign state keep the banner about the failure alone.
  // Text only: the hint names the library, never a command, never a button
  // (repo wording rule; the security gates scan install-command sources).
  const failedName = "Neovim (v0.11.3)"
  const bannerBase = Model.launchFailureBanner(failedName, "present")
  check("banner strings present", !!Model.strings.launchFailed && !!Model.strings.launchFuse2Hint)
  check("banner base line names the app", bannerBase.indexOf(failedName) !== -1
    && bannerBase.indexOf("\n") === -1, JSON.stringify(bannerBase))
  for (const quiet of ["present", "unknown", "banana", "", null, undefined]) {
    const base = Model.launchFailureBanner(failedName, quiet)
    check("banner no hint on " + String(quiet),
      base === bannerBase && base.indexOf("fuse2") === -1, JSON.stringify(base))
  }
  const hinted = Model.launchFailureBanner(failedName, "missing")
  check("banner hint line on missing", hinted.split("\n").length === 2
    && hinted.split("\n")[0] === bannerBase
    && hinted.split("\n")[1].indexOf("fuse2") !== -1
    && hinted.split("\n")[1].indexOf("libfuse.so.2") !== -1, JSON.stringify(hinted))
  check("banner is text only", hinted.indexOf("/") === -1 && hinted.indexOf("`") === -1
    && hinted.indexOf("$") === -1 && hinted.indexOf("=") === -1, JSON.stringify(hinted))
  check("banner tolerates an empty name", Model.launchFailureBanner("", "missing")
    .split("\n")[0] === Model.formatCount("", Model.strings.launchFailed))
}

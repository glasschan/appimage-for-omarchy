// ---- F6: --list-updates --json ------------------------------------------------
// Real capture: a custom StaticFileUpdater app (static sources carry no
// version) + the real neovim AppImage whose embedded gh-releases-zsync
// source was checked against the live GitHub API (values frozen at capture
// time, like every capture here).

import { Model, cap, check, updates } from "../core.mjs"

export function run() {
  const updates = Model.parseUpdatesJson(cap("updates.json"))
  check("updates ok", updates.ok === true)
  check("updates count", updates.updates.length === 2, JSON.stringify(updates.updates))
  const embeddedUpd = updates.updates.find((u) => u.embeddedSource === true)
  const staticUpd = updates.updates.find((u) => u.embeddedSource === false)
  check("updates found both kinds", !!embeddedUpd && !!staticUpd)
  check("updates id mapping", embeddedUpd.id === "neovim.desktop"
    && staticUpd.id === "secondapp.desktop")
  check("updates version mapping", embeddedUpd.updateVersion === "v0.12.5"
    && staticUpd.updateVersion === "")
  check("updates manager mapping", embeddedUpd.manager === "GithubUpdater"
    && staticUpd.manager === "StaticFileUpdater")
  check("updates downloadSize mapping", embeddedUpd.downloadSize === 11442680
    && staticUpd.downloadSize === 10996235)
  check("updates embedded update triple", embeddedUpd.updateVersion !== ""
    && embeddedUpd.manager !== "" && embeddedUpd.embeddedSource === true
    && embeddedUpd.downloadSize > 0)
  // notified/offline only exist on --fetch-updates; --list-updates defaults false.
  check("updates flags default", updates.offline === false && updates.notified === false)

  // M1 items still normalize; the new keys default to their empty values.
  const listed = Model.parseListJson(cap("list.json"))
  check("list update keys default", listed.items[0].manager === ""
    && listed.items[0].embeddedSource === false && listed.items[0].downloadSize === 0)

  // ---- F8: --fetch-updates --json -----------------------------------------------
  const fetched = Model.parseUpdatesJson(cap("fetch-updates.json"))
  check("fetch ok", fetched.ok === true && fetched.updates.length === 2)
  check("fetch flags", fetched.notified === true && fetched.offline === false)
  // offline passthrough stays covered by an inline literal (the real capture
  // was taken online; offline capture files would freeze a port-dependent URL).
  const offline = Model.parseUpdatesJson('{"schema_version":1,"updates":[],"notified":false,"offline":true}')
  check("fetch offline flagged", offline.ok === true && offline.offline === true
    && offline.updates.length === 0)

  // applyUpdates merge: the sweep result is authoritative for the apps it
  // reports — every reported app gains hasUpdate (the badge must tint even
  // for versionless static-source updates, which the old updateVersion-based
  // count missed). Sweep metadata is also fresher than the list cache, so
  // manager/embeddedSource/downloadSize refresh on reported apps.
  //
  // A sweep NEVER clears an app it does not report (issue #50): the
  // response lists only the apps with a pending release, so an app whose
  // check failed is simply absent while the backend keeps that app's last
  // known verdict in its durable record. The listing is what clears a
  // verdict (pending_update false).
  // The store is seeded with copies: in the real flow --list-installed and
  // the sweep parse into separate objects, and the freshness check below
  // must not mutate the sweep doc it merges.
  Model.setItems(updates.updates.map((u) => Object.assign({}, u)))
  check("items seeded hasUpdate false", Model.findItem("neovim.desktop").hasUpdate === false
    && Model.findItem("secondapp.desktop").hasUpdate === false)
  check("updateCount seeded", Model.updateCount() === 0,
    "parsed items carry no update state until a sweep is applied")
  // Stale list-cache data on a reported app is overwritten by the sweep doc.
  Model.findItem("neovim.desktop").manager = "SomethingStale"
  Model.findItem("neovim.desktop").embeddedSource = false
  Model.findItem("neovim.desktop").downloadSize = 0
  Model.applyUpdates(updates.updates)
  check("applyUpdates hasUpdate both kinds", Model.findItem("neovim.desktop").hasUpdate === true
    && Model.findItem("secondapp.desktop").hasUpdate === true)
  check("updateCount counts versionless static update", Model.updateCount() === 2,
    "static secondapp.desktop has updateVersion \"\" but a real pending update")
  check("applyUpdates refreshes stale source metadata", Model.findItem("neovim.desktop").manager === "GithubUpdater"
    && Model.findItem("neovim.desktop").embeddedSource === true
    && Model.findItem("neovim.desktop").downloadSize === 11442680)
  check("applyUpdates version marker stays version-only", Model.findItem("secondapp.desktop").updateVersion === "")
  Model.applyUpdates([embeddedUpd])
  check("applyUpdates present id kept", Model.findItem("neovim.desktop").updateVersion === "v0.12.5"
    && Model.findItem("neovim.desktop").manager === "GithubUpdater"
    && Model.findItem("neovim.desktop").hasUpdate === true)
  check("applyUpdates absent id keeps its verdict", Model.findItem("secondapp.desktop").hasUpdate === true
    && Model.findItem("secondapp.desktop").updateVersion === "",
    "an app the sweep does not report keeps the verdict the backend's record holds")
  check("applyUpdates absent id keeps manager", Model.findItem("secondapp.desktop").manager === "StaticFileUpdater")
  check("updateCount after merge", Model.updateCount() === 2)
  Model.applyUpdates([])
  check("applyUpdates empty sweep clears nothing", Model.updateCount() === 2
    && Model.findItem("neovim.desktop").hasUpdate === true
    && Model.findItem("neovim.desktop").updateVersion === "v0.12.5",
    "an empty sweep reports no app, so it retires no verdict")
  check("applyUpdates empty sweep keeps manager", Model.findItem("neovim.desktop").manager === "GithubUpdater")
}

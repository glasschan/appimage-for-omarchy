// Job-document fixtures. Captures in tests-js/captures/job-*.json are REAL
// backend documents, and classification time is pinned to each capture's own
// heartbeat (+offset seconds), so the decision table is deterministic no
// matter when the harness runs.
import { JobDocs, cap } from "./core.mjs"

export const jobDoc = (f) => JobDocs.parseJobDocument(cap(f)).doc
export const at = (f, offsetSec) => jobDoc(f).heartbeat * 1000 + offsetSec * 1000
export const adopt = (f, pidAlive, offsetSec) =>
  JobDocs.adoptDocument(cap(f), pidAlive, at(f, offsetSec === undefined ? 5 : offsetSec))

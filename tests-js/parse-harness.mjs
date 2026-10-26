// Harness: feeds real backend/main.py output (captured in tests-js/captures/
// during an e2e run) through the exact QML-side JS parse logic
// (lib/Model.js + lib/Backend.js).
// Run: node tests-js/parse-harness.mjs
//
// Checks live in tests-js/harness/cases/*.mjs (one module per document
// family, imported and run in order); the shared plumbing they build on is
// tests-js/harness/{core,live,jobs,fakes}.mjs.
import { failureCount } from "./harness/core.mjs"
import { cleanupLive } from "./harness/live.mjs"
import * as c01 from "./harness/cases/01-installed-list.mjs"
import * as c02 from "./harness/cases/02-integrate-preview.mjs"
import * as c03 from "./harness/cases/03-backend-store.mjs"
import * as c04 from "./harness/cases/04-updates.mjs"
import * as c05 from "./harness/cases/05-settings.mjs"
import * as c06 from "./harness/cases/06-pick-drop.mjs"
import * as c07 from "./harness/cases/07-error-mapping.mjs"
import * as c08 from "./harness/cases/08-producer-bound.mjs"
import * as c09 from "./harness/cases/09-python-card.mjs"
import * as c10 from "./harness/cases/10-live-backend.mjs"
import * as c11 from "./harness/cases/11-job-documents.mjs"
import * as c12 from "./harness/cases/12-force-quit.mjs"
import * as c13 from "./harness/cases/13-panel-reader.mjs"
import * as c14 from "./harness/cases/14-runtime-flags.mjs"
import * as c15 from "./harness/cases/15-verdicts.mjs"
import * as c16 from "./harness/cases/16-errors.mjs"
import * as c17 from "./harness/cases/17-launch-argv.mjs"

const cases = [c01, c02, c03, c04, c05, c06, c07, c08, c09, c10, c11, c12, c13, c14, c15, c16, c17]
for (const c of cases) c.run()

cleanupLive()

const failures = failureCount()
console.log(failures === 0 ? "\nALL PASS" : "\n" + failures + " FAILURES")
process.exit(failures === 0 ? 0 : 1)

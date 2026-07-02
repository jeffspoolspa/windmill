//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"

// PERMANENT AD-HOC ION RUNNER (inert baseline).
//
// Usage: override main() with a one-off body (keep the bun-extra-requirements header), deploy via
// createScript with parent_hash (versions in place), run via runScriptByPath (NO args -- hardcode
// params in the body), fetch the result via getJob. Reset to this baseline when done.
// Gotchas: runScriptByPath can resolve the PREVIOUS hash right after createScript -- check
// script_hash in getJob and re-run if stale. Chromium tag required for anything using the ION session.
export async function main() {
  return { noop: true, note: "adhoc runner is at its inert baseline -- override main() for one-off ION jobs" }
}

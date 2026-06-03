//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// No-arg background runner for the canonical log-based re-ingest of May 2026.
// Loops weekly windows and calls f/ION/ingest_day_logs with dry_run=false for each.
// Each window is its own scoped transactional replace, so completed weeks persist
// even if a later week fails -> safely resumable by editing the `chunks` list.

import "playwright@1.40.0"
import { main as ingest } from "/f/ION/ingest_day_logs"

export async function main() {
  const chunks: [string, string][] = [
    ["05/01/2026", "05/07/2026"],
    ["05/08/2026", "05/14/2026"],
    ["05/15/2026", "05/21/2026"],
    ["05/22/2026", "05/28/2026"],
    ["05/29/2026", "05/31/2026"],
  ]
  const out: any[] = []
  for (const [s, e] of chunks) {
    console.log(`[ingest_may] window ${s}..${e} starting`)
    const r = await ingest(s, e, false)
    console.log(`[ingest_may] window ${s}..${e} done: built=${r.logs_built} inserted=${r.insertedVisits} deleted=${r.deletedVisits} unresolved=${r.unresolved_count}`)
    out.push({ window: `${s}..${e}`, logs_built: r.logs_built, resolved: r.resolved_to_task, inserted: r.insertedVisits, deletedVisits: r.deletedVisits, deletedCons: r.deletedCons, insertedCons: r.insertedCons, skipped: r.skipped, unresolved: r.unresolved_count, unresolved_events: r.unresolved_events })
  }
  return out
}

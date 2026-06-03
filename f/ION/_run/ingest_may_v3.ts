//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

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
    console.log(`[ingest_may_v3] ${s}..${e} starting`)
    const r = await ingest(s, e, false)
    console.log(`[ingest_may_v3] ${s}..${e} done: built=${r.logs_built} inserted=${r.insertedVisits} deleted=${r.deletedVisits} serviceable=${r.serviceable} unresolved=${r.unresolved_count}`)
    out.push({ window: `${s}..${e}`, logs_built: r.logs_built, resolved: r.resolved_to_task, inserted: r.insertedVisits, serviceable: r.serviceable, deletedVisits: r.deletedVisits, insertedCons: r.insertedCons, skipped: r.skipped, unresolved: r.unresolved_count })
  }
  return out
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// Yearly visit backfill in WEEKLY chunks. Each week is one ingest_day_logs call =
// one DB transaction (committed independently). Per-week failures are caught and
// recorded, not fatal -- and because ingest upserts on ion_log_id, re-running the
// whole thing (or a failed week) is idempotent. Default range: 2025-01-01 -> today.

import "playwright@1.40.0"
import { main as ingestDayLogs } from "/f/ION/ingest_day_logs"

function pad(n: number) { return String(n).padStart(2, "0") }
function mdy(d: Date) { return `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}/${d.getUTCFullYear()}` }
function parseMdy(s: string) { const [m, d, y] = s.split("/").map(Number); return new Date(Date.UTC(y, m - 1, d)) }

export async function main(start_date: string = "01/01/2025", end_date: string = "", dry_run: boolean = false) {
  const start = parseMdy(start_date)
  const endT = end_date ? parseMdy(end_date).getTime() : Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate())
  const weeks: [string, string][] = []
  let cur = start.getTime()
  while (cur <= endT) { const wkEndT = Math.min(cur + 6 * 86400000, endT); weeks.push([mdy(new Date(cur)), mdy(new Date(wkEndT))]); cur += 7 * 86400000 }

  const per_week: any[] = []
  let totV = 0, totR = 0, totK = 0, totC = 0, totU = 0, failed = 0
  for (const [ws, we] of weeks) {
    try {
      const r: any = await ingestDayLogs(ws, we, dry_run)
      const visits = r.insVisits ?? r.logs_built ?? 0
      per_week.push({ week: `${ws}..${we}`, visits, readings: r.insReadings ?? r.readings_rows ?? 0, checklist: r.insChecklist ?? r.checklist_rows ?? 0, consumables: r.insConsumables ?? r.consumable_rows ?? 0, unlinked: r.unlinked_visits ?? 0 })
      totV += visits; totR += (r.insReadings ?? r.readings_rows ?? 0); totK += (r.insChecklist ?? r.checklist_rows ?? 0); totC += (r.insConsumables ?? r.consumable_rows ?? 0); totU += (r.unlinked_visits ?? 0)
    } catch (e: any) { failed++; per_week.push({ week: `${ws}..${we}`, error: String(e?.message ?? e).slice(0, 160) }) }
  }
  return { range: { start: start_date, end: end_date || mdy(new Date(endT)) }, weeks: weeks.length, dry_run, failed_weeks: failed, totals: { visits: totV, readings: totR, checklist: totK, consumables: totC, unlinked: totU }, per_week }
}

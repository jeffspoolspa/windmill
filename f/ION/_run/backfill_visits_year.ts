//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// Yearly visit backfill in WEEKLY chunks, NEWEST WEEK FIRST (so the current month lands in
// minutes and older history fills in behind it). Reads ION creds + logs in ONCE, holds the
// session, RE-LOGINS from the held creds when it ages out -- no mid-run f/ION variable reads.
// Each week is one ingest_day_logs call = one DB transaction; per-week failures are caught and
// logged, and ingest upserts on ion_log_id so the whole thing is idempotent / safe to re-run.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { loginToIon, isSessionFresh } from "/f/ION/_lib/session"
import { main as ingestDayLogs } from "/f/ION/ingest_day_logs"

function pad(n: number) { return String(n).padStart(2, "0") }
function mdy(d: Date) { return `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}/${d.getUTCFullYear()}` }
function parseMdy(s: string) { const [m, d, y] = s.split("/").map(Number); return new Date(Date.UTC(y, m - 1, d)) }

export async function main(start_date: string = "01/01/2025", end_date: string = "", dry_run: boolean = false) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  let session = await loginToIon(ion)
  let logins = 1

  const start = parseMdy(start_date)
  const endT = end_date ? parseMdy(end_date).getTime() : Date.UTC(new Date().getUTCFullYear(), new Date().getUTCMonth(), new Date().getUTCDate())
  const weeks: [string, string][] = []
  let cur = start.getTime()
  while (cur <= endT) { const e = Math.min(cur + 6 * 86400000, endT); weeks.push([mdy(new Date(cur)), mdy(new Date(e))]); cur += 7 * 86400000 }
  weeks.reverse() // NEWEST WEEK FIRST -- current month commits first
  console.log(`backfill ${start_date}..${end_date || mdy(new Date(endT))} = ${weeks.length} weeks, NEWEST-FIRST, dry_run=${dry_run}`)

  const per_week: any[] = []
  let totV = 0, totR = 0, totK = 0, totC = 0, totU = 0, failed = 0
  for (let i = 0; i < weeks.length; i++) {
    const [ws, we] = weeks[i]
    try {
      if (!isSessionFresh(session)) { session = await loginToIon(ion); logins++ }
      const r: any = await ingestDayLogs(ws, we, dry_run, session)
      const v = r.insVisits ?? r.logs_built ?? 0
      const rd = r.insReadings ?? r.readings_rows ?? 0, ck = r.insChecklist ?? r.checklist_rows ?? 0
      const cn = r.insConsumables ?? r.consumable_rows ?? 0, ul = r.unlinked_visits ?? 0
      totV += v; totR += rd; totK += ck; totC += cn; totU += ul
      per_week.push({ week: `${ws}..${we}`, visits: v, readings: rd, checklist: ck, consumables: cn, unlinked: ul })
      console.log(`[${i + 1}/${weeks.length}] ${ws}..${we}: visits=${v} readings=${rd} checklist=${ck} cons=${cn} unlinked=${ul} | cum visits=${totV}`)
    } catch (e: any) {
      failed++
      const msg = String(e?.message ?? e).slice(0, 160)
      per_week.push({ week: `${ws}..${we}`, error: msg })
      console.log(`[${i + 1}/${weeks.length}] ${ws}..${we}: ERROR ${msg}`)
    }
  }
  return { range: { start: start_date, end: end_date || mdy(new Date(endT)) }, weeks: weeks.length, order: "newest-first", logins, dry_run, failed_weeks: failed, totals: { visits: totV, readings: totR, checklist: totK, consumables: totC, unlinked: totU }, per_week }
}

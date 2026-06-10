//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// GAP-AWARE visit backfill. Each run queries which days in [gap_start,gap_end] already have visits,
// builds weekly chunks, KEEPS ONLY weeks with ZERO visits (so nothing already pulled is re-fetched),
// ingests up to max_weeks of them (oldest-first), then exits well under Windmill's hard 90-min job cap.
// Idempotent (ingest upserts on ion_log_id) + self-resuming -> safe on a cron until the gap is full,
// after which it no-ops (returns before logging into ION). Reads creds/resource ONCE, re-logins from
// held creds when the session ages out (no mid-run wmill reads -- those degrade ~15 min into a job).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { loginToIon, isSessionFresh } from "/f/ION/_lib/session"
import { main as ingestDayLogs } from "/f/ION/ingest_day_logs"

function pad(n: number) { return String(n).padStart(2, "0") }
function mdy(d: Date) { return `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}/${d.getUTCFullYear()}` }
function iso(d: Date) { return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}` }
function parseMdy(s: string) { const [m, d, y] = s.split("/").map(Number); return new Date(Date.UTC(y, m - 1, d)) }

export async function main(gap_start: string = "01/29/2025", gap_end: string = "11/11/2025", max_weeks: number = 16, dry_run: boolean = false) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const sb: any = await wmill.getResource("u/carter/supabase")

  // Which days in the gap window already have visits? (so we never re-pull a filled week.)
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user, password: sb.password, ssl: "require", max: 2 })
  const existing = new Set<string>()
  try {
    const rows = await sql<any[]>`SELECT DISTINCT scheduled_date::text d FROM maintenance.visits
      WHERE scheduled_date BETWEEN ${iso(parseMdy(gap_start))} AND ${iso(parseMdy(gap_end))}`
    for (const r of rows) existing.add(r.d)
  } finally { await sql.end() }

  const start = parseMdy(gap_start), endT = parseMdy(gap_end).getTime()
  const allWeeks: [Date, Date][] = []
  let c = start.getTime()
  while (c <= endT) { const e = Math.min(c + 6 * 86400000, endT); allWeeks.push([new Date(c), new Date(e)]); c += 7 * 86400000 }
  const weekFilled = (w: [Date, Date]) => { let t = w[0].getTime(); const end = w[1].getTime(); while (t <= end) { if (existing.has(iso(new Date(t)))) return true; t += 86400000 } return false }
  const remaining = allWeeks.filter((w) => !weekFilled(w)) // oldest-first
  const todo = remaining.slice(0, max_weeks)

  console.log(`GAP ${gap_start}..${gap_end}: ${allWeeks.length} weeks, ${remaining.length} still empty, processing ${todo.length} (max ${max_weeks}), dry_run=${dry_run}`)
  if (todo.length === 0) { console.log("GAP FILLED -- nothing to do"); return { done: true, weeks_total: allWeeks.length, remaining: 0, processed: 0 } }

  let session = await loginToIon(ion); let logins = 1
  const per_week: any[] = []; let totV = 0, failed = 0
  for (let i = 0; i < todo.length; i++) {
    const ws = mdy(todo[i][0]), we = mdy(todo[i][1])
    try {
      if (!isSessionFresh(session)) { session = await loginToIon(ion); logins++ }
      const r: any = await ingestDayLogs(ws, we, dry_run, session, sb)
      const v = r.insVisits ?? r.logs_built ?? 0; totV += v
      per_week.push({ week: `${ws}..${we}`, visits: v, unlinked: r.unlinked_visits ?? 0 })
      console.log(`[${i + 1}/${todo.length}] ${ws}..${we}: visits=${v} unlinked=${r.unlinked_visits ?? 0} | cum ${totV}`)
    } catch (e: any) {
      failed++; const msg = String(e?.message ?? e).slice(0, 160)
      per_week.push({ week: `${ws}..${we}`, error: msg })
      console.log(`[${i + 1}/${todo.length}] ${ws}..${we}: ERROR ${msg}`)
    }
  }
  return { done: remaining.length <= todo.length, gap: { start: gap_start, end: gap_end }, weeks_total: allWeeks.length, remaining_before: remaining.length, processed: todo.length, failed, logins, dry_run, totals: { visits: totV }, per_week }
}

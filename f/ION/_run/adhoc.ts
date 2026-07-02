//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { main as getLogDetail } from "/f/ION/api/get_log_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: re-scrape BARTH (task 5978326) June visits and refresh consumables_usage -- catches the
// HALF HOUR MAINTENANCE + CAL HYPO added to log 37214371 on the 29th (outside the date-window lookback).
const DRY_RUN = false
const TASKS = ["5978326"]

export async function main() {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 3, prepare: false })
  try {
    const visits = await sql<any[]>`
      select v.id as visit_id, v.ion_log_id, v.ion_calendar_id, v.ion_task_id, v.visit_date
      from maintenance.visits v
      where v.ion_task_id = any(${TASKS}) and v.visit_date >= '2026-06-01' and v.visit_date < '2026-07-01' and v.ion_log_id is not null
      order by v.visit_date`
    const before = await sql<any[]>`select cu.visit_id, count(*)::int as n from maintenance.consumables_usage cu where cu.visit_id = any(${visits.map(v => v.visit_id)}) group by cu.visit_id`
    const beforeMap: Record<string, number> = {}; for (const b of before) beforeMap[b.visit_id] = b.n

    const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
    const sess = await getOrRefreshSession(ion)
    const det: any = await getLogDetail(visits.map(v => ({ log_id: v.ion_log_id, calendar_id: v.ion_calendar_id })), sess)
    const byLog: Record<string, any> = {}; for (const d of (det.details || [])) byLog[d.log_id] = d

    const changes: any[] = []; let refreshed = 0
    for (const v of visits) {
      const d = byLog[v.ion_log_id]; if (!d) continue
      const cons = d.consumables || []
      const wasN = beforeMap[v.visit_id] || 0
      if (cons.length !== wasN) changes.push({ date: String(v.visit_date).slice(0, 10), log: v.ion_log_id, before: wasN, after: cons.length, items: cons.map((c: any) => `${c.name} x${c.quantity}`) })
      if (!DRY_RUN) {
        await sql.begin(async (tx: any) => {
          await tx`delete from maintenance.consumables_usage where visit_id=${v.visit_id}`
          for (const c of cons) await tx`insert into maintenance.consumables_usage (visit_id, ion_item_id, item_name, quantity, source, recorded_at) values (${v.visit_id}, ${c.ion_item_id}, ${c.name}, ${c.quantity}, 'ion', now())`
        })
        refreshed++
      }
    }
    return { dry_run: DRY_RUN, visits: visits.length, refreshed, changes }
  } finally { await sql.end().catch(() => {}) }
}

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
// CURRENT: re-scrape get_log_detail for the June visits of the reconcile-diff tasks and refresh
// their consumables_usage (chems may have been added in ION after our nightly ingest ran).
// Same DELETE+reinsert per visit as f/ION/ingest_day_logs. DRY_RUN=true previews additions only.
const DRY_RUN = true
const TASKS = ["5764078","5764017","5210399","5139937","5723168","5617095","5233998","5764072",
  "5723141","5973386","5111205","4076559","4225225","5381779","5958693","5937721","5939498","5943034"]

export async function main() {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 3, prepare: false })
  try {
    const visits = await sql<any[]>`
      select v.id as visit_id, v.ion_log_id, v.ion_calendar_id, v.ion_task_id, v.visit_date
      from maintenance.visits v
      where v.ion_task_id = any(${TASKS}) and v.visit_date >= '2026-06-01' and v.visit_date < '2026-07-01'
        and v.ion_log_id is not null
      order by v.ion_task_id, v.visit_date`
    // existing consumable row-count per visit (before)
    const before = await sql<any[]>`
      select cu.visit_id, count(*)::int as n
      from maintenance.consumables_usage cu
      where cu.visit_id = any(${visits.map(v => v.visit_id)})
      group by cu.visit_id`
    const beforeMap: Record<string, number> = {}
    for (const b of before) beforeMap[b.visit_id] = b.n

    const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
    const sess = await getOrRefreshSession(ion)

    // scrape log detail in chunks
    const chunks: any[][] = []
    for (let i = 0; i < visits.length; i += 25) chunks.push(visits.slice(i, i + 25))
    const byLog: Record<string, any> = {}
    for (const ch of chunks) {
      const det: any = await getLogDetail(ch.map(v => ({ log_id: v.ion_log_id, calendar_id: v.ion_calendar_id })), sess)
      for (const d of (det.details || [])) byLog[d.log_id] = d
    }

    const perTask: Record<string, any> = {}
    let refreshed = 0, addedRows = 0, missedScrape = 0
    for (const v of visits) {
      const key = v.ion_task_id
      perTask[key] = perTask[key] || { ion_task_id: key, visits: 0, rows_before: 0, rows_after: 0, changes: [] }
      perTask[key].visits++
      perTask[key].rows_before += (beforeMap[v.visit_id] || 0)
      const d = byLog[v.ion_log_id]
      if (!d) { missedScrape++; perTask[key].rows_after += (beforeMap[v.visit_id] || 0); continue } // no detail -> don't touch
      const cons = d.consumables || []
      perTask[key].rows_after += cons.length
      const wasN = beforeMap[v.visit_id] || 0
      if (cons.length !== wasN) {
        perTask[key].changes.push({ date: String(v.visit_date).slice(0, 10), before: wasN, after: cons.length,
          items: cons.map((c: any) => `${c.name} x${c.quantity}`) })
      }
      if (!DRY_RUN) {
        await sql.begin(async (tx: any) => {
          await tx`delete from maintenance.consumables_usage where visit_id=${v.visit_id}`
          for (const c of cons) {
            await tx`insert into maintenance.consumables_usage (visit_id, ion_item_id, item_name, quantity, source, recorded_at)
                     values (${v.visit_id}, ${c.ion_item_id}, ${c.name}, ${c.quantity}, 'ion', now())`
          }
        })
        refreshed++
      }
      if (cons.length > wasN) addedRows += (cons.length - wasN)
    }
    return { dry_run: DRY_RUN, tasks: TASKS.length, visits: visits.length, refreshed, missed_scrape: missedScrape,
      net_rows_added: addedRows, per_task: Object.values(perTask).filter((p: any) => p.changes.length) }
  } finally {
    await sql.end().catch(() => {})
  }
}

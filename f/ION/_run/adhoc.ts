//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. CURRENT: the schedule sync adds day rows but never PRUNES days ION
// dropped. Reconcile the 3 affected active tasks against ION's live day1-7 roster: DELETE schedule
// rows whose day_of_week is not in the live roster (deletion audited via task_schedules_audit; the
// freq trigger then recalcs tasks.frequency/days_per_week automatically).
const TASKS: [string, string, string][] = [
  ["5664059","1124217","ALTMAN"],
  ["5333849","2367390","WINDING RIVER COMMUNITY"],
  ["5870352","1983152","ZEH"],
]

export async function main() {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 2, prepare: false })
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const results: any[] = []
  try {
    for (const [eid, cid, name] of TASKS) {
      const { detail }: any = await getTaskDetail(s, eid, cid)
      const liveDays = (detail.perDayTech || []).map((d: any) => d.dow)
      const del = await sql`
        delete from maintenance.task_schedules
        where ion_task_id=${eid} and active and not (day_of_week = any(${liveDays}))
        returning day_of_week`
      const now = await sql`select frequency, days_per_week from maintenance.tasks where ion_task_id=${eid}`
      results.push({ name, eid, ion_live_days: liveDays, deleted_days: del.map((r: any) => r.day_of_week),
        frequency_now: now[0]?.frequency, days_per_week_now: now[0]?.days_per_week })
    }
    return { results }
  } finally { await sql.end().catch(() => {}) }
}

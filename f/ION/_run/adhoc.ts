//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: 10 legacy closed recurring tasks predate the schedule sync (no task_schedules rows, no
// recurrence text) -> frequency null. Pull each task's edit form from ION (works for closed tasks)
// and insert its day1-7 roster into task_schedules (active=false; closed). The tasks_freq trigger
// then sets tasks.frequency automatically. Also stamp external_data.recurrence as fallback.
const TASKS: [string, string, string, string][] = [
  ["bcca9551-eb45-4364-a25c-61aeaef9de64","1516689","1127440","RETTSTADT"],
  ["ba352af7-8f96-4e94-a74e-7f4b59b3a7b5","4631854","2334054","ELLER"],
  ["4d1e4e7c-1476-40ce-89a6-48d53c985447","4760212","1128388","WEIR"],
  ["2107afba-1870-469a-aa31-7c4d7655d355","5161529","2020016","ENGLISH"],
  ["3377d2f1-aab8-47a8-8483-c65ce6c74224","5210360","2439587","Bremer"],
  ["0bd157f3-cd6a-4021-a501-973875f87258","5210572","2439470","Tucker"],
  ["4da8bdbf-f3ac-4a89-a13f-7349f4d62ced","5234210","2414173","HESSENAUER"],
  ["0811b81e-2c7b-46ee-9692-8ec56954d0f3","5479347","2503094","SHROPSHIRE"],
  ["a488817b-e796-49f1-a74f-caf14839b295","5878442","1127556","ROWAN"],
  ["ff778399-b561-4011-aa8d-20dfba4eec98","5940937","2107750","CARTER"],
]
const mapFreq = (r: string) => {
  r = (r || "").trim().toLowerCase().replace(/-/g, "")
  return r === "weekly" ? "weekly" : r === "biweekly" ? "biweekly_a" : r === "daily" ? "daily" : r === "monthly" ? "monthly" : null
}

export async function main() {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 2, prepare: false })
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const results: any[] = []
  try {
    for (const [taskId, eid, cid, name] of TASKS) {
      try {
        const { detail }: any = await getTaskDetail(s, eid, cid)
        const repeat = detail.serviceRepeat?.text || ""
        const days = (detail.perDayTech || [])
        const freq = mapFreq(repeat)
        let inserted = 0
        for (const d of days) {
          const r = await sql`
            insert into maintenance.task_schedules (task_id, ion_task_id, day_of_week, frequency, active, starts_on, ends_on, external_source)
            select ${taskId}, ${eid}, ${d.dow}, ${freq}, false, t.starts_on, t.ends_on, 'ion_backfill'
            from maintenance.tasks t where t.id=${taskId}
              and not exists (select 1 from maintenance.task_schedules x where x.task_id=${taskId} and x.day_of_week=${d.dow})
            returning id`
          inserted += r.count
        }
        // stamp recurrence text as fallback (covers empty day roster)
        await sql`update maintenance.tasks set external_data = external_data || jsonb_build_object('recurrence', ${repeat}::text) where id=${taskId} and ${repeat}::text <> ''`
        // if no schedule rows were inserted, trigger never fired -> recalc via the fallback
        await sql`select maintenance.recalc_task_frequency(${taskId}::uuid)`
        const now = await sql`select frequency, days_per_week from maintenance.tasks where id=${taskId}`
        results.push({ name, eid, repeat, roster_days: days.map((d: any) => d.dayName), inserted, frequency_now: now[0]?.frequency, days_per_week: now[0]?.days_per_week })
      } catch (e: any) {
        results.push({ name, eid, error: String(e?.message ?? e).slice(0, 140) })
      }
    }
    return { results }
  } finally { await sql.end().catch(() => {}) }
}

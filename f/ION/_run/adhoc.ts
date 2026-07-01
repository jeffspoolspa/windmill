//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { main as listDayLogs } from "/f/ION/api/list_day_logs"
import { main as getLogDetail } from "/f/ION/api/get_log_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: scan SUGARMILL's June service days in ION, dump EVERY log for event 5139937 (incl.
// logs with no time_in that the ingester skips), with consumables -- locate the 8 liquid chlorine
// and see which log carries them + whether that log was ingestible. Uses fetch APIs (no browser dep).
const EVENT = "5139937"
const DAYS = ["06/01/2026","06/03/2026","06/05/2026","06/08/2026","06/10/2026","06/12/2026","06/15/2026",
  "06/17/2026","06/19/2026","06/22/2026","06/24/2026","06/26/2026","06/29/2026"]

export async function main() {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 2, prepare: false })
  const haveLogs: Set<string> = new Set(
    (await sql<any[]>`select ion_log_id from maintenance.visits where ion_task_id=${EVENT} and visit_date>='2026-06-01' and visit_date<'2026-07-01'`)
      .map((r) => String(r.ion_log_id)))
  await sql.end().catch(() => {})

  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const sess = await getOrRefreshSession(ion)

  const logs: any[] = []
  let chlorineUnits = 0
  for (const day of DAYS) {
    const enr: any = await listDayLogs(day, 0, sess)
    const dayLogs = (enr.logs ?? [])
    const det: any = await getLogDetail(dayLogs.map((l: any) => ({ log_id: l.log_id, calendar_id: l.calendar_id })), sess)
    for (const d of (det.details || [])) {
      if (String(d.event_id) !== EVENT) continue
      const cons = (d.consumables || []).map((c: any) => `${c.name} x${c.quantity}`)
      for (const c of (d.consumables || [])) if (/chlorine/i.test(c.name || "")) chlorineUnits += Number(c.quantity) || 0
      logs.push({ day, log_id: d.log_id, in_db: haveLogs.has(String(d.log_id)),
        time_in: d.time_in || null, service_type: d.service_type || null, consumables: cons })
    }
  }
  return { event: EVENT, days: DAYS.length, ion_logs: logs.length, in_db: logs.filter((l) => l.in_db).length,
    not_in_db: logs.filter((l) => !l.in_db).length, chlorine_units_total: chlorineUnits, logs }
}

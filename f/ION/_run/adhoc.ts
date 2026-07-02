//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { main as listDayLogs } from "/f/ION/api/list_day_logs"
import { main as getLogDetail } from "/f/ION/api/get_log_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: for 06/15, list ALL logs, keep SUGARMILL ones, and show each log's full detail
// (event_id, serviceable, time_in, consumables) -- locate the non-serviceable log carrying the 8
// liquid chlorine and see why the ingest drops it.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const sess = await getOrRefreshSession(ion)
  const enr: any = await listDayLogs("06/15/2026", 0, sess)
  const sm = (enr.logs ?? []).filter((l: any) => /sugarmill/i.test(l.customer_name || ""))
  const det: any = await getLogDetail(sm.map((l: any) => ({ log_id: l.log_id, calendar_id: l.calendar_id })), sess)
  const byLog: Record<string, any> = {}
  for (const d of (det.details || [])) byLog[d.log_id] = d
  const out = sm.map((l: any) => {
    const d = byLog[l.log_id] || {}
    return {
      log_id: l.log_id, list_service_type: l.service_type, list_completed: l.completed, status_bullet: l.status_bullet,
      detail_event_id: d.event_id ?? null, serviceable: d.serviceable ?? null, time_in: d.time_in ?? null,
      consumables: (d.consumables || []).map((c: any) => `${c.name} x${c.quantity}`),
    }
  })
  return { date: "06/15/2026", sugarmill_logs: sm.length, logs: out }
}

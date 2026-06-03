//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// CANONICAL LOG-BASED VISIT INGESTION (Carter's design: LogID is the unique grain).
//
// For each day in [start_date, end_date]:
//   1. list_day_logs   -> every COMPLETED service log that day (LogID + calendarID + service)
//   2. get_log_detail  -> per log: EventID(=task), TaskInvoiceID(=billed QBO cust),
//                         scheduled date, time-in/out (-> serviceable), consumables
//   3. resolve EventID -> (task_id uuid, service_location_id) via task_schedules+tasks
//      (DISTINCT ON ion_task_id; all schedule rows for a task point to the same task uuid)
//   4. SCOPED TRANSACTIONAL REPLACE over the date window: delete consumables_usage for the
//      window's visits, delete those visits (cascades visit_tasks + chem_readings), then
//      INSERT one visit per completed log keyed by ion_log_id, plus consumables_usage.
//
// Why replace (not insert-alongside): the billing build collapses to one charge per
// (task, day) and would double-count if a stale mis-attributed row and the new authoritative
// log row both exist for the same task-day. Replace guarantees the window reflects ONLY the
// logs. FK note: consumables_usage is NO ACTION (delete first); visit_tasks/chem_readings CASCADE.
//
// Attribution is DIRECT (no inference): task = EventID, customer flows through the task's
// canonical ion.recurring_tasks.qbo_customer_id, serviceable = real time-in<time-out.
//
// dry_run=true (default): fetch + resolve + report coverage, NO writes. Inspect
// resolved_to_task / unresolved_events before committing. dry_run=false: commit the replace.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { main as listDayLogs } from "/f/ION/api/list_day_logs"
import { main as getLogDetail } from "/f/ION/api/get_log_detail"

function pad(n: number) { return String(n).padStart(2, "0") }
// iterate MM/DD/YYYY inclusive
function eachDay(startMdy: string, endMdy: string): string[] {
  const p = (s: string) => { const [m, d, y] = s.split("/").map(Number); return new Date(Date.UTC(y, m - 1, d)) }
  const a = p(startMdy), b = p(endMdy), out: string[] = []
  for (let t = a.getTime(); t <= b.getTime(); t += 86400000) {
    const dt = new Date(t)
    out.push(`${pad(dt.getUTCMonth() + 1)}/${pad(dt.getUTCDate())}/${dt.getUTCFullYear()}`)
  }
  return out
}
function toIso(mdy: string | null): string | null {
  const m = String(mdy ?? "").match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/)
  return m ? `${m[3]}-${pad(+m[1])}-${pad(+m[2])}` : null
}
function priceFromService(svc: string): number | null {
  const m = String(svc ?? "").match(/(\d{2,4})/)
  return m ? parseInt(m[1]) * 100 : null
}
function tsLocal(isoDate: string | null, t: string | null): string | null {
  if (!isoDate) return null
  const m = String(t ?? "").match(/(\d+):(\d+)\s*(AM|PM)/i)
  if (!m) return null
  let h = (+m[1]) % 12; if (/pm/i.test(m[3])) h += 12
  return `${isoDate} ${pad(h)}:${pad(+m[2])}:00`
}

export async function main(
  start_date: string,
  end_date: string,
  dry_run: boolean = true,
) {
  const days = eachDay(start_date, end_date)

  // ---- 1+2. enumerate + detail per day, build visit candidates ----
  const visits: any[] = []
  const perDay: any[] = []
  for (const day of days) {
    const enr: any = await listDayLogs(day)
    const completed = (enr.logs ?? []).filter((l: any) => l.completed)
    const det: any = await getLogDetail(completed.map((l: any) => ({ log_id: l.log_id, calendar_id: l.calendar_id })))
    const byLog: Record<string, any> = {}
    for (const d of det.details) byLog[d.log_id] = d
    let built = 0, noEvent = 0
    for (const l of completed) {
      const d = byLog[l.log_id] || {}
      if (!d.event_id) { noEvent++; continue }
      const iso = toIso(d.scheduled_date) || toIso(day)
      visits.push({
        ion_log_id: l.log_id, ion_calendar_id: l.calendar_id,
        event_id: String(d.event_id),
        scheduled_date: iso,
        service_type: l.service_type ?? null,
        serviceable: d.serviceable === false ? false : true,
        price_cents: priceFromService(l.service_type),
        time_in: d.time_in ?? null, time_out: d.time_out ?? null,
        consumables: d.consumables || {},
        task_invoice_id: d.task_invoice_id ?? null,
      })
      built++
    }
    perDay.push({ day, completed: completed.length, built, no_event: noEvent })
  }

  // ---- DB connection ----
  const sb: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user, password: sb.password, ssl: "require", max: 4 })

  let result: any
  try {
    // ---- 3. resolve EventID -> task_id uuid, service_location_id, billing_method ----
    const eventIds = [...new Set(visits.map((v) => v.event_id))]
    const taskRows = eventIds.length ? await sql<any[]>`
      SELECT DISTINCT ON (ts.ion_task_id)
             ts.ion_task_id, ts.task_id, t.service_location_id,
             ts.billing_method, rt.task_price_cents
      FROM maintenance.task_schedules ts
      JOIN maintenance.tasks t ON t.id = ts.task_id
      LEFT JOIN ion.recurring_tasks rt ON rt.ion_task_id = ts.ion_task_id
      WHERE ts.ion_task_id = ANY(${eventIds})
      ORDER BY ts.ion_task_id, ts.active DESC, ts.updated_at DESC` : []
    const tmap: Record<string, any> = {}
    for (const r of taskRows) tmap[r.ion_task_id] = r

    let resolved = 0, serviceableN = 0
    for (const v of visits) {
      const tm = tmap[v.event_id]
      v.task_id = tm?.task_id ?? null
      v.service_location_id = tm?.service_location_id ?? null
      v.billing_method = tm?.billing_method ?? "per_visit"
      if (v.price_cents == null) v.price_cents = tm?.task_price_cents ?? null
      if (v.task_id) resolved++
      if (v.serviceable) serviceableN++
    }
    const unresolved = visits.filter((v) => !v.task_id)
    const unresolvedEvents = [...new Set(unresolved.map((v) => v.event_id))]
      .map((e) => ({ event_id: e, service_type: unresolved.find((v) => v.event_id === e)?.service_type }))

    const summary = {
      window: { start: start_date, end: end_date, days: days.length },
      per_day: perDay,
      logs_built: visits.length,
      distinct_events: eventIds.length,
      resolved_to_task: resolved,
      insertable: visits.filter((v) => v.service_location_id).length,
      serviceable: serviceableN,
      unresolved_count: unresolved.length,
      unresolved_events: unresolvedEvents.slice(0, 60),
    }

    if (dry_run) {
      result = { dry_run: true, ...summary }
    } else {
      const isoStart = toIso(start_date), isoEnd = toIso(end_date)
      let deletedCons = 0, deletedVisits = 0, insertedVisits = 0, insertedCons = 0, skipped = 0
      await sql.begin(async (tx: any) => {
        const dc = await tx`DELETE FROM maintenance.consumables_usage
          WHERE visit_id IN (SELECT id FROM maintenance.visits
            WHERE scheduled_date BETWEEN ${isoStart} AND ${isoEnd})`
        deletedCons = dc.count
        const dv = await tx`DELETE FROM maintenance.visits
          WHERE scheduled_date BETWEEN ${isoStart} AND ${isoEnd}`
        deletedVisits = dv.count
        for (const v of visits) {
          if (!v.service_location_id || !v.scheduled_date) { skipped++; continue }
          const [row] = await tx`INSERT INTO maintenance.visits
            (service_location_id, task_id, ion_task_id, scheduled_date, visit_date, is_serviceable,
             service_type, price_cents, billing_method, status, visit_type, started_at, ended_at,
             ion_log_id, ion_calendar_id, external_source)
            VALUES (${v.service_location_id}, ${v.task_id}, ${v.event_id}, ${v.scheduled_date}, ${v.scheduled_date},
             ${v.serviceable}, ${v.service_type}, ${v.price_cents}, ${v.billing_method}, 'completed', 'route',
             ${tsLocal(v.scheduled_date, v.time_in)}, ${tsLocal(v.scheduled_date, v.time_out)},
             ${v.ion_log_id}, ${v.ion_calendar_id}, 'ion_log')
            RETURNING id`
          insertedVisits++
          for (const [itemId, qty] of Object.entries(v.consumables || {})) {
            await tx`INSERT INTO maintenance.consumables_usage (visit_id, item_id, quantity, source, recorded_at)
              VALUES (${row.id}, ${parseInt(itemId)}, ${qty as number}, 'ion_log', now())`
            insertedCons++
          }
        }
      })
      result = { dry_run: false, committed: true, ...summary, deletedCons, deletedVisits, insertedVisits, insertedCons, skipped }
    }
  } finally {
    await sql.end()
  }
  return result
}

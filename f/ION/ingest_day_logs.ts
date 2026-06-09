//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// CANONICAL LOG-BASED VISIT INGESTION (LogID = the unique grain; dedup on ion_log_id).
// Per day: list_day_logs -> get_log_detail (readings/checklist/consumables/tech/notes/failure)
// -> keep performed (time_in) logs -> per-log UPSERT on ion_log_id + refresh children.
// Pass `sess` (a logged-in IonSession) AND `sb` (the supabase resource object) to reuse both
// across a long run and skip per-call wmill variable/resource reads -- those reads degrade
// ~15 min into a long job (broke the first backfills). dry_run=true writes nothing.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { main as listDayLogs } from "/f/ION/api/list_day_logs"
import { main as getLogDetail } from "/f/ION/api/get_log_detail"

function pad(n: number) { return String(n).padStart(2, "0") }
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

export async function main(start_date: string, end_date: string, dry_run: boolean = true, sess: any = null, sb: any = null) {
  // Resolve the DB resource up front (fail fast); reuse the passed one in long runs.
  const res: any = (!dry_run) ? (sb ?? await wmill.getResource("u/carter/supabase")) : null

  const days = eachDay(start_date, end_date)
  const visits: any[] = []
  const perDay: any[] = []
  for (const day of days) {
    const enr: any = await listDayLogs(day, 0, sess)
    const dayLogs = (enr.logs ?? [])
    const det: any = await getLogDetail(dayLogs.map((l: any) => ({ log_id: l.log_id, calendar_id: l.calendar_id })), sess)
    const byLog: Record<string, any> = {}
    for (const d of det.details) byLog[d.log_id] = d
    let built = 0, noEvent = 0, notPerformed = 0
    for (const l of dayLogs) {
      const d = byLog[l.log_id] || {}
      if (!d.event_id) { noEvent++; continue }
      if (!d.time_in) { notPerformed++; continue }
      visits.push({
        ion_log_id: l.log_id, ion_calendar_id: l.calendar_id,
        event_id: String(d.event_id),
        scheduled_date: toIso(d.scheduled_date) || toIso(day),
        service_type: l.service_type ?? null,
        serviceable: d.serviceable === true,
        time_in: d.time_in ?? null, time_out: d.time_out ?? null,
        submitted_by: d.submitted_by ?? null,
        comment: d.comment ?? null,
        failure_reason: d.failure_reason ?? null,
        consumables: d.consumables || [],
        readings: d.readings || [],
        task_checklist: d.task_checklist || [],
      })
      built++
    }
    perDay.push({ day, logs: dayLogs.length, built, no_event: noEvent, not_performed: notPerformed })
  }

  const eventIds = [...new Set(visits.map((v) => v.event_id))]
  const summaryBase = {
    window: { start: start_date, end: end_date, days: days.length },
    per_day: perDay, logs_built: visits.length, distinct_events: eventIds.length,
    readings_rows: visits.reduce((n, v) => n + (v.readings?.length || 0), 0),
    checklist_rows: visits.reduce((n, v) => n + (v.task_checklist?.length || 0), 0),
    consumable_rows: visits.reduce((n, v) => n + (v.consumables?.length || 0), 0),
    with_tech: visits.filter((v) => v.submitted_by).length,
    with_notes: visits.filter((v) => v.comment).length,
  }
  if (dry_run) return { dry_run: true, ...summaryBase }

  const sql = postgres({ host: res.host, port: res.port, database: res.dbname, username: res.user, password: res.password, ssl: "require", max: 4 })
  let result: any
  try {
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

    let resolved = 0
    for (const v of visits) {
      const tm = tmap[v.event_id]
      v.task_id = tm?.task_id ?? null
      v.service_location_id = tm?.service_location_id ?? null
      v.billing_method = tm?.billing_method ?? "per_visit"
      v.price_cents = (tm?.task_price_cents ?? null) ?? priceFromService(v.service_type)
      if (v.task_id) resolved++
    }
    const unknownEvents = [...new Set(visits.filter((v) => !v.task_id).map((v) => v.event_id))]

    let insVisits = 0, insReadings = 0, insChecklist = 0, insConsumables = 0, skipped = 0
    await sql.begin(async (tx: any) => {
      for (const v of visits) {
        if (!v.ion_log_id || !v.scheduled_date) { skipped++; continue }
        const ins = await tx`INSERT INTO maintenance.visits
          (service_location_id, task_id, ion_task_id, scheduled_date, visit_date, is_serviceable,
           service_type, price_cents, billing_method, status, visit_type, started_at, ended_at,
           ion_log_id, ion_calendar_id, ion_submitted_by, notes, failure_reason, external_source)
          VALUES (${v.service_location_id}, ${v.task_id}, ${v.event_id}, ${v.scheduled_date}, ${v.scheduled_date},
           ${v.serviceable}, ${v.service_type}, ${v.price_cents}, ${v.billing_method}, 'completed', 'route',
           ${tsLocal(v.scheduled_date, v.time_in)}, ${tsLocal(v.scheduled_date, v.time_out)},
           ${v.ion_log_id}, ${v.ion_calendar_id}, ${v.submitted_by}, ${v.comment}, ${v.failure_reason}, 'ion_log')
          ON CONFLICT (ion_log_id) WHERE ion_log_id IS NOT NULL DO UPDATE SET
            service_location_id=EXCLUDED.service_location_id, task_id=EXCLUDED.task_id, ion_task_id=EXCLUDED.ion_task_id,
            scheduled_date=EXCLUDED.scheduled_date, visit_date=EXCLUDED.visit_date, is_serviceable=EXCLUDED.is_serviceable,
            service_type=EXCLUDED.service_type, price_cents=EXCLUDED.price_cents, billing_method=EXCLUDED.billing_method,
            started_at=EXCLUDED.started_at, ended_at=EXCLUDED.ended_at, ion_calendar_id=EXCLUDED.ion_calendar_id,
            ion_submitted_by=EXCLUDED.ion_submitted_by, notes=EXCLUDED.notes, failure_reason=EXCLUDED.failure_reason,
            updated_at=now()
          RETURNING id`
        const vid = ins[0].id
        insVisits++
        await tx`DELETE FROM maintenance.visit_readings WHERE visit_id=${vid}`
        await tx`DELETE FROM maintenance.visit_tasks WHERE visit_id=${vid}`
        await tx`DELETE FROM maintenance.consumables_usage WHERE visit_id=${vid}`
        for (const rd of (v.readings || [])) {
          await tx`INSERT INTO maintenance.visit_readings (visit_id, name, value) VALUES (${vid}, ${rd.name}, ${String(rd.value ?? "")})`
          insReadings++
        }
        for (const c of (v.task_checklist || [])) {
          await tx`INSERT INTO maintenance.visit_tasks (visit_id, task_name, completed, source) VALUES (${vid}, ${c.name}, ${c.completed === true}, 'ion')`
          insChecklist++
        }
        for (const c of (v.consumables || [])) {
          await tx`INSERT INTO maintenance.consumables_usage (visit_id, ion_item_id, item_name, quantity, source, recorded_at) VALUES (${vid}, ${c.ion_item_id}, ${c.name}, ${c.quantity}, 'ion', now())`
          insConsumables++
        }
      }
    })
    result = { dry_run: false, committed: true, ...summaryBase, resolved_to_task: resolved, unlinked_visits: visits.filter((v) => !v.task_id).length, unknown_event_ids: unknownEvents.slice(0, 60), insVisits, insReadings, insChecklist, insConsumables, skipped }
  } finally {
    await sql.end()
  }
  return result
}

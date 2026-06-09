//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// CANONICAL LOG-BASED VISIT INGESTION (LogID is the unique grain).
//
// Per day in [start_date, end_date]:
//   1. list_day_logs   -> every service log that day (ALL statuses, not just "completed")
//   2. get_log_detail  -> per log: EventID(task), TaskInvoiceID, times, serviceable,
//                         consumables, task_checklist
//   3. KEEP logs that were PERFORMED = have a time_in AND resolve to a task (EventID).
//   4. resolve EventID -> (task_id, sl, rate); PRICE at task_price_cents (fallback name-parse).
//   5. SCOPED TRANSACTIONAL REPLACE over the window. INSERT one visit per performed log;
//      ON CONFLICT (sl, date, service_type, pool_id, started_at; NULLS NOT DISTINCT) DO NOTHING.
//      Per visit also write consumables_usage + visit_tasks (one row per checklist item,
//      task_name = slug(label)).
//
// dry_run=true (default): fetch + resolve + report, NO writes. dry_run=false: commit.

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
// addLog checklist label -> our canonical visit_tasks.task_name (snake_case), matching
// f/ION/_lib/normalize TASK_DEFINITIONS (e.g. "Skim/Net Surface" -> skim_net_surface).
function slug(s: string): string { return String(s ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") }

export async function main(start_date: string, end_date: string, dry_run: boolean = true) {
  const days = eachDay(start_date, end_date)

  const visits: any[] = []
  const perDay: any[] = []
  for (const day of days) {
    const enr: any = await listDayLogs(day)
    const dayLogs = (enr.logs ?? [])
    const det: any = await getLogDetail(dayLogs.map((l: any) => ({ log_id: l.log_id, calendar_id: l.calendar_id })))
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
        consumables: d.consumables || {},
        task_checklist: d.task_checklist || [],
        task_invoice_id: d.task_invoice_id ?? null,
      })
      built++
    }
    perDay.push({ day, logs: dayLogs.length, built, no_event: noEvent, not_performed: notPerformed })
  }

  const sb: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user, password: sb.password, ssl: "require", max: 4 })

  let result: any
  try {
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
      v.price_cents = (tm?.task_price_cents ?? null) ?? priceFromService(v.service_type)
      if (v.task_id) resolved++
      if (v.serviceable) serviceableN++
    }
    const unresolved = visits.filter((v) => !v.task_id)
    const unresolvedEvents = [...new Set(unresolved.map((v) => v.event_id))]
      .map((e) => ({ event_id: e, service_type: unresolved.find((v) => v.event_id === e)?.service_type }))

    const summary = {
      window: { start: start_date, end: end_date, days: days.length },
      per_day: perDay, logs_built: visits.length, distinct_events: eventIds.length,
      resolved_to_task: resolved, insertable: visits.filter((v) => v.service_location_id).length,
      serviceable: serviceableN, checklist_rows: visits.reduce((n, v) => n + (v.task_checklist?.length || 0), 0),
      unresolved_count: unresolved.length, unresolved_events: unresolvedEvents.slice(0, 60),
    }

    if (dry_run) {
      result = { dry_run: true, ...summary }
    } else {
      const isoStart = toIso(start_date), isoEnd = toIso(end_date)
      let deletedCons = 0, deletedTasks = 0, deletedVisits = 0, insertedVisits = 0, insertedCons = 0, insertedTasks = 0, skippedNoSl = 0, dupSkipped = 0
      await sql.begin(async (tx: any) => {
        const dc = await tx`DELETE FROM maintenance.consumables_usage WHERE visit_id IN (SELECT id FROM maintenance.visits WHERE scheduled_date BETWEEN ${isoStart} AND ${isoEnd})`
        deletedCons = dc.count
        const dtk = await tx`DELETE FROM maintenance.visit_tasks WHERE visit_id IN (SELECT id FROM maintenance.visits WHERE scheduled_date BETWEEN ${isoStart} AND ${isoEnd})`
        deletedTasks = dtk.count
        const dv = await tx`DELETE FROM maintenance.visits WHERE scheduled_date BETWEEN ${isoStart} AND ${isoEnd}`
        deletedVisits = dv.count
        for (const v of visits) {
          if (!v.service_location_id || !v.scheduled_date) { skippedNoSl++; continue }
          const ins = await tx`INSERT INTO maintenance.visits
            (service_location_id, task_id, ion_task_id, scheduled_date, visit_date, is_serviceable,
             service_type, price_cents, billing_method, status, visit_type, started_at, ended_at,
             ion_log_id, ion_calendar_id, external_source)
            VALUES (${v.service_location_id}, ${v.task_id}, ${v.event_id}, ${v.scheduled_date}, ${v.scheduled_date},
             ${v.serviceable}, ${v.service_type}, ${v.price_cents}, ${v.billing_method}, 'completed', 'route',
             ${tsLocal(v.scheduled_date, v.time_in)}, ${tsLocal(v.scheduled_date, v.time_out)},
             ${v.ion_log_id}, ${v.ion_calendar_id}, 'ion_log')
            ON CONFLICT (service_location_id, scheduled_date, service_type, pool_id, started_at) DO NOTHING
            RETURNING id`
          if (!ins.length) { dupSkipped++; continue }
          insertedVisits++
          for (const [itemId, qty] of Object.entries(v.consumables || {})) {
            await tx`INSERT INTO maintenance.consumables_usage (visit_id, item_id, quantity, source, recorded_at)
              VALUES (${ins[0].id}, ${parseInt(itemId)}, ${qty as number}, 'ion', now())`
            insertedCons++
          }
          for (const c of (v.task_checklist || [])) {
            const tn = slug(c.name); if (!tn) continue
            await tx`INSERT INTO maintenance.visit_tasks (visit_id, task_name, completed, source, recorded_at)
              VALUES (${ins[0].id}, ${tn}, ${c.completed === true}, 'ion', now())`
            insertedTasks++
          }
        }
      })
      result = { dry_run: false, committed: true, ...summary, deletedCons, deletedTasks, deletedVisits, insertedVisits, insertedCons, insertedTasks, skippedNoSl, dupSkipped }
    }
  } finally {
    await sql.end()
  }
  return result
}

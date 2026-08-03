//bun-extra-requirements:
//playwright@1.40.0
//chromium-bidi@0.8.0
//postgres@3.4.4

// TARGETED log refresh: re-read SPECIFIC logs by id and upsert them.
//
// This exists because the alternative was absurd: answering a dispute about
// one customer's three visits by re-ingesting every customer's logs for those
// days (474 logs, minutes of wall clock, plus side effects on uninvolved
// customers). We already HOLD each visit's ion_log_id and ion_calendar_id, so
// a refresh is: prime a session once, then one HTTP detail-fetch per log.
// 50 tasks ≈ a couple hundred logs ≈ seconds, not minutes.
//
// The detail-fetch and the upsert are the SAME code path as ingest_day_logs
// (getLogDetail + the per-log upsert keyed on ion_log_id), so a targeted
// refresh can never disagree with the nightly ingest about what a log means.
// What this script does NOT do is discover logs we have never seen — that is
// the day ingest's job; this one refreshes what we know exists.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { main as getLogDetail } from "/f/ION/api/get_log_detail"

const pad = (n: number) => String(n).padStart(2, "0")
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
  log_refs: { log_id: string; calendar_id: string | null }[],
  dry_run: boolean = true,
) {
  if (!Array.isArray(log_refs) || log_refs.length === 0) {
    throw new Error("refresh_logs needs log_refs — refreshing nothing must be an error, not a success")
  }
  const res: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: res.host, port: res.port, database: res.dbname, username: res.user, password: res.password, ssl: "require", max: 4 })

  try {
    // What we currently believe about these logs — the fallbacks the day grid
    // would have supplied (service_type, scheduled day) live on our rows.
    const ids = log_refs.map((r) => String(r.log_id))
    const current = await sql<any[]>`
      SELECT ion_log_id, ion_calendar_id, visit_date, service_type
      FROM maintenance.visits WHERE ion_log_id = ANY(${ids})`
    const cur: Record<string, any> = {}
    for (const c of current) cur[c.ion_log_id] = c

    const det: any = await getLogDetail(
      log_refs.map((r) => ({ log_id: String(r.log_id), calendar_id: r.calendar_id ?? cur[String(r.log_id)]?.ion_calendar_id ?? null })),
      null,
    )

    const visits: any[] = []
    let noEvent = 0, notPerformed = 0
    for (const d of det.details ?? []) {
      const known = cur[String(d.log_id)] ?? {}
      if (!d.event_id) { noEvent++; continue }
      if (!d.time_in) { notPerformed++; continue }
      visits.push({
        ion_log_id: String(d.log_id),
        ion_calendar_id: d.calendar_id ?? known.ion_calendar_id ?? null,
        event_id: String(d.event_id),
        scheduled_date: toIso(d.scheduled_date) || (known.visit_date ? String(known.visit_date).slice(0, 10) : null),
        service_type: known.service_type ?? null,
        service_profile: (d.service_profile && String(d.service_profile).trim()) ? String(d.service_profile).trim() : null,
        serviceable: d.serviceable === true,
        time_in: d.time_in ?? null, time_out: d.time_out ?? null,
        submitted_by: (d.submitted_by && String(d.submitted_by).trim()) ? d.submitted_by : null,
        comment: d.comment ?? null,
        failure_reason: d.failure_reason ?? null,
        consumables: d.consumables || [],
        readings: d.readings || [],
        task_checklist: d.task_checklist || [],
      })
    }

    if (dry_run) {
      await sql.end()
      return { dry_run: true, requested: log_refs.length, fetched: (det.details ?? []).length, would_upsert: visits.length, no_event: noEvent, not_performed: notPerformed }
    }

    const empRows = await sql<any[]>`SELECT id, ion_username FROM public.employees WHERE ion_username IS NOT NULL`
    const aliasMap: Record<string, string> = {}
    for (const e of empRows) for (const a of (e.ion_username || [])) aliasMap[a] = e.id

    const eventIds = [...new Set(visits.map((v) => v.event_id))]
    const taskRows = eventIds.length ? await sql<any[]>`
      SELECT DISTINCT ON (ts.ion_task_id)
             ts.ion_task_id, ts.task_id, t.customer_id, t.billing_method, rt.task_price_cents
      FROM maintenance.task_schedules ts
      JOIN maintenance.tasks t ON t.id = ts.task_id
      LEFT JOIN ion.recurring_tasks rt ON rt.ion_task_id = ts.ion_task_id
      WHERE ts.ion_task_id = ANY(${eventIds})
      ORDER BY ts.ion_task_id, ts.active DESC, ts.updated_at DESC` : []
    const tmap: Record<string, any> = {}
    for (const r of taskRows) tmap[r.ion_task_id] = r

    for (const v of visits) {
      const tm = tmap[v.event_id]
      v.task_id = tm?.task_id ?? null
      v.customer_id = tm?.customer_id ?? null
      v.billing_method = tm?.billing_method ?? "per_visit"
      v.price_cents = (tm?.task_price_cents ?? null) ?? priceFromService(v.service_type)
      v.actual_tech_id = (v.submitted_by && aliasMap[v.submitted_by]) ? aliasMap[v.submitted_by] : null
    }

    let insVisits = 0, insConsumables = 0, skipped = 0
    await sql.begin(async (tx: any) => {
      for (const v of visits) {
        if (!v.ion_log_id || !v.scheduled_date) { skipped++; continue }
        const ins = await tx`INSERT INTO maintenance.visits
          (customer_id, task_id, ion_task_id, scheduled_date, visit_date, is_serviceable,
           service_type, service_profile, price_cents, billing_method, status, visit_type, started_at, ended_at,
           ion_log_id, ion_calendar_id, ion_submitted_by, actual_tech_id, notes, failure_reason, external_source)
          VALUES (${v.customer_id}, ${v.task_id}, ${v.event_id}, ${v.scheduled_date}, ${v.scheduled_date},
           ${v.serviceable}, ${v.service_type}, ${v.service_profile}, ${v.price_cents}, ${v.billing_method}, 'completed', 'route',
           ${tsLocal(v.scheduled_date, v.time_in)}, ${tsLocal(v.scheduled_date, v.time_out)},
           ${v.ion_log_id}, ${v.ion_calendar_id}, ${v.submitted_by}, ${v.actual_tech_id}, ${v.comment}, ${v.failure_reason}, 'ion_log')
          ON CONFLICT (ion_log_id) WHERE ion_log_id IS NOT NULL DO UPDATE SET
            customer_id=COALESCE(EXCLUDED.customer_id, maintenance.visits.customer_id), task_id=EXCLUDED.task_id, ion_task_id=EXCLUDED.ion_task_id,
            scheduled_date=EXCLUDED.scheduled_date, visit_date=EXCLUDED.visit_date, is_serviceable=EXCLUDED.is_serviceable,
            service_type=COALESCE(EXCLUDED.service_type, maintenance.visits.service_type), service_profile=EXCLUDED.service_profile,
            price_cents=EXCLUDED.price_cents, billing_method=EXCLUDED.billing_method,
            started_at=EXCLUDED.started_at, ended_at=EXCLUDED.ended_at, ion_calendar_id=EXCLUDED.ion_calendar_id,
            ion_submitted_by=EXCLUDED.ion_submitted_by, actual_tech_id=COALESCE(EXCLUDED.actual_tech_id, maintenance.visits.actual_tech_id),
            notes=EXCLUDED.notes, failure_reason=EXCLUDED.failure_reason, updated_at=now()
          RETURNING id`
        const vid = ins[0].id
        insVisits++
        await tx`DELETE FROM maintenance.visit_readings WHERE visit_id=${vid}`
        await tx`DELETE FROM maintenance.visit_tasks WHERE visit_id=${vid}`
        await tx`DELETE FROM maintenance.consumables_usage WHERE visit_id=${vid}`
        for (const rd of (v.readings || [])) {
          await tx`INSERT INTO maintenance.visit_readings (visit_id, name, value) VALUES (${vid}, ${rd.name}, ${String(rd.value ?? "")})`
        }
        for (const c of (v.task_checklist || [])) {
          await tx`INSERT INTO maintenance.visit_tasks (visit_id, task_name, completed, source) VALUES (${vid}, ${c.name}, ${c.completed === true}, 'ion')`
        }
        for (const c of (v.consumables || [])) {
          await tx`INSERT INTO maintenance.consumables_usage (visit_id, ion_item_id, item_name, quantity, source, recorded_at) VALUES (${vid}, ${c.ion_item_id}, ${c.name}, ${c.quantity}, 'ion', now())`
          insConsumables++
        }
      }
    })
    await sql.end()
    return { requested: log_refs.length, fetched: (det.details ?? []).length, upserted: insVisits, consumable_rows: insConsumables, skipped, no_event: noEvent, not_performed: notPerformed }
  } catch (e) {
    await sql.end().catch(() => {})
    throw e
  }
}

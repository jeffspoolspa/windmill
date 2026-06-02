//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// Populate ion.recurring_tasks (canonical, one row per ion_task_id) directly from
// ION's recurring-task feed. This is the source-of-truth task census; everything
// downstream (maintenance.tasks, visit->task assignment, per-task reconcile) keys off
// it. Reuses the cached ION session (chromium only if stale), fetches+normalizes via
// reports.ts, then UPSERTs each row into Postgres (u/carter/supabase). Idempotent.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getRecurringTasks } from "/f/ION/_lib/reports"

function priceCents(p: any): number | null {
  const n = parseFloat(String(p ?? "").replace(/[^0-9.\-]/g, ""))
  return isNaN(n) ? null : Math.round(n * 100)
}
function isoDate(s: any): string | null {
  const v = String(s ?? "").trim()
  const m = v.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/)
  return m ? `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}` : null
}

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const tasks: any[] = await getRecurringTasks(session, {})

  const sb: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({
    host: sb.host, port: sb.port, database: sb.dbname,
    username: sb.user, password: sb.password, ssl: "require", max: 4,
  })

  let n = 0
  const dupes: Record<string, number> = {}
  try {
    await sql`TRUNCATE ion.recurring_tasks`
    for (const t of tasks) {
      const id = String(t.ionTaskId ?? "").trim()
      if (!id) continue
      dupes[id] = (dupes[id] ?? 0) + 1
      await sql`
        INSERT INTO ion.recurring_tasks
          (ion_task_id, ion_cust_id, customer_name, customer_type, service_type,
           task_price_cents, billing_type, service_repeat, service_address, city, state,
           zip, zone, route_name, task_start, task_end, last_visit, recurring_notes,
           facility_description, raw, synced_at)
        VALUES
          (${id}, ${t.ionCustId ?? null}, ${t.customerName ?? null}, ${t.customerType ?? null},
           ${t.serviceType ?? null}, ${priceCents(t.taskPrice)}, ${t.billingType ?? null},
           ${t.serviceRepeat ?? null}, ${t.serviceAddress ?? null}, ${t.city ?? null},
           ${t.state ?? null}, ${t.zip ?? null}, ${t.zone ?? null}, ${t.routeName ?? null},
           ${isoDate(t.taskStart)}, ${isoDate(t.taskEnd)}, ${isoDate(t.lastVisit)},
           ${t.recurringNotes ?? null}, ${t.facilityDescription ?? null},
           ${sql.json(t)}, now())
        ON CONFLICT (ion_task_id) DO UPDATE SET
          ion_cust_id=EXCLUDED.ion_cust_id, customer_name=EXCLUDED.customer_name,
          customer_type=EXCLUDED.customer_type, service_type=EXCLUDED.service_type,
          task_price_cents=EXCLUDED.task_price_cents, billing_type=EXCLUDED.billing_type,
          service_repeat=EXCLUDED.service_repeat, service_address=EXCLUDED.service_address,
          city=EXCLUDED.city, state=EXCLUDED.state, zip=EXCLUDED.zip, zone=EXCLUDED.zone,
          route_name=EXCLUDED.route_name, task_start=EXCLUDED.task_start,
          task_end=EXCLUDED.task_end, last_visit=EXCLUDED.last_visit,
          recurring_notes=EXCLUDED.recurring_notes,
          facility_description=EXCLUDED.facility_description, raw=EXCLUDED.raw,
          synced_at=now()
      `
      n++
    }
  } finally {
    await sql.end()
  }
  const dupCount = Object.values(dupes).filter((c) => c > 1).length
  return { fetched: tasks.length, upserted: n, duplicate_ion_task_ids: dupCount }
}

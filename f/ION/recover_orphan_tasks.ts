//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.5
import "playwright@1.40.0"
import postgres from "postgres@3.4.5"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const routeStripped = (s: string) => { s = (s || "").trim(); const i = s.indexOf(" "); return i > 0 ? s.slice(i + 1).trim() : s }
const mapFreq = (r: string) => {
  r = (r || "").trim().toLowerCase().replace(/-/g, "")
  return r === "weekly" ? "weekly" : r === "biweekly" ? "biweekly_a" : r === "daily" ? "daily" : r === "monthly" ? "monthly" : null
}
const LOCK_KEY = 916273

// Recover task-less ("orphan") visits, EventID-driven. Per distinct ion_task_id: read the customer
// from the service log (addLog -> CustomerID), pull task detail, create the task (customer_id only --
// ADR 007 §9: a task carries NO service_location_id) + per-day schedules, and link the visits
// (task_id + customer_id + ion_cust_id; the VISIT's service_location_id is set from the customer's
// confirmed link-table address). Committing, idempotent (skips EventIDs whose task exists), batched
// highest-visit-first, advisory-locked so concurrent / scheduled runs serialize (get_task_detail
// primes the shared ION session, so they MUST NOT overlap).
export async function main(limit = 250) {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const conn = { host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require" as const, prepare: false }
  const lock = postgres({ ...conn, max: 1, idle_timeout: 30, connect_timeout: 15 })
  if (!(await lock`select pg_try_advisory_lock(${LOCK_KEY}) as ok`)[0].ok) { await lock.end(); return { skipped: "another recovery run in progress" } }

  const sql = postgres({ ...conn, max: 3, idle_timeout: 20, connect_timeout: 15 })
  try {
    const targets = await sql`
      select ion_task_id, (array_agg(ion_log_id order by visit_date desc))[1] as log_id, count(*)::int as visits
      from maintenance.visits where task_id is null
      group by ion_task_id order by count(*) desc limit ${limit}`
    if (!targets.length) return { done: true, batch: 0, remaining_orphan_visits: 0 }

    const emps = await sql`select id, ion_username from public.employees where ion_username is not null`
    const byFull = new Map<string, any>(), bySuffix = new Map<string, any>()
    for (const e of emps) for (const u of (e.ion_username || [])) {
      const f = (u || "").trim().toUpperCase(); if (!f) continue
      if (!byFull.has(f)) byFull.set(f, e.id)
      const sf = routeStripped(f); if (!bySuffix.has(sf)) bySuffix.set(sf, e.id)
    }
    const resolveTech = (a: string) => {
      a = (a || "").trim(); if (!a || a.toUpperCase().includes("ASSIGN PEND")) return null
      const up = a.toUpperCase(); return byFull.get(up) ?? bySuffix.get(routeStripped(up)) ?? null
    }
    const custByIon = new Map((await sql`select ion_cust_id, id from public."Customers" where ion_cust_id is not null`).map((x: any) => [String(x.ion_cust_id), Number(x.id)]))
    const slByCust = new Map((await sql`
        select csa.customer_id,
               coalesce(min(sl.id) filter (where sl.geocode_status='ok'), min(sl.id)) as sl
        from public.customer_service_addresses csa
        join public.service_locations sl on sl.id = csa.service_location_id
        where csa.is_active group by csa.customer_id`).map((x: any) => [Number(x.customer_id), Number(x.sl)]))

    const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
    const s = await getOrRefreshSession(ion)
    const o = s.ionOrigin
    const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
    const get = (u: string) => fetch(`${o}${u}`, { headers: H, redirect: "manual" }).then((x) => x.text())

    const today = new Date().toISOString().slice(0, 10)
    const stats: any = { batch: targets.length, tasks_created: 0, tasks_created_no_location: 0, schedules_created: 0, visits_linked: 0, customer_unmatched: 0, no_customerid_on_log: 0, errors: 0, examples: [] }

    for (const t of targets) {
      const eid = String(t.ion_task_id)
      try {
        const logHtml = await get(`/tasks/addLog.cfm?LogID=${t.log_id}&Source=ServiceLog`)
        const ionCust = parse(logHtml).querySelector('input[name="CustomerID"]')?.getAttribute("value") || (logHtml.match(/CustomerID=(\d+)/) || [])[1]
        if (!ionCust) { stats.no_customerid_on_log++; continue }
        const customerId = custByIon.get(String(ionCust)) ?? null
        if (customerId == null) { stats.customer_unmatched++; continue } // can't attribute a task with no owner
        const slId = slByCust.get(customerId) ?? null // the customer's confirmed location -> the VISIT's location (may be null -> resolved later)

        const ex = await sql`select id from maintenance.tasks where ion_task_id = ${eid} limit 1`
        let tid: any
        if (ex.length) {
          tid = ex[0].id
        } else {
          // ADR 007 §9: the task carries NO service_location_id (a contract can outlive an address
          // change); customer_id is the owner. The visit's location comes from the customer (slId).
          const { detail } = await getTaskDetail(s, eid, ionCust)
          const startsOn = detail.startsOn || null
          const endsOn = detail.endsOn || null
          const status = endsOn && endsOn < today ? "closed" : "active"
          const ext = { ion_cust_id: String(ionCust), service_type: detail.serviceType?.text || "", recurrence: detail.serviceRepeat?.text || "", captured: "orphan_recovery" }
          tid = (await sql`
            insert into maintenance.tasks (customer_id, ion_task_id, status, starts_on, ends_on, external_source, external_data)
            values (${customerId}, ${eid}, ${status}, coalesce(${startsOn}::date, current_date), ${endsOn}::date, 'ion_log', ${sql.json(ext)})
            returning id`)[0].id
          stats.tasks_created++
          if (slId == null) stats.tasks_created_no_location++
          const freq = mapFreq(detail.serviceRepeat?.text)
          for (const d of (detail.perDayTech || []).filter((x: any) => x.techId)) {
            await sql`
              insert into maintenance.task_schedules (task_id, ion_task_id, day_of_week, tech_employee_id, frequency, active, starts_on, ends_on, external_source)
              values (${tid}, ${eid}, ${d.dow}, ${resolveTech(d.techName)}, ${freq}, ${status !== "closed"}, coalesce(${startsOn}::date, current_date), ${endsOn}::date, 'ion_log')`
            stats.schedules_created++
          }
        }
        const upd = await sql`
          update maintenance.visits set task_id = ${tid}, customer_id = ${customerId}, ion_cust_id = ${String(ionCust)}, service_location_id = ${slId}
          where ion_task_id = ${eid} and task_id is null`
        stats.visits_linked += upd.count
      } catch (e: any) {
        stats.errors++
        if (stats.examples.length < 12) stats.examples.push({ eid, error: String(e?.message ?? e).slice(0, 160) })
      }
    }
    stats.remaining_orphan_visits = Number((await sql`select count(*)::int as n from maintenance.visits where task_id is null`)[0].n)
    return stats
  } finally {
    await sql.end().catch(() => {})
    try { await lock`select pg_advisory_unlock(${LOCK_KEY})` } catch {}
    await lock.end().catch(() => {})
  }
}

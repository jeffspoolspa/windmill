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
const norm = (s: string) => (s || "").toLowerCase().replace(/[^a-z0-9]/g, "")
const phone10 = (s: string) => { const d = (s || "").replace(/\D/g, ""); return d.length >= 10 ? d.slice(-10) : "" }
const emailRe = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g
const phoneRe = /\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}/g
const EMAIL_NOISE = /placs\.net|medtronic|volmedia/i  // placeholder emails baked into the ION page template
const LOCK_KEY = 916273

// Recover task-less ("orphan") visits, EventID-driven. INVARIANT: every orphan visit MUST get a task
// (else it is silently unbillable). Per distinct ion_task_id: read the ION CustomerID from the service
// log (addLog), pull task detail, create the task and link the visits. A task carries NO
// service_location_id (ADR 007 §9); customer_id is the owner via ion_cust_id.
//
// SELF-HEAL: ION exposes no QBO id, so a Customer synced from QBO starts with ion_cust_id NULL and is
// invisible to the by-ion_cust_id match. Before flagging an unmatched CustomerID, we fetch the ION
// customer detail (customerTabs) and ADOPT an UNLINKED Customer (ion_cust_id IS NULL) by exact email,
// then exact display_name, then phone -- a UNIQUE hit sets that Customer's ion_cust_id so the task
// attributes correctly and future runs match directly. Email/name are reliable; phone drifts. Only a
// genuinely-new customer (no confident match) stays customer_id = NULL + FLAGGED
// (external_data.needs_fix). Committing, idempotent, batched highest-visit-first, advisory-locked.
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

    // UNLINKED Customers (ion_cust_id IS NULL) for self-heal adoption, indexed by email / name / phone.
    const pushArr = (m: Map<string, number[]>, k: string, v: number) => { const a = m.get(k); if (a) a.push(v); else m.set(k, [v]) }
    const unlByEmail = new Map<string, number[]>(), unlByName = new Map<string, number[]>(), unlByPhone = new Map<string, number[]>()
    for (const c of (await sql`select id, display_name, email, phone from public."Customers" where ion_cust_id is null and deleted_at is null`)) {
      if ((c as any).email) pushArr(unlByEmail, String((c as any).email).toLowerCase().trim(), Number((c as any).id))
      const n = norm((c as any).display_name); if (n) pushArr(unlByName, n, Number((c as any).id))
      const p = phone10((c as any).phone); if (p) pushArr(unlByPhone, p, Number((c as any).id))
    }
    const adopted = new Set<number>()

    const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
    const s = await getOrRefreshSession(ion)
    const o = s.ionOrigin
    const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
    const get = (u: string) => fetch(`${o}${u}`, { headers: H, redirect: "manual" }).then((x) => x.text())

    const today = new Date().toISOString().slice(0, 10)
    const stats: any = { batch: targets.length, tasks_created: 0, tasks_created_no_location: 0, tasks_created_needs_customer: 0, self_healed: 0, self_heal_examples: [], schedules_created: 0, visits_linked: 0, customer_unmatched: 0, no_customerid_on_log: 0, errors: 0, examples: [] }

    for (const t of targets) {
      const eid = String(t.ion_task_id)
      try {
        const logHtml = await get(`/tasks/addLog.cfm?LogID=${t.log_id}&Source=ServiceLog`)
        const ionCust = parse(logHtml).querySelector('input[name="CustomerID"]')?.getAttribute("value") || (logHtml.match(/CustomerID=(\d+)/) || [])[1] || null
        if (!ionCust) stats.no_customerid_on_log++
        let customerId = ionCust ? (custByIon.get(String(ionCust)) ?? null) : null

        // SELF-HEAL: ION CustomerID with no linked Customer -> fetch the ION customer detail and adopt an
        // UNLINKED Customer (ion_cust_id IS NULL) by exact email -> exact display_name -> phone (unique).
        if (customerId == null && ionCust) {
          try {
            const chtml = await get(`/customers/customerTabs.cfm?customerid=${ionCust}`)
            const croot = parse(chtml); croot.querySelectorAll("script, style").forEach((n: any) => n.remove())
            const ctext = croot.text.replace(/\s+/g, " ").trim()
            const cEmail = ((chtml.match(emailRe) || []).filter((e: string) => !EMAIL_NOISE.test(e))[0] || "").toLowerCase().trim()
            const cName = (ctext.match(/QuickBooks Data\s+([A-Za-z0-9][^]{0,60}?)\s+Customer ID:\s*\d/)?.[1] || "").trim()
            const cPhone = phone10((ctext.match(phoneRe) || [])[0] || "")
            let cand: number[] = [], how = ""
            if (cEmail && unlByEmail.has(cEmail)) { cand = unlByEmail.get(cEmail)!; how = "email" }
            else if (cName && unlByName.has(norm(cName))) { cand = unlByName.get(norm(cName))!; how = "name" }
            else if (cPhone && unlByPhone.has(cPhone)) { cand = unlByPhone.get(cPhone)!; how = "phone" }
            cand = cand.filter((id) => !adopted.has(id))
            if (cand.length === 1) {
              customerId = cand[0]; adopted.add(customerId)
              await sql`update public."Customers" set ion_cust_id=${String(ionCust)}, ion_match_method='ion_self_heal', ion_match_confidence='high', ion_matched_at=now() where id=${customerId} and ion_cust_id is null`
              custByIon.set(String(ionCust), customerId)
              stats.self_healed++
              if (stats.self_heal_examples.length < 12) stats.self_heal_examples.push({ ion_cust_id: String(ionCust), customer_id: customerId, via: how })
            }
          } catch (e: any) { /* fall through -> flagged */ }
        }

        if (ionCust && customerId == null) stats.customer_unmatched++
        const slId = customerId != null ? (slByCust.get(customerId) ?? null) : null

        const ex = await sql`select id from maintenance.tasks where ion_task_id = ${eid} limit 1`
        let tid: any
        if (ex.length) {
          tid = ex[0].id
        } else {
          // ADR 007 §9: task carries customer_id (best-effort) + NO service_location_id. Pull task detail
          // when we have a CustomerID to navigate ION; else a minimal stub. customer_id IS NULL = flag.
          // billing_type is captured from the ION task edit form's InvoiceType so captured tasks carry
          // the SAME external_data shape as the recurring sync (Do Not Invoice / list vs separate
          // consumables) -- the reconcile needs it to know whether/how a task's consumables bill.
          let startsOn: any = null, endsOn: any = null, perDayTech: any[] = [], serviceType = "", recurrence = "", billingType = "", itemCost = ""
          if (ionCust) {
            try {
              const { detail } = await getTaskDetail(s, eid, ionCust)
              startsOn = detail.startsOn || null
              endsOn = detail.endsOn || null
              perDayTech = detail.perDayTech || []
              serviceType = detail.serviceType?.text || ""
              recurrence = detail.serviceRepeat?.text || ""
              billingType = detail.invoiceType?.text || ""
              itemCost = detail.itemCost || ""
            } catch (e: any) {
              if (stats.examples.length < 12) stats.examples.push({ eid, note: "get_task_detail failed; created stub", error: String(e?.message ?? e).slice(0, 120) })
            }
          }
          // FINANCIAL TERMS (a task carries its own rate, ADR 007 §9). Derive from the ION task edit form
          // so captured tasks reconcile against the ION invoice like recurring ones:
          //   method: invoiceType "Flat..." -> flat_rate_monthly, else per_visit.
          //   CUSTOMER PRICE = the "Custom Pricing" field = detail.itemCost. Verified 2026-07-01: The Farm
          //     flat $1190 lives in itemcost="1190.00"; StopPayFixed="0.00" is the Technician Per-Stop Pay
          //     (tech comp, NOT the bill) -- do NOT use it.
          //   flat rate: itemCost (the monthly amount), else null (flag; never derivable from tech pay).
          //   per-visit rate: itemCost override if set, else the "@ $X.XX" in the description
          //     (GREEN POOL / ONE TIME), else the "POOL MAINTENANCE <N>" tier (== rate ~99% of the time).
          const isFlat = /FLAT/i.test(billingType)
          const custom = parseFloat(String(itemCost).replace(/[^0-9.]/g, "")) || 0
          const atPrice = serviceType.match(/@\s*\$?([0-9]+(?:\.[0-9]+)?)/)
          const tier = serviceType.match(/POOL MAINTENANCE\s+([0-9]+)/i)
          const billingMethod = isFlat ? "flat_rate_monthly" : "per_visit"
          const ppvCents = isFlat ? null
            : (custom > 0 ? Math.round(custom * 100)
               : atPrice ? Math.round(parseFloat(atPrice[1]) * 100)
               : tier ? parseInt(tier[1]) * 100 : null)
          const flatCents = isFlat ? (custom > 0 ? Math.round(custom * 100) : null) : null
          const status = endsOn && endsOn < today ? "closed" : "active"
          const needsFix = customerId == null
          const ext: any = { ion_cust_id: ionCust ? String(ionCust) : null, service_type: serviceType, recurrence, billing_type: billingType, captured: "orphan_recovery" }
          if (needsFix) ext.needs_fix = ionCust ? "customer_unmatched" : "no_customerid_on_log"
          tid = (await sql`
            insert into maintenance.tasks (customer_id, ion_task_id, status, starts_on, ends_on, billing_method, price_per_visit_cents, flat_rate_monthly_cents, external_source, external_data)
            values (${customerId}, ${eid}, ${status}, coalesce(${startsOn}::date, current_date), ${endsOn}::date, ${billingMethod}, ${ppvCents}, ${flatCents}, 'ion_log', ${sql.json(ext)})
            returning id`)[0].id
          stats.tasks_created++
          if (needsFix) stats.tasks_created_needs_customer++
          else if (slId == null) stats.tasks_created_no_location++
          const freq = mapFreq(recurrence)
          for (const d of perDayTech.filter((x: any) => x.techId)) {
            await sql`
              insert into maintenance.task_schedules (task_id, ion_task_id, day_of_week, tech_employee_id, frequency, active, starts_on, ends_on, external_source)
              values (${tid}, ${eid}, ${d.dow}, ${resolveTech(d.techName)}, ${freq}, ${status !== "closed"}, coalesce(${startsOn}::date, current_date), ${endsOn}::date, 'ion_log')`
            stats.schedules_created++
          }
        }
        const upd = await sql`
          update maintenance.visits set task_id = ${tid},
              customer_id = coalesce(${customerId}, maintenance.visits.customer_id),
              ion_cust_id = coalesce(${ionCust ? String(ionCust) : null}, maintenance.visits.ion_cust_id),
              service_location_id = coalesce(${slId}, maintenance.visits.service_location_id)
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

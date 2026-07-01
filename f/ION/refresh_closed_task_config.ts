//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// PRE-BILLING CONFIG REFRESH (run ONCE before freezing a month; NOT daily).
// WHY: the recurring sync only re-pulls ACTIVE ION tasks. A task edited in ION *after* it expires
// (BARTH flat->per-visit; OLSON/HAYES POOL MAINTENANCE->QUALITY CONTROL @ $0.00, verified 2026-07-01)
// keeps stale financial terms in our DB, so the billing build over/under-expects labor. This re-pulls
// getTaskDetail for EXPIRED tasks that have visits in the target month and rewrites their financial
// terms + billing_type to match ION -- the SAME derivation as recover_orphan_tasks (single source of truth).
// dry_run (default) reports every diff and writes nothing. `month` = "YYYY-MM" of the billing period.

function monthBounds(month: string) {
  const m = month.match(/^(\d{4})-(\d{2})$/)
  if (!m) throw new Error(`month must be "YYYY-MM", got ${month}`)
  const y = +m[1], mo = +m[2]
  const start = `${m[1]}-${m[2]}-01`
  const end = mo === 12 ? `${y + 1}-01-01` : `${y}-${String(mo + 1).padStart(2, "0")}-01`
  return { start, end }
}

// Financial-terms derivation -- KEEP IN SYNC with f/ION/recover_orphan_tasks.ts (the rate rule, ADR 007 §9):
//   method: invoiceType "Flat..." -> flat_rate_monthly, else per_visit.
//   Custom Pricing (detail.itemCost) overrides; else "@ $X.XX" in description; else "POOL MAINTENANCE <N>" tier.
function deriveTerms(serviceType: string, invoiceType: string, itemCost: string) {
  const isFlat = /FLAT/i.test(invoiceType)
  const custom = parseFloat(String(itemCost).replace(/[^0-9.]/g, "")) || 0
  const atPrice = serviceType.match(/@\s*\$?([0-9]+(?:\.[0-9]+)?)/)
  const tier = serviceType.match(/POOL MAINTENANCE\s+([0-9]+)/i)
  const billingMethod = isFlat ? "flat_rate_monthly" : "per_visit"
  const ppvCents = isFlat ? null
    : (custom > 0 ? Math.round(custom * 100)
       : atPrice ? Math.round(parseFloat(atPrice[1]) * 100)
       : tier ? parseInt(tier[1]) * 100 : null)
  const flatCents = isFlat ? (custom > 0 ? Math.round(custom * 100) : null) : null
  return { billingMethod, ppvCents, flatCents, billingType: invoiceType, serviceType }
}

export async function main(month: string, dry_run: boolean = true) {
  const { start, end } = monthBounds(month)
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 3, prepare: false })
  try {
    // Expired tasks (ION no longer re-syncs them) that have a visit in the billing month.
    const targets = await sql<any[]>`
      select t.id, t.ion_task_id::text as eid, t.billing_method, t.price_per_visit_cents, t.flat_rate_monthly_cents,
             t.external_data->>'billing_type' as billing_type,
             coalesce(t.external_data->>'ion_cust_id', c.ion_cust_id::text) as ion_cust_id
      from maintenance.tasks t
      join public."Customers" c on c.id = t.customer_id
      where t.ends_on is not null and t.ends_on < current_date
        and exists (select 1 from maintenance.visits v
                    where v.task_id = t.id and v.visit_date >= ${start} and v.visit_date < ${end})
      group by t.id, c.ion_cust_id
      order by t.id`
    if (!targets.length) return { month, dry_run, targets: 0, changed: 0, diffs: [] }

    const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
    const s = await getOrRefreshSession(ion)

    const diffs: any[] = []
    let changed = 0, errors = 0, no_cust = 0
    for (const t of targets) {
      if (!t.ion_cust_id) { no_cust++; continue }
      try {
        const { detail } = await getTaskDetail(s, t.eid, t.ion_cust_id)
        const d = deriveTerms(detail.serviceType?.text || "", detail.invoiceType?.text || "", detail.itemCost || "")
        const before = { billing_method: t.billing_method, ppv: t.price_per_visit_cents, flat: t.flat_rate_monthly_cents, billing_type: t.billing_type }
        const after = { billing_method: d.billingMethod, ppv: d.ppvCents, flat: d.flatCents, billing_type: d.billingType }
        const stale = before.billing_method !== after.billing_method || before.ppv !== after.ppv || before.flat !== after.flat || (before.billing_type || "") !== (after.billing_type || "")
        if (!stale) continue
        diffs.push({ task_id: t.id, eid: t.eid, service_type: d.serviceType, before, after })
        changed++
        if (!dry_run) {
          await sql`
            update maintenance.tasks set
              billing_method = ${d.billingMethod},
              price_per_visit_cents = ${d.ppvCents},
              flat_rate_monthly_cents = ${d.flatCents},
              external_data = external_data
                || jsonb_build_object('billing_type', ${d.billingType}, 'service_type', ${d.serviceType},
                                      'config_refreshed_at', to_char(now(), 'YYYY-MM-DD"T"HH24:MI:SSZ'))
            where id = ${t.id}`
        }
      } catch (e: any) {
        errors++
        diffs.push({ task_id: t.id, eid: t.eid, error: String(e?.message ?? e).slice(0, 160) })
      }
    }
    return { month, dry_run, targets: targets.length, changed, errors, no_cust, diffs }
  } finally {
    await sql.end().catch(() => {})
  }
}

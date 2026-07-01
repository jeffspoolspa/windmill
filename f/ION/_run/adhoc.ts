//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.5
import "playwright@1.40.0"
import postgres from "postgres@3.4.5"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. Override main()'s body for any one-off ION task; redeploy
// (createScript with parent_hash) then run via runScriptByPath -> getJob for the result.
// One reusable script instead of a pile of throwaway _run/ scripts.
//
// CURRENT JOB: backfill the labor rate on rate-less tasks from the ION task edit form.
// RULE (Carter 2026-07-01): the customer price is "Custom Pricing" = detail.itemCost.
//   - itemCost EMPTY or 0 -> the rate is what's parsed from the description ("@ $X.XX", else the
//                            "POOL MAINTENANCE <N>" tier).
//   - itemCost POPULATED  -> it OVERRIDES the description.
//   - per_visit  -> price_per_visit_cents = that rate (labor = rate x billable visits).
//   - flat_rate  -> flat_rate_monthly_cents = itemCost (labor = the flat, regardless of visits).
//   StopPayFixed is Technician Per-Stop Pay (tech comp) -- never the bill.
export async function main() {
  const DRY_RUN = false   // <-- flip to false to COMMIT

  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require" as const, prepare: false, max: 3 })
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const session = await getOrRefreshSession(ion)

  try {
    // rate-less tasks that actually bill (skip QC), scoped to ones with recent (>= May) visits.
    const tasks = await sql`
      select t.id, t.ion_task_id, t.external_data->>'ion_cust_id' as ion_cust_id,
             t.external_data->>'billing_type' as btype, t.billing_method
      from maintenance.tasks t
      where exists (select 1 from maintenance.visits v where v.task_id = t.id and v.scheduled_date >= date '2026-05-01')
        and ( (coalesce(t.billing_method,'per_visit') <> 'flat_rate_monthly' and coalesce(t.price_per_visit_cents,0) = 0)
           or (t.billing_method = 'flat_rate_monthly' and coalesce(t.flat_rate_monthly_cents,0) = 0) )
        and t.external_data->>'ion_cust_id' is not null
        and coalesce(t.external_data->>'service_type','') not ilike '%QUALITY CONTROL%'
      order by t.id
      limit 300`

    const results: any[] = []
    let updated = 0, skipped_no_rate = 0, errored = 0
    for (const t of tasks) {
      try {
        const { detail } = await getTaskDetail(session, String(t.ion_task_id), String(t.ion_cust_id))
        const svc = detail.serviceType?.text || ""
        const inv = detail.invoiceType?.text || t.btype || ""
        const isFlat = /FLAT/i.test(inv)
        const custom = parseFloat(String(detail.itemCost || "").replace(/[^0-9.]/g, "")) || 0
        const atM = svc.match(/@\s*\$?([0-9]+(?:\.[0-9]+)?)/)
        const tierM = svc.match(/POOL MAINTENANCE\s+([0-9]+)/i)
        const parsed = atM ? Math.round(parseFloat(atM[1]) * 100) : tierM ? parseInt(tierM[1]) * 100 : null
        const method = isFlat ? "flat_rate_monthly" : "per_visit"
        let ppv: number | null = null, flat: number | null = null, source = "none"
        if (isFlat) {
          if (custom > 0) { flat = Math.round(custom * 100); source = "itemCost(flat)" }
        } else {
          if (custom > 0) { ppv = Math.round(custom * 100); source = "itemCost(override)" }
          else if (parsed != null) { ppv = parsed; source = atM ? "desc @ $" : "tier" }
        }
        results.push({ ion_task_id: String(t.ion_task_id), method, itemCost: detail.itemCost, service: svc.slice(0, 42), invoice: inv, ppv, flat, source })
        if (!DRY_RUN && (ppv != null || flat != null)) {
          await sql`update maintenance.tasks set billing_method=${method}, price_per_visit_cents=${ppv}, flat_rate_monthly_cents=${flat}, updated_at=now() where id=${t.id}`
          updated++
        } else if (ppv == null && flat == null) skipped_no_rate++
      } catch (e: any) {
        errored++
        results.push({ ion_task_id: String(t.ion_task_id), error: String(e?.message ?? e).slice(0, 120) })
      }
    }
    return { dry_run: DRY_RUN, committed: !DRY_RUN, considered: tasks.length, updated, skipped_no_rate, errored, results }
  } finally {
    await sql.end().catch(() => {})
  }
}

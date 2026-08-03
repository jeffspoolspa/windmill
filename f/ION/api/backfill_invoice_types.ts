//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// Backfill maintenance.tasks.ion_invoice_type for tasks BILLED IN A GIVEN
// MONTH (RULED: only tasks we have invoices for — the vocabulary is needed
// where documents will be generated, not on every dormant task).
//
// One cached session, then one HTTP task-detail read per task (the same
// getTaskDetail the verify path uses — one code path, one meaning), writing
// InvoiceType back onto the task. Idempotent: only NULL rows are targeted;
// re-running converges to nothing to do.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

export async function main(
  month: string = "2026-07-01",
  dry_run: boolean = true,
  limit: number = 600,
) {
  if (!/^\d{4}-\d{2}-01$/.test(month)) throw new Error("month must be YYYY-MM-01")
  const res: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: res.host, port: res.port, database: res.dbname, username: res.user, password: res.password, ssl: "require", max: 4 })

  try {
    const targets = await sql<any[]>`
      SELECT t.id, t.ion_task_id, t.customer_id, c.ion_cust_id
      FROM maintenance.tasks t
      JOIN "Customers" c ON c.id = t.customer_id
      WHERE t.ion_invoice_type IS NULL AND t.ion_task_id IS NOT NULL
        AND t.id IN (
          SELECT DISTINCT bi.task_id::uuid
          FROM billing.billable_items bi
          JOIN billing.billing_months bm ON bm.id = bi.billing_month_id AND bm.month = ${month}
          WHERE bi.task_id IS NOT NULL)
      LIMIT ${limit}`

    if (dry_run) {
      await sql.end()
      return { dry_run: true, month, targets: targets.length }
    }

    const ion = {
      loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
      username: await wmill.getVariable("f/ION/USERNAME"),
      password: await wmill.getVariable("f/ION/PASSWORD"),
    }
    const session = await getOrRefreshSession(ion)

    let updated = 0, empty = 0, failed = 0
    const failures: { ion_task_id: string; error: string }[] = []
    for (const t of targets) {
      try {
        const { detail } = await getTaskDetail(session, t.ion_task_id, t.ion_cust_id ?? "")
        const invoiceType = String(detail?.invoiceType?.text ?? "").trim()
        if (!invoiceType) { empty++; continue }
        const r = await sql`
          UPDATE maintenance.tasks SET ion_invoice_type = ${invoiceType}
          WHERE id = ${t.id} AND ion_invoice_type IS NULL RETURNING id`
        if (r.length === 1) updated++
      } catch (e: any) {
        failed++
        failures.push({ ion_task_id: String(t.ion_task_id), error: String(e?.message ?? e).slice(0, 160) })
        if (failed > 25) throw new Error(`aborting: ${failed} failures — session likely dead; last: ${failures.at(-1)?.error}`)
      }
    }
    await sql.end()
    return { month, targets: targets.length, updated, empty, failed, failures: failures.slice(0, 10) }
  } catch (e) {
    await sql.end().catch(() => {})
    throw e
  }
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import { chromium } from "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// ION "All Transactions" report (TransactionType=Tasks) for a month.
//
// WHY A BROWSER (verified empirically 2026-07-01): /reports/_xls/allTransactions.cfm reads its
// criteria from the ColdFusion SESSION, and that session state is only created/updated by a REAL
// browser navigation form-submit of /reports/transactionRpt.cfm. A fetch POST -- raw Bun with the
// exact captured Chrome navigation headers, or even an in-page fetch on Chrome's own network stack --
// gets the form re-rendered and the criteria are NOT applied (ION is behind Imperva Incapsula; only
// page-load initializes the report session [with default dates], and only a navigation submit sets
// the chosen criteria). Symptoms: XLS 500 = no form page loaded this session; XLS 200-but-tiny =
// page loaded but criteria never submitted. So: goto form -> set fields -> form.submit() -> in-page
// fetch the XLS (goto would abort on the attachment). ~15s on a chromium worker.
// See docs/integrations/ion.md ("Two classes of report priming").
//
// Columns: Office|Customer|Address|Route|Customer Type|Transaction ID|Transaction Date|
// Transaction Type|Service Name|Completed By|Amount|Status|Status Date|Additional Info ("Task <id>").
// dry_run (default) parses only; load=true replaces that month in billing_audit.ion_task_transactions.

function bounds(month: string) {
  const m = month.match(/^(\d{4})-(\d{2})$/); if (!m) throw new Error(`month must be YYYY-MM, got ${month}`)
  const y = +m[1], mo = +m[2]
  const last = new Date(Date.UTC(mo === 12 ? y + 1 : y, mo === 12 ? 0 : mo, 0)).getUTCDate()
  const dd = (n: number) => String(n).padStart(2, "0")
  return { start: `${m[1]}-${m[2]}-01`, end: `${m[1]}-${m[2]}-${dd(last)}`, us_start: `${m[2]}/01/${m[1]}`, us_end: `${m[2]}/${dd(last)}/${m[1]}`, monthDate: `${m[1]}-${m[2]}-01` }
}
const toIsoDate = (mdy: string) => { const m = String(mdy).match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/); return m ? `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}` : null }

export async function main(month: string, dry_run: boolean = true, load: boolean = false) {
  const b = bounds(month)
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin

  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] })
  let xls = ""
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0" })
    await context.addCookies((s.cookies || []).map((c: any) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path || "/", secure: !!c.secure, httpOnly: !!c.httpOnly })))
    const page = await context.newPage()
    await page.goto(`${o}/reports/transactionRpt.cfm`, { waitUntil: "domcontentloaded" })
    await page.evaluate((a: any) => {
      const g = (id: string) => document.getElementById(id) as any
      if (g("rptStart")) g("rptStart").value = a.start
      if (g("rptEnd")) g("rptEnd").value = a.end
      const tt = document.querySelector('select[name="TransactionType"]') as any; if (tt) tt.value = "Tasks"
      const wf = document.querySelector('input[name="WorkFrom"]') as any; if (wf) wf.value = a.us_start
      const wt = document.querySelector('input[name="WorkTo"]') as any; if (wt) wt.value = a.us_end
    }, { start: b.start, end: b.end, us_start: b.us_start, us_end: b.us_end })
    await Promise.all([page.waitForLoadState("networkidle").catch(() => {}), page.evaluate(() => (document.getElementById("rpt") as any).submit())])
    await page.waitForTimeout(1000)
    const r: any = await page.evaluate(async (u: string) => { const x = await fetch(u, { credentials: "include" }); return { status: x.status, body: await x.text() } }, `${o}/reports/_xls/allTransactions.cfm`)
    if (r.status !== 200) throw new Error(`XLS fetch failed: status ${r.status}`)
    xls = r.body
  } finally { await browser.close() }

  const table = parse(xls).querySelector("table")
  const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))) : []
  const hi = rows.findIndex((r: string[]) => r.some((c) => /^Transaction ID$/i.test(c)))
  if (hi < 0) throw new Error("header row not found")
  const head = rows[hi]
  const col = (name: string) => head.findIndex((c: string) => c.toLowerCase() === name.toLowerCase())
  const ci = { tid: col("Transaction ID"), date: col("Transaction Date"), svc: col("Service Name"), amt: col("Amount"), status: col("Status"), cust: col("Customer"), info: col("Additional Info") }

  const recs: any[] = []
  for (let i = hi + 1; i < rows.length; i++) {
    const r = rows[i]; if (!r.some((c) => c)) continue
    const task = (r[ci.info] || "").match(/Task\s+(\d+)/)?.[1]; if (!task) continue
    const amtRaw = (r[ci.amt] || "").replace(/[^0-9.\-]/g, ""); if (amtRaw === "") continue
    recs.push({ transaction_id: r[ci.tid] || null, ion_task_id: task, amt_cents: Math.round(parseFloat(amtRaw) * 100),
      customer: r[ci.cust] || null, service_name: r[ci.svc] || null, status: r[ci.status] || null, transaction_date: toIsoDate(r[ci.date] || "") })
  }
  // sanity: a real month should never parse to a handful of rows -- tiny result = criteria didn't take
  if (recs.length < 10) throw new Error(`suspiciously few rows (${recs.length}) -- report criteria likely not applied; not loading`)

  let loaded = 0
  let ionStamped = 0
  if (!dry_run && load) {
    const cfg = (await wmill.getResource("u/carter/supabase")) as any
    const sql = postgres({ host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require", max: 3, prepare: false })
    try {
      await sql.begin(async (tx: any) => {
        await tx`delete from billing_audit.ion_task_transactions where month = ${b.monthDate}`
        for (const r of recs.filter((x) => x.transaction_id)) {
          await tx`insert into billing_audit.ion_task_transactions (transaction_id, month, ion_task_id, amt_cents, customer, service_name, status, transaction_date)
            values (${r.transaction_id}, ${b.monthDate}, ${r.ion_task_id}, ${r.amt_cents}, ${r.customer}, ${r.service_name}, ${r.status}, ${r.transaction_date})
            on conflict (transaction_id) do update set month=excluded.month, ion_task_id=excluded.ion_task_id, amt_cents=excluded.amt_cents,
              customer=excluded.customer, service_name=excluded.service_name, status=excluded.status, transaction_date=excluded.transaction_date, pulled_at=now()`
          loaded++
        }
      })
      // stage 1 of the billing pipeline: stamp ION invoice numbers/amounts on
      // the month's promises + project processing_status (pending -> ion_matched
      // | needs_review). Rides here so the UI's Refresh button gives stamped
      // statuses as soon as the report lands, not on the next hourly reconcile.
      const [m] = await sql`select billing_audit.match_promises_to_ion(${b.monthDate}) as n`
      ionStamped = m?.n ?? 0
      await sql`select billing_audit.project_maint_processing_status(${b.monthDate})`
    } finally { await sql.end().catch(() => {}) }
  }
  return { month, parsed_rows: recs.length, distinct_tasks: new Set(recs.map((r) => r.ion_task_id)).size,
    total_amt_usd: Math.round(recs.reduce((n, r) => n + r.amt_cents, 0)) / 100, loaded: (!dry_run && load) ? loaded : "skipped",
    ion_stamped: (!dry_run && load) ? ionStamped : "skipped", sample: recs.slice(0, 3) }
}

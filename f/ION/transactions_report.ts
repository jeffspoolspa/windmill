//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// ION "All Transactions" report (TransactionType=Tasks) for a month, via RAW session fetch -- no
// browser. The endpoint /reports/_xls/allTransactions.cfm is SESSION-primed (unlike the URL-param
// work-order report), so we must: GET /reports/transactionRpt.cfm (establish session) -> POST it the
// criteria (prime) -> GET the XLS, carrying a cookie jar across all three (ColdFusion evolves the
// session cookie; a static header + naive POST 500s). Columns: Office|Customer|Address|Route|
// CustomerType|Transaction ID|Transaction Date|Transaction Type|Service Name|Completed By|Amount|
// Status|Status Date|Additional Info ("Task <ion_task_id>"). dry_run (default) parses only; load=true
// replaces that month in billing_audit.ion_task_transactions. `month` = "YYYY-MM".

function bounds(month: string) {
  const m = month.match(/^(\d{4})-(\d{2})$/); if (!m) throw new Error(`month must be YYYY-MM, got ${month}`)
  const y = +m[1], mo = +m[2]
  const iso = (yy: number, mm: number, dd: number) => `${yy}-${String(mm).padStart(2, "0")}-${String(dd).padStart(2, "0")}`
  const last = new Date(Date.UTC(mo === 12 ? y + 1 : y, mo === 12 ? 0 : mo, 0)).getUTCDate()
  return { start: iso(y, mo, 1), end: iso(y, mo, last), us_start: `${m[2]}/01/${m[1]}`, us_end: `${m[2]}/${String(last).padStart(2, "0")}/${m[1]}`, monthDate: iso(y, mo, 1) }
}
const toIsoDate = (mdy: string) => { const m = String(mdy).match(/(\d{1,2})\/(\d{1,2})\/(\d{4})/); return m ? `${m[3]}-${m[1].padStart(2, "0")}-${m[2].padStart(2, "0")}` : null }

export async function main(month: string, dry_run: boolean = true, load: boolean = false) {
  const b = bounds(month)
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin

  const jar = new Map<string, string>()
  for (const c of (s.cookies || [])) jar.set(c.name, c.value)
  const cookieStr = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
  const merge = (res: any) => { for (const line of ((res.headers.getSetCookie?.() || []) as string[])) { const kv = line.split(";")[0]; const i = kv.indexOf("="); if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim()) } }
  const H = () => ({ Cookie: cookieStr(), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" })

  const r1 = await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H(), redirect: "manual" }); merge(r1); await r1.text()
  const body = `rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=${b.start}&rptEnd=${b.end}&ServiceItem=&WOItem=&WorkFrom=${encodeURIComponent(b.us_start)}&WorkTo=${encodeURIComponent(b.us_end)}`
  const r2 = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: { ...H(), "Content-Type": "application/x-www-form-urlencoded", Referer: `${o}/reports/transactionRpt.cfm` }, body, redirect: "manual" }); merge(r2); await r2.text()
  const r3 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: H(), redirect: "manual" }); merge(r3)
  const xls = await r3.text()
  if (r3.status !== 200) throw new Error(`XLS fetch failed: status ${r3.status}`)

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

  let loaded = 0
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
    } finally { await sql.end().catch(() => {}) }
  }
  return { month, xls_status: r3.status, parsed_rows: recs.length, distinct_tasks: new Set(recs.map((r) => r.ion_task_id)).size,
    total_amt_usd: Math.round(recs.reduce((n, r) => n + r.amt_cents, 0)) / 100, loaded: (!dry_run && load) ? loaded : "skipped", sample: recs.slice(0, 4) }
}

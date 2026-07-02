//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: pull the ION transaction report (Tasks) via RAW FETCH -- the trick vs a naive POST is a
// cookie jar carried across GET transactionRpt -> POST transactionRpt (prime session) -> GET the XLS.
// Parses rows to { ion_task_id (from "Task <id>"), amt_cents }. No browser.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin

  const jar = new Map<string, string>()
  for (const c of (s.cookies || [])) jar.set(c.name, c.value)
  const cookieStr = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
  const merge = (res: any) => { const sc = (res.headers.getSetCookie?.() || []) as string[]; for (const line of sc) { const kv = line.split(";")[0]; const i = kv.indexOf("="); if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim()) } }
  const H = () => ({ Cookie: cookieStr(), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" })

  const start = "2026-06-01", end = "2026-06-30", wf = "06/01/2026", wt = "06/30/2026"
  // 1) GET the form (establish/refresh session)
  const r1 = await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H(), redirect: "manual" }); merge(r1); await r1.text()
  // 2) POST criteria (prime session)
  const body = `rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=${start}&rptEnd=${end}&ServiceItem=&WOItem=&WorkFrom=${encodeURIComponent(wf)}&WorkTo=${encodeURIComponent(wt)}`
  const r2 = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: { ...H(), "Content-Type": "application/x-www-form-urlencoded", Referer: `${o}/reports/transactionRpt.cfm` }, body, redirect: "manual" }); merge(r2)
  const postBody = await r2.text()
  // 3) GET the XLS
  const r3 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: H(), redirect: "manual" }); merge(r3)
  const xls = await r3.text()

  const table = parse(xls).querySelector("table")
  const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))) : []
  // parse task id + amount from each data row
  const parsed: any[] = []
  for (const r of rows) {
    const joined = r.join(" | ")
    const task = joined.match(/Task\s+(\d+)/)?.[1]
    const amt = joined.match(/\$([0-9,]+\.[0-9]{2})/)?.[1]
    if (task && amt) parsed.push({ ion_task_id: task, amt_cents: Math.round(parseFloat(amt.replace(/,/g, "")) * 100) })
  }
  return { post_status: r2.status, post_is_form: /_CF_checkrpt/.test(postBody) && !/allTransactions/i.test(postBody),
    xls_status: r3.status, xls_len: xls.length, parsed_rows: parsed.length, sample: parsed.slice(0, 5) }
}

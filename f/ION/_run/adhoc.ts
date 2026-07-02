//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: pull the transaction report with TransactionType=0 (ALL) for June, and dump every row for
// WAITES / FREDERICA / ATLANTIC BREEZE / BARTH -- to confirm separate-consumables chem lands as its own
// Consumables transaction (and how it ties to the task).
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const jar = new Map<string, string>()
  for (const c of (s.cookies || [])) jar.set(c.name, c.value)
  const cookieStr = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
  const merge = (res: any) => { for (const line of ((res.headers.getSetCookie?.() || []) as string[])) { const kv = line.split(";")[0]; const i = kv.indexOf("="); if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim()) } }
  const H = () => ({ Cookie: cookieStr(), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" })

  const r1 = await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H(), redirect: "manual" }); merge(r1); await r1.text()
  const body = `rptOffice=0&CustomerType=0&TransactionType=0&SyncStatus=0&Routes=0&rptStart=2026-06-01&rptEnd=2026-06-30&ServiceItem=&WOItem=&WorkFrom=${encodeURIComponent("06/01/2026")}&WorkTo=${encodeURIComponent("06/30/2026")}`
  const r2 = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: { ...H(), "Content-Type": "application/x-www-form-urlencoded", Referer: `${o}/reports/transactionRpt.cfm` }, body, redirect: "manual" }); merge(r2); await r2.text()
  const r3 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: H(), redirect: "manual" }); merge(r3)
  const xls = await r3.text()
  const table = parse(xls).querySelector("table")
  const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))) : []
  const hi = rows.findIndex((r: string[]) => r.some((c) => /^Transaction ID$/i.test(c)))
  const header = rows[hi]
  const names = ["WAITES", "FREDERICA", "ATLANTIC BREEZE", "BARTH"]
  const hits = rows.filter((r: string[]) => names.some((n) => r.some((c) => c.toUpperCase().includes(n))))
  return { total_rows: rows.length, header: header?.join(" | "), matched: hits.map((r: string[]) => r.join(" | ")) }
}

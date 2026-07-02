//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: isolate the transaction-report 500 -- pull Tasks for a few date ranges and report status
// + row count, to tell whether it's month/data-specific (WAITES) or endpoint-wide.
async function pull(o: string, seed: any[], start: string, end: string, wf: string, wt: string) {
  const jar = new Map<string, string>(); for (const c of seed) jar.set(c.name, c.value)
  const cookieStr = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
  const merge = (res: any) => { for (const line of ((res.headers.getSetCookie?.() || []) as string[])) { const kv = line.split(";")[0]; const i = kv.indexOf("="); if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim()) } }
  const H = () => ({ Cookie: cookieStr(), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" })
  const r1 = await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H(), redirect: "manual" }); merge(r1); await r1.text()
  const body = `rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=${start}&rptEnd=${end}&ServiceItem=&WOItem=&WorkFrom=${encodeURIComponent(wf)}&WorkTo=${encodeURIComponent(wt)}`
  const r2 = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: { ...H(), "Content-Type": "application/x-www-form-urlencoded", Referer: `${o}/reports/transactionRpt.cfm` }, body, redirect: "manual" }); merge(r2); await r2.text()
  const r3 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: H(), redirect: "manual" }); const xls = await r3.text()
  const table = parse(xls).querySelector("table")
  const rows = table ? table.querySelectorAll("tr").filter((tr: any) => tr.text.trim()).length : 0
  return { status: r3.status, len: xls.length, rows }
}
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin, seed = s.cookies || []
  return {
    may_2026: await pull(o, seed, "2026-05-01", "2026-05-31", "05/01/2026", "05/31/2026"),
    june_full: await pull(o, seed, "2026-06-01", "2026-06-30", "06/01/2026", "06/30/2026"),
    june_1_15: await pull(o, seed, "2026-06-01", "2026-06-15", "06/01/2026", "06/15/2026"),
  }
}

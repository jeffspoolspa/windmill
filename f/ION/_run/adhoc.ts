//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: diagnose why the transaction XLS now 500s -- report status/len of each of GET->POST->GET,
// whether the POST re-rendered the form, and a snippet of the XLS body.
function cookieHeader(cookies: any[], origin: string) {
  const host = new URL(origin).hostname
  return cookies.filter((c: any) => { const d = (c.domain || "").replace(/^\./, ""); return host === d || host.endsWith("." + d) }).map((c: any) => `${c.name}=${c.value}`).join("; ")
}
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const jar = new Map<string, string>()
  for (const c of (s.cookies || [])) jar.set(c.name, c.value)
  const cookieStr = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
  const merge = (res: any) => { for (const line of ((res.headers.getSetCookie?.() || []) as string[])) { const kv = line.split(";")[0]; const i = kv.indexOf("="); if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim()) } }
  const H = () => ({ Cookie: cookieStr(), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" })

  const cookieCount = (s.cookies || []).length
  const r1 = await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H(), redirect: "manual" }); const b1 = await r1.text(); merge(r1)
  const body = `rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=2026-06-01&rptEnd=2026-06-30&ServiceItem=&WOItem=&WorkFrom=${encodeURIComponent("06/01/2026")}&WorkTo=${encodeURIComponent("06/30/2026")}`
  const r2 = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: { ...H(), "Content-Type": "application/x-www-form-urlencoded", Referer: `${o}/reports/transactionRpt.cfm` }, body, redirect: "manual" }); const b2 = await r2.text(); merge(r2)
  const r3 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: H(), redirect: "manual" }); const b3 = await r3.text()
  return {
    session_cookie_count: cookieCount, origin: o,
    r1_get: { status: r1.status, len: b1.length, is_login: /login|password/i.test(b1.slice(0, 500)) },
    r2_post: { status: r2.status, len: b2.length, is_form: /_CF_checkrpt/.test(b2) },
    r3_xls: { status: r3.status, len: b3.length, snippet: b3.slice(0, 300) },
  }
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: work-order-report pattern -- GET the picker (transactionRpt.cfm) WITH criteria as URL params
// so it primes session/renders the XLS link, then GET allTransactions.cfm. Reports whether the picker
// GET surfaced the XLS link and whether the XLS then returns data.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const deent = (u: string) => u.replace(/&#x2f;/gi, "/").replace(/&amp;/gi, "&")

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H: any = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", Referer: `${o}/main.cfm` }
  const get = async (u: string) => { const r = await fetch(`${o}${u}`, { headers: H, redirect: "manual" }); return { status: r.status, body: await r.text() } }

  const crit = "rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=2026-06-01&rptEnd=2026-06-30&WorkFrom=06/01/2026&WorkTo=06/30/2026&ServiceItem=&WOItem="
  const out: any = {}
  // 1) GET-prime the picker with criteria
  const picker = await get(`/reports/transactionRpt.cfm?${crit}`)
  out.picker_status = picker.status
  out.picker_len = picker.body.length
  out.picker_has_xls = /allTransactions/i.test(picker.body)
  out.picker_xls_hrefs = [...new Set([...picker.body.matchAll(/href="([^"]*_xls[^"]*)"/gi)].map((m) => deent(m[1])))].slice(0, 6)

  // 2) then fetch the XLS (session should now be primed)
  const xls = await get(`/reports/_xls/allTransactions.cfm`)
  const table = parse(xls.body).querySelector("table")
  const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))).filter((x: string[]) => x.some((c) => c)) : []
  out.xls_status = xls.status
  out.xls_len = xls.body.length
  out.xls_rows = rows.length
  out.xls_head = rows.slice(0, 6).map((x: string[]) => x.join(" | "))
  return out
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: find the working allTransactions.cfm request -- try the full criteria field set as query
// params in several date-format variants (report likely reads URL/FORM scope directly).
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H: any = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", Referer: `${o}/reports/transactionRpt.cfm` }

  const base = { rptOffice: "0", CustomerType: "0", TransactionType: "Tasks", SyncStatus: "0", Routes: "0", ServiceItem: "", WOItem: "" }
  const variants: Record<string, any>[] = [
    { ...base, rptStart: "2026-06-01", rptEnd: "2026-06-30", WorkFrom: "06/01/2026", WorkTo: "06/30/2026" },
    { ...base, WorkFrom: "06/01/2026", WorkTo: "06/30/2026" },
    { ...base, rptStart: "06/01/2026", rptEnd: "06/30/2026" },
    { ...base, StartDate: "06/01/2026", EndDate: "06/30/2026", WorkFrom: "06/01/2026", WorkTo: "06/30/2026" },
  ]
  const results: any[] = []
  for (const v of variants) {
    const qs = new URLSearchParams(v).toString()
    try {
      const r = await fetch(`${o}/reports/_xls/allTransactions.cfm?${qs}`, { headers: H, redirect: "manual" })
      const body = await r.text()
      const table = parse(body).querySelector("table")
      const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))).filter((x: string[]) => x.some((c) => c)) : []
      results.push({ qs, status: r.status, len: body.length, rows: rows.length, head: rows.slice(0, 5).map((x: string[]) => x.join(" | ")) })
    } catch (e: any) { results.push({ qs, error: String(e?.message ?? e).slice(0, 120) }) }
  }
  return { results }
}

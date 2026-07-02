//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: prove the transaction-report pull -- POST criteria to /reports/transactionRpt.cfm to prime
// the session, then GET /reports/_xls/allTransactions.cfm (XLS). Shows header + sample + row count.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const cookie = cookieHeader(s)
  const H: any = { Cookie: cookie, "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` }

  // 1) PRIME: POST criteria (form-urlencoded) -> transactionRpt.cfm
  const form = new URLSearchParams({
    rptOffice: "0", CustomerType: "0", TransactionType: "Tasks", SyncStatus: "0", Routes: "0",
    rptStart: "2026-06-01", rptEnd: "2026-06-30", WorkFrom: "06/01/2026", WorkTo: "06/30/2026",
    ServiceItem: "", WOItem: "",
  })
  const primeRes = await fetch(`${o}/reports/transactionRpt.cfm`, {
    method: "POST", headers: { ...H, "Content-Type": "application/x-www-form-urlencoded" }, body: form.toString(), redirect: "manual",
  })
  const primeStatus = primeRes.status
  await primeRes.text()

  // 2) FETCH XLS
  const xls = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: H, redirect: "manual" })
  const xlsStatus = xls.status
  const body = await xls.text()
  const table = parse(body).querySelector("table")
  const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))) : []
  const nonEmpty = rows.filter((r: string[]) => r.some((c) => c))
  return {
    prime_status: primeStatus, xls_status: xlsStatus, xls_len: body.length,
    total_rows: nonEmpty.length, header_rows: nonEmpty.slice(0, 6).map((r: string[]) => r.join(" | ")),
    sample: nonEmpty.slice(6, 12).map((r: string[]) => r.join(" | ")),
  }
}

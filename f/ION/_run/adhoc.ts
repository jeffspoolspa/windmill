//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: replicate the real transaction-report flow -- POST criteria to transactionRpt.cfm (renders
// the results page + primes session), extract the actual allTransactions XLS href from that response,
// then GET it. Reports whether the POST returned a results page (has XLS link) or bounced to the form.
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
  const cookie = cookieHeader(s)
  const H: any = { Cookie: cookie, "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", Referer: `${o}/reports/transactionRpt.cfm` }

  const form = new URLSearchParams({
    rptOffice: "0", CustomerType: "0", TransactionType: "Tasks", SyncStatus: "0", Routes: "0",
    rptStart: "2026-06-01", rptEnd: "2026-06-30", WorkFrom: "06/01/2026", WorkTo: "06/30/2026",
    ServiceItem: "", WOItem: "",
  })
  const postRes = await fetch(`${o}/reports/transactionRpt.cfm`, {
    method: "POST", headers: { ...H, "Content-Type": "application/x-www-form-urlencoded" }, body: form.toString(), redirect: "manual",
  })
  const postStatus = postRes.status
  const postBody = await postRes.text()
  // pull the XLS href(s) from the POST response
  const hrefs = [...new Set([...postBody.matchAll(/href="([^"]*_xls[^"]*allTransactions[^"]*)"/gi)].map((m) => deent(m[1])))]
  const anyXls = [...new Set([...postBody.matchAll(/href="([^"]*_xls[^"]*)"/gi)].map((m) => deent(m[1])))].slice(0, 6)

  let fetched: any = null
  if (hrefs.length) {
    const u = hrefs[0].startsWith("http") ? hrefs[0] : `${o}${hrefs[0]}`
    const r = await fetch(u, { headers: H, redirect: "manual" })
    const body = await r.text()
    const table = parse(body).querySelector("table")
    const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))).filter((x: string[]) => x.some((c) => c)) : []
    fetched = { url: hrefs[0], status: r.status, len: body.length, rows: rows.length, head: rows.slice(0, 6).map((x: string[]) => x.join(" | ")) }
  }
  return {
    post_status: postStatus, post_len: postBody.length,
    post_is_results_page: /allTransactions/i.test(postBody), xls_hrefs: hrefs, any_xls_hrefs: anyXls, fetched,
  }
}

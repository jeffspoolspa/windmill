//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: discover how /reports/_xls/allTransactions.cfm is parameterized. Mine transactionRpt.cfm
// for the criteria form (field names + action), then probe the XLS endpoint with June date guesses.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const grab = async (u: string) => { const r = await fetch(`${o}${u}`, { headers: H, redirect: "manual" }); return { status: r.status, body: await r.text() } }

  // 1) mine the criteria form on transactionRpt.cfm
  const pg = await grab("/reports/transactionRpt.cfm")
  const root = parse(pg.body)
  const fields: any[] = []
  for (const el of root.querySelectorAll("input, select")) {
    const name = el.getAttribute("name"); if (!name) continue
    let opts: string[] | undefined
    if (el.tagName === "SELECT") opts = el.querySelectorAll("option").slice(0, 8).map((op: any) => `${op.getAttribute("value")}=${op.text.trim()}`)
    fields.push({ tag: el.tagName, name, type: el.getAttribute("type") || null, value: el.getAttribute("value") || null, options: opts })
  }
  const forms = root.querySelectorAll("form").map((f: any) => ({ action: f.getAttribute("action"), method: f.getAttribute("method") }))

  // 2) probe the XLS endpoint a few ways; report status + first row(s) of the parsed table
  const probes = [
    "/reports/_xls/allTransactions.cfm",
    "/reports/_xls/allTransactions.cfm?StartDate=06/01/2026&EndDate=06/30/2026",
    "/reports/_xls/allTransactions.cfm?startDate=2026-06-01&endDate=2026-06-30",
    "/reports/_xls/allTransactions.cfm?rptStart=2026-06-01&rptEnd=2026-06-30",
  ]
  const results: any[] = []
  for (const u of probes) {
    try {
      const r = await grab(u)
      const t = parse(r.body).querySelector("table")
      const rows = t ? t.querySelectorAll("tr").slice(0, 3).map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " ")).join(" | ")) : []
      results.push({ url: u, status: r.status, len: r.body.length, first_rows: rows })
    } catch (e: any) { results.push({ url: u, error: String(e?.message ?? e).slice(0, 120) }) }
  }
  return { form_status: pg.status, forms, fields, probes: results }
}

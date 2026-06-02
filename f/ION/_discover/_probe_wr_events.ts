//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const get = (url: string) => fetch(`${o}${url}`, { headers: H, redirect: "manual" }).then(r => r.text())
  const post = (url: string, body: string) => fetch(`${o}${url}`, {
    method: "POST",
    headers: { ...H, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm`, Origin: o },
    body, redirect: "manual",
  }).then(r => r.text())
  const fld = (root: any, name: string) => (root.querySelector(`input[name="${name}"]`)?.getAttribute("value") ?? "")

  const cid = "2367390"
  await get(`/customers/customerTabs.cfm?customerid=${cid}`)
  const logHtml = await post(`/customers/logs/loglist.cfm`, "limit=400")
  const entries: { date: string; logId: string }[] = []
  for (const a of parse(logHtml).querySelectorAll('a[href*="addLog.cfm"]')) {
    const m = (a.getAttribute("href") || "").match(/LogID=(\d+)/)
    const dm = a.text.match(/(\d{2})\/(\d{2})\/(\d{4})/)
    if (m && dm) entries.push({ date: `${dm[3]}-${dm[1]}-${dm[2]}`, logId: m[1] })
  }
  const may = entries.filter(e => e.date >= "2026-05-01" && e.date <= "2026-05-31")

  // per (EventID) -> per (TaskInvoiceID) -> sorted distinct dates
  const out: Record<string, Record<string, Set<string>>> = {}
  for (const e of may) {
    const root = parse(await get(`/tasks/addLog.cfm?LogID=${e.logId}&Source=ServiceLog`))
    const ev = fld(root, "EventID") || "none"
    const inv = fld(root, "TaskInvoiceID") || "NULL"
    ;((out[ev] ??= {})[inv] ??= new Set()).add(e.date)
  }
  const result: Record<string, Record<string, string[]>> = {}
  for (const [ev, byInv] of Object.entries(out)) {
    result[ev] = {}
    for (const [inv, ds] of Object.entries(byInv)) result[ev][inv] = [...ds].sort()
  }
  return { ion_customerid: cid, may_log_entries: may.length, event_to_invoice_to_dates: result }
}

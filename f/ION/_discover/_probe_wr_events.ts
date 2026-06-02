//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Throwaway proof: for WINDING RIVER, pull every May service-log entry's EventID
// (the authoritative ION task id) and tally distinct (EventID, date) per task --
// the true billable-visit count -- to compare against the invoice lines.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const up = (x: string) => (x || "").toUpperCase()

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

  // 1. resolve WINDING RIVER customer id
  const listHtml = await get(`/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=${encodeURIComponent("WINDING RIVER")}&reset=1`)
  let cid: string | null = null
  for (const a of parse(listHtml).querySelectorAll('a[href*="customerTabs"]')) {
    const m = (a.getAttribute("href") || "").match(/customerid=(\d+)/)
    if (m && up(a.text).includes("WINDING RIVER")) { cid = m[1]; break }
    if (m && !cid) cid = m[1]
  }
  if (!cid) return { error: "no WINDING RIVER customer found" }

  // 2. loglist -> entries (date, LogID)
  await get(`/customers/customerTabs.cfm?customerid=${cid}`)
  const logHtml = await post(`/customers/logs/loglist.cfm`, "limit=400")
  const entries: { date: string; logId: string }[] = []
  for (const a of parse(logHtml).querySelectorAll('a[href*="addLog.cfm"]')) {
    const m = (a.getAttribute("href") || "").match(/LogID=(\d+)/)
    const dm = a.text.match(/(\d{2})\/(\d{2})\/(\d{4})/)
    if (m && dm) entries.push({ date: `${dm[3]}-${dm[1]}-${dm[2]}`, logId: m[1] })
  }
  const may = entries.filter(e => e.date >= "2026-05-01" && e.date <= "2026-05-31")

  // 3. EventID per LogID
  const byEvent: Record<string, { logs: number; dates: Set<string> }> = {}
  for (const e of may) {
    const ah = await get(`/tasks/addLog.cfm?LogID=${e.logId}&Source=ServiceLog`)
    const inp = parse(ah).querySelector('input[name="EventID"]')
    const ev = (inp?.getAttribute("value") || "none")
    ;(byEvent[ev] ??= { logs: 0, dates: new Set() })
    byEvent[ev].logs++
    byEvent[ev].dates.add(e.date)
  }

  const tally: Record<string, { log_entries: number; distinct_days: number }> = {}
  let totalDistinct = 0
  for (const [ev, d] of Object.entries(byEvent)) {
    tally[ev] = { log_entries: d.logs, distinct_days: d.dates.size }
    totalDistinct += d.dates.size
  }
  return {
    ion_customerid: cid,
    may_log_entries: may.length,
    distinct_event_date_pairs: totalDistinct,
    by_event_id: tally,
  }
}

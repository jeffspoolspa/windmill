//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Authoritative visit->task linker, step 2. Per task-less visit: resolve its ION
// customer (ion_cust_hint, else customerlist search by name+street), fetch the
// customer's loglist (build a date -> LogID map), find the LogID for the visit's
// scheduled_date, then open addLog.cfm?LogID=X and read EventID -- the ION task id
// ION itself recorded for that service log. SEQUENTIAL (session customer context).

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
const firstTwo = (s: string) => up(s).trim().split(/\s+/).slice(0, 2).join(" ")
const searchTerm = (name: string) => (name || "").split(/[,(]/)[0].trim().split(/\s+/).pop() || name
const isoToUS = (iso: string) => { const [y, m, d] = (iso || "").split("-"); return y ? `${m}/${d}/${y}` : "" }

export async function main(visits: any[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const cookie = cookieHeader(s)
  const H = { Cookie: cookie, "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const get = (url: string) => fetch(`${o}${url}`, { headers: H, redirect: "manual" }).then(r => r.text())
  const post = (url: string, body: string) => fetch(`${o}${url}`, {
    method: "POST",
    headers: { ...H, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm`, Origin: o },
    body, redirect: "manual",
  }).then(r => r.text())

  async function resolveCustId(v: any): Promise<string | null> {
    if (v.ion_cust_hint) return String(v.ion_cust_hint)
    const html = await get(`/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=${encodeURIComponent(searchTerm(v.name))}&reset=1`)
    const root = parse(html)
    const cands: { cid: string; text: string }[] = []
    for (const a of root.querySelectorAll('a[href*="customerTabs"]')) {
      const m = (a.getAttribute("href") || "").match(/customerid=(\d+)/)
      if (!m) continue
      let row: any = a
      for (let k = 0; k < 5 && row && row.tagName !== "TR"; k++) row = row.parentNode
      cands.push({ cid: m[1], text: up(row ? row.text : a.text) })
    }
    const tok = firstTwo(v.street)
    const chosen = (tok && cands.find(c => c.text.includes(tok))) || cands[0]
    return chosen?.cid ?? null
  }

  // date -> [LogID] map from a customer's loglist
  async function logMap(custid: string): Promise<Record<string, string[]>> {
    await get(`/customers/customerTabs.cfm?customerid=${custid}`)
    const html = await post(`/customers/logs/loglist.cfm`, "limit=200")
    const map: Record<string, string[]> = {}
    for (const a of parse(html).querySelectorAll('a[href*="addLog.cfm"]')) {
      const m = (a.getAttribute("href") || "").match(/LogID=(\d+)/)
      const dm = a.text.match(/(\d{2})\/(\d{2})\/(\d{4})/)
      if (m && dm) (map[`${dm[3]}-${dm[1]}-${dm[2]}`] ??= []).push(m[1])
    }
    return map
  }

  async function eventIdForLog(logId: string): Promise<string | null> {
    const html = await get(`/tasks/addLog.cfm?LogID=${logId}&Source=ServiceLog`)
    const inp = parse(html).querySelector('input[name="EventID"]')
    const v = inp?.getAttribute("value")
    return v && /^\d+$/.test(v) ? v : null
  }

  const out: any[] = []
  const cache: Record<string, Record<string, string[]>> = {}
  for (const v of visits) {
    const rec: any = { visit_id: v.visit_id, sl: v.service_location_id, date: v.scheduled_date || v.visit_date }
    try {
      const cid = await resolveCustId(v)
      if (!cid) { rec.error = "no ion customer"; out.push(rec); continue }
      rec.ion_customerid = cid
      if (!cache[cid]) cache[cid] = await logMap(cid)
      const logs = cache[cid][rec.date] || cache[cid][v.visit_date] || []
      if (!logs.length) { rec.error = "no log on date"; out.push(rec); continue }
      rec.log_id = logs[0]
      rec.event_id = await eventIdForLog(logs[0])
      if (!rec.event_id) rec.error = "no EventID in addLog"
    } catch (e: any) { rec.error = String(e?.message ?? e).slice(0, 140) }
    out.push(rec)
  }
  return { visits: visits.length, links: out }
}

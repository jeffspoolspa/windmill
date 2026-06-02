//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Capture-nonactive step 2. For each task-less-visit target {service_location_id,
// name, street}, search ION customerlist by a name token, pick the customer whose
// row matches the street, and pull the FULL taskList (getCustomerTasks returns
// active + expired/one-time). Returns rows the Python step c upserts + links.
// SEQUENTIAL (customerTabs sets one server-side customer per session).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getCustomerTasks } from "/f/ION/_lib/customer_tasks"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const up = (x: string) => (x || "").toUpperCase()
const firstTwo = (s: string) => up(s).trim().split(/\s+/).slice(0, 2).join(" ")
const searchTerm = (name: string) => (name || "").split(/[,(]/)[0].trim().split(/\s+/).pop() || name

export async function main(targets: any[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const cookie = cookieHeader(s)
  const out: any[] = []
  for (const t of targets) {
    const rec: any = { service_location_id: t.service_location_id, name: t.name, street: t.street }
    try {
      const term = searchTerm(t.name)
      const html = await (await fetch(`${o}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=${encodeURIComponent(term)}&reset=1`,
        { headers: { Cookie: cookie, "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }, redirect: "manual" })).text()
      const root = parse(html)
      const cands: { cid: string; text: string }[] = []
      for (const a of root.querySelectorAll('a[href*="customerTabs"]')) {
        const m = (a.getAttribute("href") || "").match(/customerid=(\d+)/)
        if (!m) continue
        let row: any = a
        for (let k = 0; k < 5 && row && row.tagName !== "TR"; k++) row = row.parentNode
        cands.push({ cid: m[1], text: up(row ? row.text : a.text) })
      }
      const tok = firstTwo(t.street)
      const chosen = (tok && cands.find(c => c.text.includes(tok))) || cands[0]
      rec.candidate_count = cands.length
      if (!chosen) { rec.matched = false; out.push(rec); continue }
      rec.ion_customerid = chosen.cid
      rec.matched = true
      rec.tasks = await getCustomerTasks(s, chosen.cid)
    } catch (e: any) {
      rec.error = String(e?.message ?? e).slice(0, 140)
    }
    out.push(rec)
  }
  return { targets: targets.length, rows: out }
}

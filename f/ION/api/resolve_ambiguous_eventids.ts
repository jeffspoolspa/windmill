//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// EventID correction, step 2. For each rate-ambiguous location, pull the customer's
// service logs in [start_date, end_date) and read the ION-recorded EventID + clock
// times per log via addLog.cfm. EventID is the ground-truth ion_task_id that splits
// same-rate tasks (e.g. WINDING RIVER morning vs afternoon chem). serviceable is
// derived from the same form (timein==timeout zero-duration OR an explicit failure
// reason) -- matches the bulk-report Start==End signal. SEQUENTIAL per customer
// (ION's session customer-context is single-state).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const val = (r: any, name: string) => { const i = r.querySelector(`input[name="${name}"]`); return i ? (i.getAttribute("value") || "") : "" }

type Target = { service_location_id: number; ion_customerid: string | number; start_date: string; end_date: string }

export async function main(targets: Target[] = []) {
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

  const links: any[] = []
  const perTarget: any[] = []
  for (const t of targets) {
    const cid = String(t.ion_customerid)
    const rec: any = { service_location_id: t.service_location_id, ion_customerid: cid, logs: 0, errors: 0 }
    try {
      await get(`/customers/customerTabs.cfm?customerid=${cid}`)
      const listHtml = await post(`/customers/logs/loglist.cfm`, "limit=400")
      const logIds: { logId: string; date: string }[] = []
      for (const a of parse(listHtml).querySelectorAll('a[href*="addLog.cfm"]')) {
        const m = (a.getAttribute("href") || "").match(/LogID=(\d+)/)
        const dm = a.text.match(/(\d{2})\/(\d{2})\/(\d{4})/)
        if (!m || !dm) continue
        const iso = `${dm[3]}-${dm[1]}-${dm[2]}`
        if (iso >= t.start_date && iso < t.end_date) logIds.push({ logId: m[1], date: iso })
      }
      for (const { logId, date } of logIds) {
        try {
          const r = parse(await get(`/tasks/addLog.cfm?LogID=${logId}&Source=ServiceLog`))
          const eventId = val(r, "EventID")
          const tin = val(r, "timeinvalue"), tout = val(r, "timeoutvalue")
          const failure = val(r, "OriginalFailureID")
          const sched = val(r, "ScheduledDate") // MM/DD/YYYY
          const schedIso = sched && sched.length >= 10 ? `${sched.slice(6, 10)}-${sched.slice(0, 2)}-${sched.slice(3, 5)}` : date
          const serviceable = !(tin && tin === tout) && !failure
          if (eventId) {
            links.push({ service_location_id: t.service_location_id, scheduled_date: schedIso, timein: tin || null, event_id: eventId, serviceable })
            rec.logs++
          } else { rec.errors++ }
        } catch { rec.errors++ }
      }
    } catch (e: any) { rec.error = String(e?.message ?? e).slice(0, 140) }
    perTarget.push(rec)
  }
  return { targets: targets.length, link_count: links.length, per_target: perTarget, links }
}

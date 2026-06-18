//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

const PAIRS = [
  { event: "5208146", log: "34879057", visits: 594 },
  { event: "5310468", log: "35058992", visits: 573 },
  { event: "5210279", log: "33014773", visits: 40 },
  { event: "5430635", log: "34867666", visits: 40 },
  { event: "1704388", log: "32160233", visits: 1 },
]

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
  const get = (url: string) => fetch(`${o}${url}`, { headers: H, redirect: "manual" }).then((r) => r.text())

  const out: any[] = []
  for (const p of PAIRS) {
    const rec: any = { event_id: p.event, log_id: p.log, visits: p.visits }
    try {
      const logHtml = await get(`/tasks/addLog.cfm?LogID=${p.log}&Source=ServiceLog`)
      const root = parse(logHtml)
      const cid = root.querySelector('input[name="CustomerID"]')?.getAttribute("value") || (logHtml.match(/CustomerID=(\d+)/) || [])[1]
      const eidOnLog = root.querySelector('input[name="EventID"]')?.getAttribute("value")
      rec.ion_customer_id = cid
      rec.event_on_log = eidOnLog
      rec.event_matches = eidOnLog === p.event
      if (cid) {
        const { detail } = await getTaskDetail(s, p.event, cid)
        rec.serviceType = detail.serviceType?.text
        rec.serviceRepeat = detail.serviceRepeat?.text
        rec.startsOn = detail.startsOn
        rec.endsOn = detail.endsOn
        rec.proposed_status = !detail.endsOn || detail.endsOn >= "2026-06-18" ? "active" : "closed"
        rec.perDayTech = detail.perDayTech?.filter((d: any) => d.techId).map((d: any) => `${d.dayName}:${d.techName}`)
        rec.note = (detail.taskNote || "").slice(0, 50)
      }
    } catch (e: any) { rec.error = String(e?.message ?? e).slice(0, 180) }
    out.push(rec)
  }
  return out
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Canonical per-DAY service-log enumerator. ONE call to customerLogDetails.cfm
// (global, all customers) returns every scheduled event + submitted log for the date,
// each row carrying the unique LogID + calendarID (-> addLog.cfm), customer name,
// service type, tech, and a status bullet (green = completed/submitted log). This is
// the discovery step of the log-based ingestion: enumerate a day's LogIDs here, then
// open addLog.cfm?LogID per COMPLETED log for the authoritative detail (EventID=task,
// scheduled date, time-in/out, TaskInvoiceID, price, consumables).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

// date_us = MM/DD/YYYY
export async function main(date_us: string, officeid: number | string = 0) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` }
  const url = `/home/customerLogDetails.cfm?officeid=${officeid}&techid=0&status=0&logset=1`
    + `&dayindexsel=${encodeURIComponent(date_us)}&dayindex=&_cf_nodebug=true&_cf_nocache=true&_cf_rc=0`
  const html = await (await fetch(`${o}${url}`, { headers: H, redirect: "manual" })).text()

  const root = parse(html)
  const logs: any[] = []
  for (const a of root.querySelectorAll('a[href*="addLog.cfm"]')) {
    const href = a.getAttribute("href") || ""
    const log = href.match(/LogID=(\d+)/)?.[1]
    if (!log) continue
    const cal = href.match(/calendarID=(\d+)/)?.[1] || null
    // row = the enclosing <tr>; its tds: [status-bullet, customer(link), service, tech, date]
    let tr: any = a
    for (let k = 0; k < 6 && tr && tr.tagName !== "TR"; k++) tr = tr.parentNode
    const tds = tr ? tr.querySelectorAll("td") : []
    const txt = (n: any) => (n ? n.text.replace(/\s+/g, " ").trim() : "")
    const bullet = tr ? (tr.querySelector("img")?.getAttribute("src") || "") : ""
    logs.push({
      log_id: log,
      calendar_id: cal,
      customer_name: txt(a),
      service_type: txt(tds[2]),
      tech: txt(tds[3]),
      date: txt(tds[4]) || date_us,
      status_bullet: bullet.split("/").pop()?.replace(/\.(png|gif)$/i, "") || null,
      completed: /green/i.test(bullet),
      addlog_url: `/tasks/addLog.cfm?calendarID=${cal || ""}&LogID=${log}&source=ServiceLog`,
    })
  }
  return {
    date: date_us,
    total: logs.length,
    completed: logs.filter((l) => l.completed).length,
    logs,
  }
}

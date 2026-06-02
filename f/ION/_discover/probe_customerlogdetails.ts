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

export async function main(date_us: string = "05/15/2026") {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` }
  const get = (url: string) => fetch(`${o}${url}`, { headers: H, redirect: "manual" }).then(r => r.text())

  const inspect = (h: string, label: string) => {
    const r = parse(h)
    const trs = r.querySelectorAll("tr")
    const dateRows = trs.map(tr => tr.text.replace(/\s+/g, " ").trim()).filter(t => /\d{1,2}\/\d{1,2}\/\d{4}/.test(t))
    // collect any task/event/log id signals from hrefs + onclicks + hidden inputs
    const idHrefs = r.querySelectorAll('a[href*="LogID"], a[href*="EventID"], a[href*="addLog"], a[href*="taskid"], a[href*="customerid"]')
      .slice(0, 6).map(a => a.getAttribute("href"))
    return {
      label, bytes: h.length, total_rows: trs.length, date_rows: dateRows.length,
      headers: r.querySelectorAll("th").map(t => t.text.trim().replace(/\s+/g, " ")).filter(Boolean).slice(0, 25),
      EventID: (h.match(/eventid/gi) || []).length, taskid: (h.match(/taskid/gi) || []).length,
      logid: (h.match(/logid/gi) || []).length, customerid: (h.match(/customerid/gi) || []).length,
      addLog_links: r.querySelectorAll('a[href*="addLog"]').length,
      id_hrefs: idHrefs,
      first_row: dateRows[0]?.slice(0, 400) || null,
      first_row_html: (r.querySelectorAll("tbody tr")[0] || trs.find(t => /\d{2}\/\d{2}\/\d{4}/.test(t.text)))?.toString().slice(0, 1200) || null,
    }
  }

  const base = `officeid=0&techid=0&status=0&logset=1&_cf_nodebug=true&_cf_nocache=true&_cf_rc=0`
  const out: any = {}
  // try date via dayindexsel / qdatesel / dayindex
  out.dayindexsel = inspect(await get(`/home/customerLogDetails.cfm?${base}&dayindexsel=${encodeURIComponent(date_us)}&dayindex=`), "dayindexsel")
  out.qdatesel    = inspect(await get(`/home/customerLogDetails.cfm?${base}&qdatesel=${encodeURIComponent(date_us)}`), "qdatesel")
  out.no_date     = inspect(await get(`/home/customerLogDetails.cfm?${base}`), "no_date(today)")
  return out
}

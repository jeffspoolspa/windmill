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
  const post = (url: string, body: string) => fetch(`${o}${url}`, { method: "POST",
    headers: { ...H, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", Origin: o }, body, redirect: "manual" }).then(r => r.text())

  // inspect the filter form
  const formHtml = await get(`/home/customerlogs.cfm?_cf_nocache=true&_cf_rc=0`)
  const froot = parse(formHtml)
  const form = froot.querySelector("form")
  const formMeta = {
    action: form?.getAttribute("action") || null,
    method: form?.getAttribute("method") || null,
    inputs: froot.querySelectorAll("input").map(i => `${i.getAttribute("name")||"?"}[${i.getAttribute("type")||""}]=${(i.getAttribute("value")||"").slice(0,20)}`).filter(x=>!x.startsWith("?")).slice(0, 40),
    selects: froot.querySelectorAll("select").map(se => `${se.getAttribute("name")}:{${se.querySelectorAll("option").slice(0,4).map(op=>op.getAttribute("value")).join(",")}}`).slice(0, 15),
    // hunt for any bind / data-source URLs (cfgrid/cflayout often load data separately)
    bind_urls: Array.from(new Set((formHtml.match(/(customerlogs[^"'\s)]*|logsrch[^"'\s)]*|\/[a-z\/]*\.cfc\?[^"'\s)]*)/gi) || []))).slice(0, 15),
  }

  const summarize = (h: string, label: string) => {
    const r = parse(h)
    const dateRows = r.querySelectorAll("tr").map(tr => tr.text.replace(/\s+/g, " ").trim()).filter(t => /\d{1,2}\/\d{1,2}\/\d{4}/.test(t))
    return { label, bytes: h.length, total_rows: r.querySelectorAll("tr").length, date_rows: dateRows.length,
      addLog_links: r.querySelectorAll('a[href*="addLog"]').length,
      EventID: (h.match(/EventID/gi)||[]).length, taskid: (h.match(/taskid/gi)||[]).length,
      eventlog_links: r.querySelectorAll('a[href*="EventID"], a[href*="eventid"], a[href*="LogID"]').slice(0,5).map(a=>a.getAttribute("href")),
      sample: dateRows[0]?.slice(0,300) || null }
  }

  const tries: any[] = []
  // submit the filter a few ways
  tries.push(summarize(await get(`/home/customerlogs.cfm?qdatesel=${encodeURIComponent(date_us)}&officeid=0&techid=0&_cf_nocache=true&_cf_rc=0`), "GET qdatesel+office+tech"))
  tries.push(summarize(await post(`/home/customerlogs.cfm`, `qdatesel=${encodeURIComponent(date_us)}&officeid=0&techid=0&logstatus=`), "POST qdatesel"))
  // common CF list endpoint sibling
  tries.push(summarize(await get(`/home/customerlogslist.cfm?qdatesel=${encodeURIComponent(date_us)}&_cf_nocache=true&_cf_rc=0`), "GET customerlogslist.cfm"))

  return { date_us, formMeta, tries }
}

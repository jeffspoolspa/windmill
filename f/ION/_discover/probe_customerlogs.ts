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

export async function main(ion_cust_id: string = "2367390") {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` }
  const get = (url: string) => fetch(`${o}${url}`, { headers: H, redirect: "manual" }).then(r => r.text())

  // prime the session's customer context, then load the customer logs container
  await get(`/customers/customerTabs.cfm?customerid=${ion_cust_id}`)
  const html = await get(`/home/customerlogs.cfm?_cf_containerId=pageContent&_cf_nodebug=true&_cf_nocache=true&_cf_rc=0`)

  const root = parse(html)
  const headers = root.querySelectorAll("th").map(th => th.text.trim().replace(/\s+/g, " ")).filter(Boolean).slice(0, 30)
  const rows = root.querySelectorAll("tr").length
  const addLogLinks = root.querySelectorAll('a[href*="addLog"]').length
  const upper = html.toUpperCase()
  const firstDataRow = root.querySelectorAll("tr").map(tr => tr.text.replace(/\s+/g, " ").trim()).filter(t => /\d{2}\/\d{2}\/\d{4}/.test(t))[0] || null
  // hunt for task-id signals
  const eventIdHits = (html.match(/EventID/gi) || []).length
  const taskHrefs = root.querySelectorAll('a[href*="ask"]').slice(0, 5).map(a => a.getAttribute("href"))
  const inputNames = Array.from(new Set(root.querySelectorAll("input").map(i => i.getAttribute("name")).filter(Boolean))).slice(0, 40)

  return {
    bytes: html.length,
    looks_like_login: /loginform|password/i.test(html) && rows < 3,
    table_headers: headers,
    row_count: rows,
    addLog_links: addLogLinks,
    has_EventID_text: eventIdHits,
    has_taskid_text: (upper.match(/TASKID/g) || []).length,
    has_eventid_attr: (upper.match(/EVENTID/g) || []).length,
    task_hrefs_sample: taskHrefs,
    input_names: inputNames,
    first_data_row: firstDataRow ? firstDataRow.slice(0, 300) : null,
    head_snippet: html.slice(0, 600),
  }
}

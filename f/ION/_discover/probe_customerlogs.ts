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

export async function main(date_us: string = "") {
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

  // 1) default GET (likely today's logs)
  const html = await get(`/home/customerlogs.cfm?_cf_containerId=pageContent&_cf_nodebug=true&_cf_nocache=true&_cf_rc=0`)
  const root = parse(html)
  const summarize = (h: string) => {
    const r = parse(h)
    return {
      bytes: h.length,
      headers: r.querySelectorAll("th").map(th => th.text.trim().replace(/\s+/g, " ")).filter(Boolean).slice(0, 30),
      rows: r.querySelectorAll("tr").length,
      addLog_links: r.querySelectorAll('a[href*="addLog"]').length,
      EventID_text: (h.match(/EventID/gi) || []).length,
      taskid_text: (h.match(/taskid/gi) || []).length,
      date_inputs: r.querySelectorAll('input[type="text"], input[type="date"]').map(i => i.getAttribute("name")).filter(Boolean).slice(0, 20),
      hidden_inputs: r.querySelectorAll('input[type="hidden"]').map(i => `${i.getAttribute("name")}=${i.getAttribute("value")}`).slice(0, 20),
      first_date_row: r.querySelectorAll("tr").map(tr => tr.text.replace(/\s+/g, " ").trim()).filter(t => /\d{2}\/\d{2}\/\d{4}/.test(t))[0]?.slice(0, 400) || null,
      sample_row_html: (r.querySelectorAll("tbody tr")[0] || r.querySelectorAll("tr")[1])?.toString().slice(0, 800) || null,
    }
  }
  const out: any = { default: summarize(html), head: html.slice(0, 500) }

  // 2) if a date was requested, try common param names
  if (date_us) {
    for (const p of ["logdate", "date", "servicedate", "txndate", "selecteddate", "startdate"]) {
      try {
        const h = await get(`/home/customerlogs.cfm?${p}=${encodeURIComponent(date_us)}&_cf_containerId=pageContent&_cf_nocache=true&_cf_rc=0`)
        out[`get_${p}`] = { rows: parse(h).querySelectorAll("tr").length, first_date_row: parse(h).querySelectorAll("tr").map(tr => tr.text.replace(/\s+/g, " ").trim()).filter(t => /\d{2}\/\d{2}\/\d{4}/.test(t))[0]?.slice(0, 120) || null }
      } catch (e: any) { out[`get_${p}`] = { error: String(e?.message ?? e).slice(0, 80) } }
    }
    out.post_date = await (async () => { try { const h = await post(`/home/customerlogs.cfm`, `logdate=${encodeURIComponent(date_us)}&date=${encodeURIComponent(date_us)}`); return { rows: parse(h).querySelectorAll("tr").length, first: parse(h).querySelectorAll("tr").map(tr=>tr.text.replace(/\s+/g," ").trim()).filter(t=>/\d{2}\/\d{2}\/\d{4}/.test(t))[0]?.slice(0,120)||null } } catch(e:any){ return {error:String(e?.message??e).slice(0,80)} } })()
  }
  return out
}

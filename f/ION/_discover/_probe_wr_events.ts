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

  const cid = "2367390"
  await get(`/customers/customerTabs.cfm?customerid=${cid}`)
  const logHtml = await post(`/customers/logs/loglist.cfm`, "limit=400")
  const entries: { date: string; logId: string }[] = []
  for (const a of parse(logHtml).querySelectorAll('a[href*="addLog.cfm"]')) {
    const m = (a.getAttribute("href") || "").match(/LogID=(\d+)/)
    const dm = a.text.match(/(\d{2})\/(\d{2})\/(\d{4})/)
    if (m && dm) entries.push({ date: `${dm[3]}-${dm[1]}-${dm[2]}`, logId: m[1] })
  }
  const may = entries.filter(e => e.date >= "2026-05-01" && e.date <= "2026-05-31")

  // For task 5333857 logs: capture candidate billable/serviceable fields + price.
  const re = /charge|servic|skip|complete|status|billable|price|amount|reason|nocharge|nobill|tasktype|eventtype|completed/i
  const per: any[] = []
  for (const e of may) {
    const root = parse(await get(`/tasks/addLog.cfm?LogID=${e.logId}&Source=ServiceLog`))
    const ev = root.querySelector('input[name="EventID"]')?.getAttribute("value") || ""
    if (ev !== "5333857") continue
    const f: Record<string, string> = {}
    for (const inp of root.querySelectorAll("input,select,textarea")) {
      const n = inp.getAttribute("name"); if (!n || !re.test(n)) continue
      // for select, read selected option text; else value
      let v = inp.getAttribute("value") || ""
      if (inp.tagName === "SELECT") {
        const sel = inp.querySelector("option[selected]"); v = sel ? (sel.text || sel.getAttribute("value") || "") : (inp.querySelector("option")?.text || "")
      }
      f[n] = String(v).slice(0, 30)
    }
    per.push({ date: e.date, logId: e.logId, fields: f })
  }
  return { task: "5333857", count: per.length, per_log: per }
}

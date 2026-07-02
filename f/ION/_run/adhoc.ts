//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// CLEAN EXPERIMENT (no browser in this run): does a RAW fetch POST prime the CF session criteria?
// Prior successes were all within minutes of a real browser submit (confounded). Here: force a FRESH
// login (new JSESSIONID, criteria definitely unprimed), then raw GET form -> raw POST June/Tasks with
// the EXACT header template captured from Chrome -> raw GET XLS (two header variants). If XLS 200 =>
// raw POST DOES prime and earlier failures were something else. If 500 => only a navigation POST primes.
const UA = "Mozilla/5.0"
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion, { forceRefresh: true }) // fresh session scope
  const o = s.ionOrigin
  const jar = new Map<string, string>()
  for (const c of (s.cookies || [])) { const d = (c.domain || "").replace(/^\./, ""); if ("ionpoolcare.com".endsWith(d) || d.endsWith("ionpoolcare.com")) jar.set(c.name, c.value) }
  const cookieStr = () => [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
  const merge = (res: any) => { for (const line of ((res.headers.getSetCookie?.() || []) as string[])) { const kv = line.split(";")[0]; const i = kv.indexOf("="); if (i > 0) jar.set(kv.slice(0, i).trim(), kv.slice(i + 1).trim()) } }
  const out: any = { seeded_cookies: [...jar.keys()] }

  // GET form, browser-like headers
  const gh = () => ({ cookie: cookieStr(), "user-agent": UA, accept: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8", "accept-language": "en-US,en;q=0.9", "sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-fetch-site": "same-origin", "upgrade-insecure-requests": "1", referer: `${o}/main.cfm` })
  const r1 = await fetch(`${o}/reports/transactionRpt.cfm`, { headers: gh(), redirect: "manual" }); merge(r1)
  out.get_form = { status: r1.status, len: (await r1.text()).length, cookies_now: [...jar.keys()] }

  // POST with the exact captured Chrome header template
  const postBody = "rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=2026-06-01&rptEnd=2026-06-30&ServiceItem=&WOItem=&WorkFrom=06%2F01%2F2026&WorkTo=06%2F30%2F2026"
  const ph: Record<string, string> = {
    cookie: cookieStr(), "user-agent": UA, origin: o, referer: `${o}/reports/transactionRpt.cfm`,
    accept: "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9", "cache-control": "max-age=0", "content-type": "application/x-www-form-urlencoded",
    priority: "u=0, i", "sec-fetch-dest": "document", "sec-fetch-mode": "navigate", "sec-fetch-site": "same-origin", "sec-fetch-user": "?1", "upgrade-insecure-requests": "1",
  }
  const r2 = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: ph, body: postBody, redirect: "manual" }); merge(r2)
  const b2 = await r2.text()
  out.post = { status: r2.status, len: b2.length, echoes_dates: b2.includes("2026-06-01"), has_xls_link: /allTransactions/i.test(b2) }

  // XLS variant A: minimal headers
  const ra = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: { cookie: cookieStr(), "user-agent": UA, accept: "text/html, */*" }, redirect: "manual" })
  const ba = await ra.text()
  out.xls_minimal = { status: ra.status, len: ba.length }
  // XLS variant B: browser-like nav headers
  const rb = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: { ...gh(), referer: `${o}/reports/transactionRpt.cfm` }, redirect: "manual" })
  const bb = await rb.text()
  const table = parse(bb).querySelector("table")
  out.xls_browserlike = { status: rb.status, len: bb.length, rows: table ? table.querySelectorAll("tr").length : 0 }
  return out
}

//bun-extra-requirements:
//playwright@1.40.0

// probe_customer_files — DISCOVERY step 4: photos aren't on addLog or the day
// grid; the remaining surface is the customer page (customerTabs.cfm sets the
// server-side customer context, then tab fragments load). Fetch the customer
// page + likely file-tab fragments and sweep for openFile()/getSignedUrl
// wiring, plus enumerate the tab fragment URLs the page declares.
import "playwright@1.40.0"

import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import type { IonResource } from "/f/ION/_lib/session"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

function sweep(html: string) {
  return {
    bytes: html.length,
    openfile: [...html.matchAll(/(openFile|downloadFile)\s*\(\s*\{[^}]{0,400}/g)]
      .slice(0, 6).map((m) => m[0].replace(/\s+/g, " ").slice(0, 400)),
    signed: html.split("\n")
      .filter((l) => /getSignedUrl|baseUrl|Containers|fileservice|amazonaws/i.test(l))
      .map((l) => l.trim().slice(0, 280)).slice(0, 10),
    file_lines: html.split("\n")
      .filter((l) => /photo|camera|attachment|upload/i.test(l) && !/\.css|favicon|logo/i.test(l))
      .map((l) => l.trim().slice(0, 240)).slice(0, 10),
    cfms: [...new Set([...html.matchAll(/([A-Za-z0-9_\/.-]+\.cfm)/g)].map((m) => m[1]))].slice(0, 25),
  }
}

export async function main(customerid = "2513043", ion: IonResource | null = null) {
  const cred = ion ?? {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(cred)
  const o = s.ionOrigin
  const H = {
    Cookie: cookieHeader(s),
    "User-Agent": "Mozilla/5.0",
    Accept: "text/html, */*",
    Referer: `${o}/main.cfm`,
  }
  const rec: any = {}
  // 1) customer page (also sets server-side customer context for fragments)
  const main_html = await (await fetch(`${o}/customers/customerTabs.cfm?customerid=${customerid}`, { headers: H, redirect: "manual" })).text()
  rec.customer_page = sweep(main_html)

  // 2) the real tabs found on the customer page — Images is the photo surface
  const guesses = [
    `/Customers/Images/images.cfm?customerid=${customerid}`,
    `/Customers/Images/images.cfm`,
    `/customers/logs/loglist.cfm?customerid=${customerid}`,
  ]
  rec.fragments = {}
  for (const g of guesses) {
    try {
      const r = await fetch(`${o}${g}`, { headers: H, redirect: "manual" })
      const t = await r.text()
      rec.fragments[g] = { status: r.status, bytes: t.length, sweep: sweep(t),
        head: t.replace(/\s+/g, " ").slice(0, 400) }
    } catch (e: any) { rec.fragments[g] = { error: String(e).slice(0, 120) } }
  }
  return rec
}

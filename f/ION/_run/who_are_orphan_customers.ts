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

// READ-ONLY: who are the flagged (customer_unmatched) orphan-task customers? Fetch each ION customer
// detail page and surface name/phone/address + a text snippet. No DB writes.
export async function main(ids: string[] = ["1807904","2262281","2340243","2408772","2460366","2463288","2499559","2545478","2545500"]) {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const get = (u: string) => fetch(`${o}${u}`, { headers: H, redirect: "manual" }).then((x) => x.text())

  const phoneRe = /\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}/g
  const out: any[] = []
  for (const id of ids) {
    const row: any = { id }
    try {
      for (const path of [`/customers/customerTabs.cfm?customerid=${id}`, `/customers/customerInfo.cfm?customerid=${id}`]) {
        const html = await get(path)
        const root = parse(html)
        const text = root.text.replace(/\s+/g, " ").trim()
        const key = path.includes("customerTabs") ? "tabs" : "info"
        row[key] = {
          len: html.length,
          title: root.querySelector("title")?.text?.trim() || null,
          input_name: root.querySelector('input[name*="ame" i]')?.getAttribute("value") || null,
          phones: [...new Set((text.match(phoneRe) || []))].slice(0, 3),
          snippet: text.slice(0, 500),
        }
      }
    } catch (e: any) {
      row.error = String(e?.message ?? e).slice(0, 200)
    }
    out.push(row)
  }
  return out
}

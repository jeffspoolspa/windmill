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

// READ-ONLY: who are these ION customers? Fetch the customer detail page, STRIP <script>/<style>
// (the page is JS-heavy; the contact info is server-rendered underneath), surface name/email/phone +
// a clean visible-text snippet. No DB writes.
export async function main(ids: string[] = ["2408772","2463288","2499559","2545478","2545500"]) {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const get = (u: string) => fetch(`${o}${u}`, { headers: H, redirect: "manual" }).then((x) => x.text())

  const emailRe = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g
  const phoneRe = /\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}/g
  const out: any[] = []
  for (const id of ids) {
    try {
      const html = await get(`/customers/customerTabs.cfm?customerid=${id}`)
      const root = parse(html)
      root.querySelectorAll("script, style").forEach((n: any) => n.remove())
      const text = root.text.replace(/\s+/g, " ").trim()
      // input values often hold first/last name, email, address fields
      const inputs = root.querySelectorAll("input")
        .map((i: any) => ({ name: i.getAttribute("name"), value: (i.getAttribute("value") || "").trim() }))
        .filter((i: any) => i.value && i.name && /name|email|addr|city|state|zip|phone/i.test(i.name))
        .slice(0, 20)
      out.push({
        id,
        emails: [...new Set((html.match(emailRe) || []))].slice(0, 4),
        phones: [...new Set((text.match(phoneRe) || []))].slice(0, 4),
        inputs,
        snippet: text.slice(0, 700),
      })
    } catch (e: any) {
      out.push({ id, error: String(e?.message ?? e).slice(0, 200) })
    }
  }
  return out
}

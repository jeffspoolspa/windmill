//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

// DISCRIMINATOR TEST: raw Bun POST (exact headers) fails to run the report; a real browser form
// submit works. Which property matters -- Chrome's network stack (TLS/h2 fingerprint at the Incapsula
// WAF) or navigation semantics (sec-fetch: navigate)? An IN-PAGE fetch POST has Chrome's stack but
// CORS-style sec-fetch headers. If it primes the report -> stack fingerprint is the discriminator and
// in-page fetch is the minimal reliable mechanism. NO form submit in this run (unconfounded); fresh session.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion, { forceRefresh: true }) // fresh session, unprimed
  const o = s.ionOrigin
  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] })
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0" })
    await context.addCookies((s.cookies || []).map((c: any) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path || "/", secure: !!c.secure, httpOnly: !!c.httpOnly })))
    const page = await context.newPage()
    await page.goto(`${o}/reports/transactionRpt.cfm`, { waitUntil: "domcontentloaded" }) // browser GET only, no submit
    // in-page fetch POST (Chrome stack, non-navigation)
    const postRes: any = await page.evaluate(async (origin: string) => {
      const body = "rptOffice=0&CustomerType=0&TransactionType=Tasks&SyncStatus=0&Routes=0&rptStart=2026-06-01&rptEnd=2026-06-30&ServiceItem=&WOItem=&WorkFrom=06%2F01%2F2026&WorkTo=06%2F30%2F2026"
      const r = await fetch(`${origin}/reports/transactionRpt.cfm`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/x-www-form-urlencoded" }, body })
      const t = await r.text()
      return { status: r.status, len: t.length, has_xls_link: /allTransactions/i.test(t), echoes_dates: t.includes("2026-06-01") }
    }, o)
    // then in-page fetch XLS
    const xlsRes: any = await page.evaluate(async (origin: string) => {
      const r = await fetch(`${origin}/reports/_xls/allTransactions.cfm`, { credentials: "include" })
      const t = await r.text()
      return { status: r.status, len: t.length }
    }, o)
    // and raw XLS with the context cookies (post-in-page-POST)
    const cookies = await context.cookies(o)
    const cookieStr = cookies.map((c: any) => `${c.name}=${c.value}`).join("; ")
    const raw = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: { cookie: cookieStr, "user-agent": "Mozilla/5.0", accept: "text/html, */*" }, redirect: "manual" })
    return { inpage_post: postRes, inpage_xls: xlsRes, raw_xls_after: { status: raw.status, len: (await raw.text()).length } }
  } finally { await browser.close() }
}

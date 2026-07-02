//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// EXPERIMENT: why does the raw POST to transactionRpt.cfm not prime the report criteria while a real
// browser submit does? Steps:
//  1. browser: goto form, set June/Tasks, capture the POST's FULL headers, submit, confirm XLS link.
//  2. in-page XLS fetch (expected 200 = browser-primed).
//  3. raw GET XLS with the browser's post-submit cookies (tests: is cookie/session state sufficient?).
//  4. raw POST with the EXACT captured browser headers + same cookies, then raw GET XLS again.
//  5. also: does the raw POST response echo our posted dates (criteria acknowledged?) - grep rptStart value.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const out: any = {}

  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] })
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0", acceptDownloads: true })
    await context.addCookies((s.cookies || []).map((c: any) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path || "/", secure: !!c.secure, httpOnly: !!c.httpOnly })))
    const page = await context.newPage()
    let postHeaders: any = null, postData: string | null = null
    page.on("request", async (r: any) => {
      if (r.method() === "POST" && r.url().includes("transactionRpt")) {
        try { postHeaders = await r.allHeaders() } catch { postHeaders = r.headers() }
        postData = r.postData()
      }
    })
    await page.goto(`${o}/reports/transactionRpt.cfm`, { waitUntil: "domcontentloaded" })
    await page.evaluate(() => {
      const g = (id: string) => document.getElementById(id) as any
      if (g("rptStart")) g("rptStart").value = "2026-06-01"
      if (g("rptEnd")) g("rptEnd").value = "2026-06-30"
      const tt = document.querySelector('select[name="TransactionType"]') as any; if (tt) tt.value = "Tasks"
      const wf = document.querySelector('input[name="WorkFrom"]') as any; if (wf) wf.value = "06/01/2026"
      const wt = document.querySelector('input[name="WorkTo"]') as any; if (wt) wt.value = "06/30/2026"
    })
    await Promise.all([page.waitForLoadState("networkidle").catch(() => {}), page.evaluate(() => (document.getElementById("rpt") as any).submit())])
    await page.waitForTimeout(1200)
    const afterHtml = await page.content()
    out.browser_submit = { has_xls_link: /allTransactions/i.test(afterHtml), post_headers: postHeaders, post_data: postData }

    // 2) in-page XLS fetch
    const inpage: any = await page.evaluate(async (u: string) => { const r = await fetch(u, { credentials: "include" }); const t = await r.text(); return { status: r.status, len: t.length } }, `${o}/reports/_xls/allTransactions.cfm`)
    out.inpage_xls = inpage

    // post-submit cookies from the browser
    const cookies = await context.cookies(o)
    out.cookie_names = cookies.map((c: any) => c.name)
    const cookieStr = cookies.map((c: any) => `${c.name}=${c.value}`).join("; ")

    // 3) raw GET XLS with browser's post-submit cookies
    const raw1 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: { Cookie: cookieStr, "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }, redirect: "manual" })
    out.raw_get_with_browser_cookies = { status: raw1.status, len: (await raw1.text()).length }

    // 4) raw POST with EXACT captured headers (swap cookie for current) then raw GET XLS
    if (postHeaders && postData) {
      const H: Record<string, string> = {}
      for (const [k, v] of Object.entries(postHeaders)) { const lk = k.toLowerCase(); if ([":authority", ":method", ":path", ":scheme", "content-length", "host"].includes(lk)) continue; H[k] = String(v) }
      H["cookie"] = cookieStr
      const rp = await fetch(`${o}/reports/transactionRpt.cfm`, { method: "POST", headers: H, body: postData, redirect: "manual" })
      const rpBody = await rp.text()
      out.raw_post_exact_headers = { status: rp.status, len: rpBody.length, echoes_dates: rpBody.includes("2026-06-01"), has_xls_link: /allTransactions/i.test(rpBody) }
      const raw2 = await fetch(`${o}/reports/_xls/allTransactions.cfm`, { headers: { Cookie: cookieStr, "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }, redirect: "manual" })
      const b2 = await raw2.text()
      const table = parse(b2).querySelector("table")
      out.raw_xls_after_raw_post = { status: raw2.status, len: b2.length, rows: table ? table.querySelectorAll("tr").length : 0 }
    }
    return out
  } finally { await browser.close() }
}

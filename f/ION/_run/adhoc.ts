//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: reuse the cached session cookies in a real browser, load transactionRpt.cfm, submit
// June/Tasks the way the page does, follow the All Transactions XLS -- and CAPTURE the exact
// requests (method, url, postData) so we can replicate the pull as a raw fetch afterward.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const browser = await chromium.launch({ executablePath: "/usr/bin/chromium", args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'] })
  const context = await browser.newContext({ userAgent: "Mozilla/5.0", acceptDownloads: true })
  await context.addCookies((s.cookies || []).map((c: any) => ({ name: c.name, value: c.value, domain: c.domain, path: c.path || "/", secure: !!c.secure, httpOnly: !!c.httpOnly })))
  const page = await context.newPage()
  const captured: any[] = []
  page.on("request", (r: any) => {
    const u = r.url()
    if (/transactionRpt\.cfm|_xls\/|allTransactions/i.test(u)) captured.push({ method: r.method(), url: u, postData: (r.postData() || "").slice(0, 800) })
  })
  try {
    await page.goto(`${o}/reports/transactionRpt.cfm`, { waitUntil: "domcontentloaded" })
    // set criteria the way the page does, then submit the form directly
    await page.evaluate(() => {
      const g = (id: string) => document.getElementById(id) as any
      const setSel = (name: string, val: string) => { const el = document.querySelector(`select[name="${name}"]`) as any; if (el) el.value = val }
      if (g("rptStart")) g("rptStart").value = "2026-06-01"
      if (g("rptEnd")) g("rptEnd").value = "2026-06-30"
      setSel("TransactionType", "Tasks")
      const wf = document.querySelector('input[name="WorkFrom"]') as any; if (wf) wf.value = "06/01/2026"
      const wt = document.querySelector('input[name="WorkTo"]') as any; if (wt) wt.value = "06/30/2026"
    })
    const rpt = await page.$("#rpt")
    if (rpt) { await Promise.all([page.waitForLoadState("networkidle").catch(() => {}), page.evaluate(() => (document.getElementById("rpt") as any).submit())]) }
    await page.waitForTimeout(1500)
    // after submit: is the XLS link present? capture href, then fetch it in-page
    const afterHtml = await page.content()
    const xlsHref = (afterHtml.match(/href="([^"]*allTransactions[^"]*)"/i) || [])[1]?.replace(/&amp;/g, "&") || null
    let xls: any = null
    if (xlsHref) {
      const u = xlsHref.startsWith("http") ? xlsHref : `${o}${xlsHref.startsWith("/") ? "" : "/reports/"}${xlsHref}`
      const resp = await page.goto(u, { waitUntil: "domcontentloaded" })
      const body = await page.content()
      const table = parse(body).querySelector("table")
      const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))).filter((x: string[]) => x.some((c) => c)) : []
      xls = { url: u, status: resp?.status() ?? null, rows: rows.length, head: rows.slice(0, 6).map((x: string[]) => x.join(" | ")) }
    }
    return { xls_href_found: xlsHref, xls, captured }
  } finally {
    await browser.close()
  }
}

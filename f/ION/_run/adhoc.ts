//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: reuse session cookies in a browser, submit transactionRpt.cfm (June/Tasks) so session
// primes + the XLS link renders, then FETCH the XLS in-page (goto aborts on the attachment). Returns
// the captured priming POST (exact body to replicate) + the XLS columns/rowcount.
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
    if (/transactionRpt\.cfm|_xls\/|allTransactions/i.test(u)) captured.push({ method: r.method(), url: u, postData: (r.postData() || "").slice(0, 1000) })
  })
  const out: any = { captured }
  try {
    await page.goto(`${o}/reports/transactionRpt.cfm`, { waitUntil: "domcontentloaded" })
    await page.evaluate(() => {
      const g = (id: string) => document.getElementById(id) as any
      const setSel = (name: string, val: string) => { const el = document.querySelector(`select[name="${name}"]`) as any; if (el) el.value = val }
      if (g("rptStart")) g("rptStart").value = "2026-06-01"
      if (g("rptEnd")) g("rptEnd").value = "2026-06-30"
      setSel("TransactionType", "Tasks")
      const wf = document.querySelector('input[name="WorkFrom"]') as any; if (wf) wf.value = "06/01/2026"
      const wt = document.querySelector('input[name="WorkTo"]') as any; if (wt) wt.value = "06/30/2026"
    })
    await Promise.all([page.waitForLoadState("networkidle").catch(() => {}), page.evaluate(() => (document.getElementById("rpt") as any).submit())])
    await page.waitForTimeout(1500)
    const afterHtml = await page.content()
    out.report_list_has_xls = /allTransactions/i.test(afterHtml)
    const xlsHref = (afterHtml.match(/href="([^"]*allTransactions[^"]*)"/i) || [])[1]?.replace(/&amp;/g, "&") || null
    out.xls_href = xlsHref
    // fetch the XLS from within the page (session primed) -- goto would abort on the attachment
    const res: any = await page.evaluate(async (u: string) => {
      const r = await fetch(u, { credentials: "include" })
      const t = await r.text()
      return { status: r.status, len: t.length, sample: t.slice(0, 4000) }
    }, xlsHref ? (xlsHref.startsWith("http") ? xlsHref : `${o}${xlsHref.startsWith("/") ? xlsHref : "/reports/" + xlsHref}`) : `${o}/reports/_xls/allTransactions.cfm`)
    out.xls_status = res.status
    const table = parse(res.sample).querySelector("table")
    const rows = table ? table.querySelectorAll("tr").map((tr: any) => tr.querySelectorAll("td,th").map((c: any) => c.text.trim().replace(/\s+/g, " "))).filter((x: string[]) => x.some((c) => c)) : []
    out.xls_rows_in_sample = rows.length
    out.xls_head = rows.slice(0, 8).map((x: string[]) => x.join(" | "))
    return out
  } catch (e: any) {
    out.error = String(e?.message ?? e).slice(0, 200)
    return out
  } finally {
    await browser.close()
  }
}

//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"

export async function main() {
  const username = await wmill.getVariable("f/ION/USERNAME")
  const password = await wmill.getVariable("f/ION/PASSWORD")
  const loginUrl = await wmill.getVariable("f/ION/LOGIN_URL")

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox", "--single-process", "--no-zygote", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  })
  const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })
  const page = await context.newPage()
  const out: any = { logged_in: false }

  try {
    await page.goto(loginUrl)
    await page.locator("#txtUserName").fill(username as string)
    await page.locator("#txtPassword").fill(password as string)
    await page.locator('button:has-text("Log In")').click()
    await page.waitForLoadState("networkidle", { timeout: 30000 })
    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 })
    await page.waitForTimeout(1000)
    await page.locator("text=ION POOL CARE").click({ timeout: 5000 })
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    const origin = new URL(page.url()).origin
    out.logged_in = true

    const fetchUrl = (url: string) =>
      page.evaluate(async (u: string) => {
        try {
          const res = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
          return { status: res.status, body: await res.text() }
        } catch (e: any) {
          return { status: 0, body: String(e) }
        }
      }, url)

    // 1) customer list with NO search — is this the full base? what fields per row?
    const listAll = await fetchUrl(`${origin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=&reset=1`)
    out.list_total_ids = (listAll.body.match(/customerTabs\.cfm\?customerid=/g) || []).length
    out.list_len = listAll.body.length
    // capture the table region around the first customer rows (headers + ~3 rows)
    const ix = listAll.body.indexOf("customerTabs.cfm?customerid=")
    const tStart = ix >= 0 ? listAll.body.lastIndexOf("<table", ix) : 0
    out.list_table_slice = listAll.body.slice(Math.max(0, tStart), (ix >= 0 ? ix : 0) + 2400).replace(/\s+/g, " ")

    // 2) a targeted search to confirm the search param + see one matched row fully
    const listSearch = await fetchUrl(`${origin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=ABOLT&reset=1`)
    const si = listSearch.body.indexOf("customerTabs.cfm?customerid=")
    out.search_sample_row = si >= 0
      ? listSearch.body.slice(listSearch.body.lastIndexOf("<tr", si), listSearch.body.indexOf("</tr>", si) + 5).replace(/\s+/g, " ").slice(0, 1500)
      : null
    out.search_total_ids = (listSearch.body.match(/customerTabs\.cfm\?customerid=/g) || []).length

    // 3) reports picker — find any customer/account/export report endpoints
    const reports = await fetchUrl(`${origin}/reports/reports.cfm`)
    out.reports_status = reports.status
    const links = new Set<string>()
    for (const m of reports.body.matchAll(/(?:href|onclick|data-url)\s*=\s*["']([^"']*\.cfm[^"']*)["']/gi)) links.add(m[1])
    for (const m of reports.body.matchAll(/ColdFusionNavigate\(\s*["']([^"']*\.cfm[^"']*)["']/gi)) links.add(m[1])
    out.report_links_customerish = [...links].filter((u) => /cust|client|account|export|list|master/i.test(u)).slice(0, 40)
    // also the visible report names (link text) for the customer-ish ones
    const names: string[] = []
    for (const m of reports.body.matchAll(/>([^<>]{4,60})<\/a>/gi)) {
      const t = m[1].trim()
      if (/cust|client|account|master|list|export/i.test(t)) names.push(t)
    }
    out.report_names_customerish = [...new Set(names)].slice(0, 40)
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

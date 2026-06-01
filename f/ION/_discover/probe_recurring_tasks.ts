//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): v15 proved the real app nav warms the session (reports.cfm
// -> default module -> serviceEvents, all 200). Now -- in that warmed session --
// fetch RecurringtasksActive directly (+ navigate fallback) to see if loading
// reports.cfm is the missing priming.

import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"

export async function main() {
  const LOGIN_URL = await wmill.getVariable("f/ION/LOGIN_URL")
  const USERNAME = await wmill.getVariable("f/ION/USERNAME")
  const PASSWORD = await wmill.getVariable("f/ION/PASSWORD")

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox","--single-process","--no-zygote","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"],
  })
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36" })
    const page = await context.newPage()
    await page.goto(LOGIN_URL)
    await page.locator("#txtUserName").fill(USERNAME)
    await page.locator("#txtPassword").fill(PASSWORD)
    await page.locator('button:has-text("Log In")').click()
    await page.waitForLoadState("networkidle", { timeout: 30000 })
    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 })
    await page.waitForTimeout(1000)
    await page.locator("text=ION POOL CARE").click({ timeout: 5000 })
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    const ionOrigin = new URL(page.url()).origin
    await page.waitForTimeout(2000)
    const today = new Date().toISOString().slice(0, 10)
    const out: any = {}

    // WARM the session the real app way
    await page.evaluate(() => { document.querySelectorAll('div.resizable.ui-draggable, div[id*="MyServiceWin"], div[id*="MyPrintWin"]').forEach(el => el.remove()) })
    await page.evaluate(() => { /* @ts-ignore */ ColdFusionNavigate("/reports/reports.cfm", "pageContent") }).catch(()=>{})
    await page.waitForTimeout(4000)
    await page.evaluate((u: string) => { /* @ts-ignore */ ColdFusionNavigate(u, "rptDetail") }, `/reports/serviceEvents.cfm?office=0&tech=0&serviceType=0&Start=${today}&end=&set=1`).catch(()=>{})
    await page.waitForTimeout(4000)
    out.rptDetailHasLink = await page.evaluate(() => !!document.querySelector('a[href*="RecurringtasksActive"]'))

    const reportUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`

    // Attempt A: in-browser fetch in the warmed session
    const fetched = await page.evaluate(async (u: string) => {
      const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
      return { status: r.status, len: (await r.text()).length }
    }, reportUrl)
    out.fetch_status = fetched.status
    out.fetch_len = fetched.len

    // If 200, re-fetch + parse the data
    if (fetched.status === 200) {
      const body = await page.evaluate(async (u: string) => (await fetch(u, { credentials: "include" })).text(), reportUrl)
      const root = parse(body)
      const rows = root.querySelectorAll("tr")
      const hdrRow = rows.find((r:any) => r.text.includes("Cust ID"))
      out.dataRows = rows.length
      out.headerFound = !!hdrRow
      if (hdrRow) out.header = hdrRow.querySelectorAll("td,th").map((c:any)=>c.text.replace(/\s+/g," ").trim())
    } else {
      out.fetch_preview = (await page.evaluate(async (u: string) => (await fetch(u, { credentials: "include" })).text(), reportUrl)).slice(0, 300)
    }
    return out
  } finally {
    await browser.close()
  }
}

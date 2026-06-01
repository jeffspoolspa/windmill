//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): mirror the working work-orders report approach for the
// "Recurring Task Detail - Active Only" report:
//   1. login -> ION
//   2. prime the tasks-tab context (loadExternalContent #csttasks taskList.cfm)
//   3. in-browser fetch the report WITH ColdFusion _cf_* params
//   /reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0

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
  let cfClientId: string | undefined
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })
    const page = await context.newPage()
    page.on("request", (req: any) => {
      if (cfClientId) return
      const m = req.url().match(/_cf_clientid=([A-F0-9]{32})/i)
      if (m) cfClientId = m[1]
    })
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

    // Prime the tasks-tab context (like the WO flow fetches its picker first)
    try {
      await page.evaluate(() => {
        // @ts-ignore
        if (typeof loadExternalContent === "function") loadExternalContent("#csttasks", "/tasks/taskList.cfm")
      })
    } catch {}
    await page.waitForTimeout(3000)

    const params = new URLSearchParams({
      techid: "0", OfficeID: "0", serviceType: "0",
      _cf_nodebug: "true", _cf_nocache: "true", _cf_rc: "1",
    })
    if (cfClientId) params.set("_cf_clientid", cfClientId)
    const reportUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?${params.toString()}`

    const r = await page.evaluate(async (u: string) => {
      const res = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
      return { status: res.status, contentType: res.headers.get("content-type"), body: await res.text() }
    }, reportUrl)

    const out: any = {
      reportUrl, status: r.status, contentType: r.contentType,
      byteLength: r.body.length, cfClientIdCaptured: Boolean(cfClientId),
    }
    const root = parse(r.body)
    const tables = root.querySelectorAll("table")
    let best: any = null, bestRows = 0
    for (const t of tables) { const rows = t.querySelectorAll("tr").length; if (rows > bestRows) { bestRows = rows; best = t } }
    out.tableCount = tables.length
    if (best && bestRows > 1) {
      const trs = best.querySelectorAll("tr")
      const cell = (tr: any) => tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim())
      out.dataTableRows = trs.length
      out.headerRow = trs[0] ? cell(trs[0]) : []
      out.columnCount = out.headerRow.length
      out.sampleRows = trs.slice(1, 4).map(cell)
    } else {
      out.preview = r.body.slice(0, 900)
    }
    return out
  } finally {
    await browser.close()
  }
}

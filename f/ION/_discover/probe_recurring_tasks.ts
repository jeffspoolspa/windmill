//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe: get ION's "Recurring Task Detail - Active Only" report as a real
// top-level navigation/download (XHR fetch 500s -- Imperva WAF rejects
// non-navigation requests). Read-only.
//   /reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0

import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { readFile } from "fs/promises"

export async function main() {
  const LOGIN_URL = await wmill.getVariable("f/ION/LOGIN_URL")
  const USERNAME = await wmill.getVariable("f/ION/USERNAME")
  const PASSWORD = await wmill.getVariable("f/ION/PASSWORD")

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox","--single-process","--no-zygote","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"],
    acceptDownloads: true,
  } as any)
  try {
    const context = await browser.newContext({
      userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      acceptDownloads: true,
    })
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

    const reportUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`

    const newPage = await context.newPage()
    await newPage.setExtraHTTPHeaders({ Referer: `${ionOrigin}/tasks/taskList.cfm` })

    const dlPromise = newPage.waitForEvent("download", { timeout: 25000 }).catch(() => null)
    let gotoStatus: number | null = null
    let gotoErr: string | null = null
    try {
      const resp = await newPage.goto(reportUrl, { waitUntil: "domcontentloaded", timeout: 25000 })
      gotoStatus = resp ? resp.status() : null
    } catch (e: any) {
      gotoErr = String(e?.message || e).slice(0, 200)
    }
    const dl = await dlPromise

    const out: any = { reportUrl, gotoStatus, gotoErr, gotDownload: Boolean(dl) }

    let body: string | null = null
    let bytes = 0
    if (dl) {
      const p = await dl.path()
      out.suggestedFilename = dl.suggestedFilename()
      if (p) {
        const buf = await readFile(p)
        bytes = buf.length
        const isZip = buf.subarray(0, 2).toString("latin1") === "PK"
        out.byteLength = bytes
        out.isBinaryXlsx = isZip
        body = isZip ? null : buf.toString("utf8")
      }
    } else if (gotoStatus) {
      body = await newPage.content()
      out.byteLength = body.length
    }

    if (body) {
      const root = parse(body)
      const tables = root.querySelectorAll("table")
      let best: any = null, bestRows = 0
      for (const t of tables) { const rows = t.querySelectorAll("tr").length; if (rows > bestRows) { bestRows = rows; best = t } }
      out.tableCount = tables.length
      if (best) {
        const trs = best.querySelectorAll("tr")
        const cell = (tr: any) => tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim())
        out.dataTableRows = trs.length
        out.headerRow = trs[0] ? cell(trs[0]) : []
        out.columnCount = out.headerRow.length
        out.sampleRows = trs.slice(1, 4).map(cell)
      } else {
        out.preview = body.slice(0, 800)
      }
    }

    return out
  } finally {
    await browser.close()
  }
}

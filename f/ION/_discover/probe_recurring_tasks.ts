//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): replicate a real click of the "Recurring Task Detail -
// Active Only" XLS link -- navigate from the authenticated page itself (natural
// Referer) and capture the download.
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
  })
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })
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
    const pageUrlAfterLogin = page.url()
    await page.waitForTimeout(2000)

    const reportUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`

    // Real navigation from the authenticated page (natural Referer + navigate sec-fetch)
    const dlPromise = page.waitForEvent("download", { timeout: 25000 }).catch(() => null)
    let navErr: string | null = null
    try {
      await page.evaluate((u: string) => { window.location.assign(u) }, reportUrl)
    } catch (e: any) { navErr = String(e?.message || e).slice(0, 150) }
    const dl = await dlPromise

    const out: any = { reportUrl, pageUrlAfterLogin, gotDownload: Boolean(dl), navErr }

    let body: string | null = null
    if (dl) {
      const p = await dl.path()
      out.suggestedFilename = dl.suggestedFilename()
      if (p) {
        const buf = await readFile(p)
        out.byteLength = buf.length
        const isZip = buf.subarray(0, 2).toString("latin1") === "PK"
        out.isBinaryXlsx = isZip
        body = isZip ? null : buf.toString("utf8")
      }
    } else {
      await page.waitForTimeout(2500)
      try { body = await page.content() } catch {}
      out.landedUrl = page.url()
      out.byteLength = body ? body.length : 0
    }

    if (body) {
      const root = parse(body)
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
        out.preview = body.slice(0, 900)
      }
    }
    return out
  } finally {
    await browser.close()
  }
}

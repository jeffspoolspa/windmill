//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): the untried winning combo --
//   1. fetch serviceEvents.cfm?...&set=1  (200, primes report criteria in session)
//   2. NAVIGATE (download semantics, not XHR) to the RecurringtasksActive XLS link
// This is the exact human flow: open reports page, then click the download link.

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
  let cfClientId: string | undefined
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36", acceptDownloads: true })
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
    await page.waitForTimeout(1500)

    const today = new Date().toISOString().slice(0, 10)
    const cid = cfClientId || ""

    // STEP 1: prime picker (set criteria in CF session)
    const pickerUrl = `${ionOrigin}/reports/serviceEvents.cfm?office=0&tech=0&serviceType=0&Start=${today}&end=&set=1&_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=1`
    const picker = await page.evaluate(async (u: string) => {
      const r = await fetch(u, { credentials: "include", headers: { Accept: "*/*" } })
      return { status: r.status }
    }, pickerUrl)

    const out: any = { pickerStatus: picker.status, cfClientId: cid }

    // STEP 2: NAVIGATE to the XLS download (not XHR)
    const dataUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`
    out.dataUrl = dataUrl
    const dlPromise = page.waitForEvent("download", { timeout: 20000 }).catch(() => null)
    try { await page.evaluate((u: string) => { window.location.assign(u) }, dataUrl) } catch {}
    const dl = await dlPromise
    out.gotDownload = Boolean(dl)

    if (dl) {
      const p = await dl.path()
      out.downloadFilename = dl.suggestedFilename()
      if (p) {
        const buf = await readFile(p)
        out.byteLength = buf.length
        const isZip = buf.subarray(0, 2).toString("latin1") === "PK"
        out.isBinaryXlsx = isZip
        const body = isZip ? null : buf.toString("utf8")
        if (body) {
          const root = parse(body)
          const tables = root.querySelectorAll("table")
          let best: any = null, bestRows = 0
          for (const t of tables) { const rows = t.querySelectorAll("tr").length; if (rows > bestRows) { bestRows = rows; best = t } }
          out.tableCount = tables.length
          if (best && bestRows > 1) {
            const trs = best.querySelectorAll("tr")
            const cell = (tr: any) => tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim())
            out.dataRows = trs.length
            out.headerRow = trs[0] ? cell(trs[0]) : []
            out.columnCount = out.headerRow.length
            out.sampleRows = trs.slice(1, 4).map(cell)
          } else { out.dataPreview = body.slice(0, 800) }
        } else { out.note = "binary xlsx downloaded" }
      }
    } else {
      await page.waitForTimeout(2000)
      out.landedUrl = page.url()
      try { out.bodyPreview = (await page.content()).slice(0, 600) } catch {}
    }
    return out
  } finally {
    await browser.close()
  }
}

//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): net-export showed the working request is a genuine user-gesture
// navigation (sec-fetch-user: ?1, sec-fetch-mode: navigate, accept: text/html). All
// my prior attempts were fetch() or scripted navigations (no sec-fetch-user). Fix:
// inject a real anchor and use Playwright's TRUSTED click -> sets sec-fetch-user: ?1.

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
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36", acceptDownloads: true })
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
    await page.waitForTimeout(1500)

    const reportUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`

    // Inject a real anchor (no target so the download fires on this page)
    await page.evaluate((href: string) => {
      const a = document.createElement("a")
      a.id = "__dl_probe"
      a.href = href
      a.textContent = "download-probe"
      a.style.position = "fixed"; a.style.top = "0"; a.style.left = "0"; a.style.zIndex = "99999"
      document.body.appendChild(a)
    }, reportUrl)

    const out: any = { reportUrl }
    const dlPromise = page.waitForEvent("download", { timeout: 20000 }).catch(() => null)
    // TRUSTED click -> real input events -> sec-fetch-user: ?1
    await page.click("#__dl_probe", { timeout: 8000 }).catch((e: any) => { out.clickErr = String(e?.message || e).slice(0, 120) })
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
      try { out.bodyPreview = (await page.content()).slice(0, 500) } catch {}
    }
    return out
  } finally {
    await browser.close()
  }
}

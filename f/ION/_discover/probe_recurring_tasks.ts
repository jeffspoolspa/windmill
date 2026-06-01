//bun-extra-requirements:
//playwright@1.40.0

// Probe (read-only): hit RecurringtasksActive.cfm as a real navigation FROM the
// authenticated page (natural Referer = the app shell), capturing the exact
// request headers we send + the result (download or error body).

import { chromium } from "playwright@1.40.0"
import { readFile } from "fs/promises"
import * as wmill from "windmill-client"

export async function main() {
  const LOGIN_URL = await wmill.getVariable("f/ION/LOGIN_URL")
  const USERNAME = await wmill.getVariable("f/ION/USERNAME")
  const PASSWORD = await wmill.getVariable("f/ION/PASSWORD")

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox","--single-process","--no-zygote","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"],
  })
  const sent: any[] = []
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })
    const page = await context.newPage()
    page.on("request", (req: any) => {
      const u = req.url()
      if (/RecurringtasksActive/i.test(u)) {
        const h = req.headers()
        sent.push({ method: req.method(), url: u.slice(0, 200), referer: h["referer"], cookieLen: (h["cookie"] || "").length,
          secFetchMode: h["sec-fetch-mode"], secFetchDest: h["sec-fetch-dest"], secFetchSite: h["sec-fetch-site"], secFetchUser: h["sec-fetch-user"] })
      }
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
    const refererPage = page.url()
    await page.waitForTimeout(2000)

    const reportUrl = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`

    const dlPromise = page.waitForEvent("download", { timeout: 20000 }).catch(() => null)
    try { await page.evaluate((u: string) => { window.location.assign(u) }, reportUrl) } catch {}
    const dl = await dlPromise

    const out: any = { reportUrl, refererPage, gotDownload: Boolean(dl), sentRequestHeaders: sent }
    if (dl) {
      const p = await dl.path()
      out.downloadFilename = dl.suggestedFilename()
      if (p) {
        const buf = await readFile(p)
        out.byteLength = buf.length
        const isZip = buf.subarray(0, 2).toString("latin1") === "PK"
        out.isBinaryXlsx = isZip
        out.preview = isZip ? "(binary xlsx)" : buf.toString("utf8").slice(0, 1500)
      }
    } else {
      await page.waitForTimeout(2500)
      out.landedUrl = page.url()
      try { out.bodyPreview = (await page.content()).slice(0, 600) } catch {}
    }
    return out
  } finally {
    await browser.close()
  }
}

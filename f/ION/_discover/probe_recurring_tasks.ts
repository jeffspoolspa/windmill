//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe: fetch ION's "Recurring Task Detail - Active Only" report from INSIDE
// the logged-in browser page (raw worker fetch 500s). Read-only.
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
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" })
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
    await page.waitForTimeout(2500)

    const base = `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`
    const variants: Record<string, string> = {
      plain: base,
      with_cf: cfClientId ? `${base}&_cf_clientid=${cfClientId}` : base,
    }

    const attempts: any[] = []
    for (const [name, url] of Object.entries(variants)) {
      const r = await page.evaluate(async (u: string) => {
        const res = await fetch(u, { credentials: "include", headers: { Accept: "*/*" } })
        return { status: res.status, body: await res.text() }
      }, url)
      const att: any = { name, url, status: r.status, byteLength: r.body.length }
      if (/<table/i.test(r.body)) {
        const root = parse(r.body)
        const tables = root.querySelectorAll("table")
        let best: any = null, bestRows = 0
        for (const t of tables) { const rows = t.querySelectorAll("tr").length; if (rows > bestRows) { bestRows = rows; best = t } }
        if (best) {
          const trs = best.querySelectorAll("tr")
          const cell = (tr: any) => tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim())
          att.dataTableRows = trs.length
          att.headerRow = trs[0] ? cell(trs[0]) : []
          att.columnCount = att.headerRow.length
          att.sampleRows = trs.slice(1, 4).map(cell)
        } else { att.preview = r.body.slice(0, 800) }
      } else { att.preview = r.body.slice(0, 800) }
      attempts.push(att)
      if (r.status === 200) break
    }

    return { ionOrigin, cfClientIdCaptured: Boolean(cfClientId), attempts }
  } finally {
    await browser.close()
  }
}

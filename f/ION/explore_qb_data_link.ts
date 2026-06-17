//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"

export async function main() {
  const username = await wmill.getVariable("f/ION/USERNAME")
  const password = await wmill.getVariable("f/ION/PASSWORD")
  const loginUrl = await wmill.getVariable("f/ION/LOGIN_URL")

  const ion = "1124167" // ABOLT, MARILYN
  const qbo = "6532"

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

    await fetchUrl(`${origin}/customers/customerTabs.cfm?customerid=${ion}`)
    const det = await fetchUrl(`${origin}/customers/details.cfm`)
    const b = det.body

    const qi = b.indexOf("QuickBooks Data")
    out.slice_qb_data = qi >= 0 ? b.slice(Math.max(0, qi - 900), qi + 300).replace(/\s+/g, " ") : null
    const ai = b.indexOf("Accounting Sync")
    out.slice_accounting = ai >= 0 ? b.slice(Math.max(0, ai - 200), ai + 500).replace(/\s+/g, " ") : null

    const calls: string[] = []
    for (const re of [/loadExternalContent\s*\([^)]*\)/gi, /window\.open\s*\([^)]{0,160}\)/gi, /openExternal\w*\s*\([^)]*\)/gi]) {
      let m
      while ((m = re.exec(b)) !== null && calls.length < 20) calls.push(m[0].slice(0, 200))
    }
    out.external_calls = calls

    const urls: string[] = []
    let m
    const reU = /https?:\/\/[^"'\s)]*proedge[^"'\s)]*/gi
    while ((m = reU.exec(b)) !== null && urls.length < 10) urls.push(m[0])
    out.proedge_urls = urls

    const qf = await fetchUrl(`${origin}/customers/qbFields.cfm?id=${ion}`)
    const qfi = qf.body.toLowerCase().indexOf("custom fields")
    out.qbfields_slice = qfi >= 0 ? qf.body.slice(qfi, qfi + 2000).replace(/\s+/g, " ") : qf.body.slice(0, 1500).replace(/\s+/g, " ")
    out.qbfields_has_qbo = qf.body.includes(qbo)

    out.detail_has_qbo = b.includes(qbo)
    out.detail_len = b.length
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

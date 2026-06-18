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
  const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" })
  const page = await context.newPage()
  let cfClientId: string | undefined
  page.on("request", (req: any) => {
    const m = req.url().match(/_cf_clientid=([A-F0-9]{32})/i)
    if (m && !cfClientId) cfClientId = m[1]
  })
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
    out.cf_clientid_captured = !!cfClientId

    const fetchUrl = (url: string) =>
      page.evaluate(async (u: string) => {
        try { const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest" } }); return { status: r.status, body: await r.text() } }
        catch (e: any) { return { status: 0, body: String(e) } }
      }, url)

    const cid = cfClientId ?? ""
    const url = `${origin}/home/customerLogDetails.cfm?officeid=0&techid=0&status=0&dayindex=&dayindexsel=&logset=1&_cf_containerId=cf_layoutareaxmanagelogCenter&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=1`
    const r = await fetchUrl(url)
    out.status = r.status
    out.len = r.body.length
    const ti = r.body.search(/<t[dh][\s>]/i)
    out.head = ti >= 0 ? r.body.slice(Math.max(0, ti - 80), ti + 1800).replace(/\s+/g, " ") : r.body.slice(0, 900).replace(/\s+/g, " ")
    out.has_addLog = (r.body.match(/addLog\.cfm/gi) || []).length
    out.has_eventid = (r.body.match(/EventID/gi) || []).length
    out.has_customerid = [...new Set((r.body.match(/customerid=(\d+)/gi) || []))].slice(0, 6)
    out.has_logid = (r.body.match(/LogID=(\d+)/gi) || []).length
    out.has_customerTabs = (r.body.match(/customerTabs/gi) || []).length
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

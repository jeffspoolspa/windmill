//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"

export async function main() {
  const username = await wmill.getVariable("f/ION/USERNAME")
  const password = await wmill.getVariable("f/ION/PASSWORD")
  const loginUrl = await wmill.getVariable("f/ION/LOGIN_URL")
  const testCust = "2007808" // BEANE, BOBB - active + expired tasks

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

    const getUrl = (url: string) =>
      page.evaluate(async (u: string) => {
        try { const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } }); return { status: r.status, body: await r.text() } }
        catch (e: any) { return { status: 0, body: String(e) } }
      }, url)
    const postTaskList = (origin2: string, body: string) =>
      page.evaluate(async (args: { origin: string; body: string }) => {
        try {
          const r = await fetch(args.origin + "/tasks/taskList.cfm", {
            method: "POST",
            headers: { "content-type": "application/x-www-form-urlencoded; charset=UTF-8", "x-requested-with": "XMLHttpRequest", accept: "text/html, */*; q=0.01" },
            body: args.body, credentials: "include",
          })
          return { status: r.status, body: await r.text() }
        } catch (e: any) { return { status: 0, body: String(e) } }
      }, { origin: origin2, body })

    function summarize(body: string) {
      const ids = [...new Set((body.match(/\b\d{7}\b/g) || []))]
      const links = new Set<string>()
      for (const m of body.matchAll(/(?:href|onclick|ColdFusionNavigate\()\s*=?\s*["']([^"']*\.cfm[^"']*)["']/gi)) links.add(m[1].slice(0, 120))
      // header row + first data row
      const ti = body.search(/<t[dh][\s>]/i)
      return { len: body.length, distinct_7digit_ids: ids.length, sample_ids: ids.slice(0, 12), cfm_links: [...links].slice(0, 15), head: body.slice(Math.max(0, ti - 50), ti + 1800).replace(/\s+/g, " ") }
    }

    // 1) set the session customer, then POST taskList (mirrors the browser flow)
    await getUrl(`${origin}/customers/customerTabs.cfm?customerid=${testCust}`)
    const withSession = await postTaskList(origin, "limit=200")
    out.with_session = { status: withSession.status, ...summarize(withSession.body) }

    // 2) try passing CustomerID directly in the POST body (no session reliance)
    const withParam = await postTaskList(origin, `CustomerID=${testCust}&customerid=${testCust}&limit=200`)
    out.with_customerid_param = { status: withParam.status, ...summarize(withParam.body) }
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

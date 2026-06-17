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
          const body = await res.text()
          return { status: res.status, ct: res.headers.get("content-type") || "", body }
        } catch (e: any) {
          return { status: 0, ct: "", body: String(e) }
        }
      }, url)

    // 1) the picker page
    const pick = await fetchUrl(`${origin}/reports/CustomerRpt.cfm`)
    out.picker = { status: pick.status, ct: pick.ct, len: pick.body.length }
    // form fields (what filters it wants) + any data/download links
    const fields: string[] = []
    for (const m of pick.body.matchAll(/<(?:input|select)[^>]*name\s*=\s*["']([^"']+)["'][^>]*>/gi)) fields.push(m[1])
    out.picker_fields = [...new Set(fields)].slice(0, 40)
    const links = new Set<string>()
    for (const m of pick.body.matchAll(/(?:href|action|data-url)\s*=\s*["']([^"']*\.cfm[^"']*)["']/gi)) links.add(m[1])
    for (const m of pick.body.matchAll(/(?:ColdFusionNavigate|window\.open|location\.href\s*=)\s*\(?\s*["']([^"']*\.cfm[^"']*)["']/gi)) links.add("[js] " + m[1])
    out.picker_links = [...links].slice(0, 40)
    out.picker_head = pick.body.slice(0, 600).replace(/\s+/g, " ")

    // 2) try the conventional _xls data endpoint with empty filters (all customers)
    const dataCandidates = [
      `${origin}/reports/_xls/CustomerRpt.cfm?Office=&Technician=&Route=&Status=&_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1`,
      `${origin}/reports/CustomerRpt.cfm?run=1&Office=&Technician=&Route=&Status=`,
    ]
    const data: any = {}
    for (const u of dataCandidates) {
      const r = await fetchUrl(u)
      // count rows + grab the header row (first <tr>)
      const rows = (r.body.match(/<tr[\s>]/gi) || []).length
      const firstTr = r.body.search(/<tr[\s>]/i)
      const headerSlice = firstTr >= 0 ? r.body.slice(firstTr, r.body.indexOf("</tr>", firstTr + 200) + 5).replace(/\s+/g, " ").slice(0, 1200) : null
      data[u.replace(origin, "")] = { status: r.status, ct: r.ct, len: r.body.length, tr_count: rows, header_slice: headerSlice }
    }
    out.data_endpoints = data
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

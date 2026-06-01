//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): the one untested variant -- fetch RecurringtasksActive WITH
// the SAME _cf_* params that make serviceEvents return 200. Prime serviceEvents
// first, then in-browser fetch the xls with _cf_containerId + _cf_clientid + etc.

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

    // prime
    const pickerUrl = `${ionOrigin}/reports/serviceEvents.cfm?office=0&tech=0&serviceType=0&Start=${today}&end=&set=1&_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=1`
    const picker = await page.evaluate(async (u: string) => (await fetch(u, { credentials: "include", headers: { Accept: "*/*" } })).status, pickerUrl)

    // try several variants of the xls fetch in one run
    const variants: Record<string, string> = {
      bare: `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`,
      with_cf: `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0&_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=2`,
      // some ION _xls reports use StartDate (serial) like the sibling reports
      with_startdate: `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&StartDate=&EndDate=&serviceType=0&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=3`,
    }

    const attempts: any[] = []
    for (const [name, url] of Object.entries(variants)) {
      const r = await page.evaluate(async (u: string) => {
        const res = await fetch(u, { credentials: "include", headers: { Accept: "*/*" } })
        return { status: res.status, contentType: res.headers.get("content-type"), len: (await res.text()).length }
      }, url)
      attempts.push({ name, url: url.slice(0, 140), status: r.status, contentType: r.contentType, len: r.len })
    }

    // for any 200, re-fetch and parse
    let parsed: any = null
    const ok = attempts.find((a) => a.status === 200)
    if (ok) {
      const full = await page.evaluate(async (u: string) => (await fetch(u, { credentials: "include", headers: { Accept: "*/*" } })).text(), variants[ok.name])
      const root = parse(full)
      const tables = root.querySelectorAll("table")
      let best: any = null, bestRows = 0
      for (const t of tables) { const rows = t.querySelectorAll("tr").length; if (rows > bestRows) { bestRows = rows; best = t } }
      if (best) {
        const trs = best.querySelectorAll("tr")
        const cell = (tr: any) => tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim())
        parsed = { rows: trs.length, header: trs[0] ? cell(trs[0]) : [], sample: trs.slice(1, 4).map(cell) }
      } else { parsed = { preview: full.slice(0, 600) } }
    }

    return { pickerStatus: picker, cfClientId: cid, attempts, parsed }
  } finally {
    await browser.close()
  }
}

//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// CONTROL probe (read-only): replicate the PROVEN work-orders report flow with my
// exact technique (woReports.cfm picker -> WorkOrderDetail link -> fetch). If this
// returns WO data but RecurringtasksActive 500s, the issue is that report, not me.
// Also re-tests RecurringtasksActive in the same session for a side-by-side.

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
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36" })
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
    const cid = cfClientId || ""

    const out: any = { cfClientId: cid }

    // ===== CONTROL: WO report (proven flow from get_scheduled_wo) =====
    const woParams = new URLSearchParams({
      Office: "", Technician: "", ScheduleStart: "2026-05-01", ScheduleEnd: "",
      WOType: "", WOTemplate: "", WOStatus: "", ScheduleStatus: "", ApprovalStatus: "",
      CreatedStart: "", CreatedEnd: "", CompletedStart: "", CompletedEnd: "",
      _cf_containerId: "rptDetail", _cf_nodebug: "true", _cf_nocache: "true", _cf_rc: "1",
    })
    if (cid) woParams.set("_cf_clientid", cid)
    const woPickerUrl = `${ionOrigin}/reports/woReports.cfm?${woParams.toString()}`
    const woPicker = await page.evaluate(async (u: string) => {
      const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
      return { status: r.status, body: await r.text() }
    }, woPickerUrl)
    out.wo_picker_status = woPicker.status
    out.wo_picker_len = woPicker.body.length

    const woRoot = parse(woPicker.body)
    let woDetailHref: string | null = null
    for (const a of woRoot.querySelectorAll("a")) {
      const h = a.getAttribute("href") || ""
      if (/WorkOrderDetail/i.test(h)) { woDetailHref = h; break }
    }
    out.wo_detail_href = woDetailHref ? woDetailHref.slice(0, 160) : null
    if (woDetailHref) {
      const woDataUrl = woDetailHref.startsWith("http") ? woDetailHref : `${ionOrigin}${woDetailHref.startsWith("/") ? "" : "/reports/"}${woDetailHref}`
      const woData = await page.evaluate(async (u: string) => {
        const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
        return { status: r.status, body: await r.text() }
      }, woDataUrl)
      out.wo_data_status = woData.status
      out.wo_data_len = woData.body.length
      const dRoot = parse(woData.body)
      const tables = dRoot.querySelectorAll("table")
      let best: any = null, bestRows = 0
      for (const t of tables) { const rows = t.querySelectorAll("tr").length; if (rows > bestRows) { bestRows = rows; best = t } }
      if (best) {
        const trs = best.querySelectorAll("tr")
        const cell = (tr: any) => tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim().slice(0, 30))
        out.wo_data_rows = trs.length
        out.wo_header_sample = (trs[0] ? cell(trs[0]) : []).slice(0, 12)
      }
    }

    // ===== SIDE-BY-SIDE: RecurringtasksActive in the same session =====
    const rta = await page.evaluate(async (u: string) => {
      const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
      return { status: r.status }
    }, `${ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`)
    out.recurringtasks_status = rta.status

    return out
  } finally {
    await browser.close()
  }
}

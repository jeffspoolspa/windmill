//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): faithful real-app flow to trigger session priming --
//   login -> ColdFusionNavigate('/reports/reports.cfm','pageContent')
//        -> ColdFusionNavigate(serviceEvents...&set=1,'rptDetail')
//        -> click the actual rendered RecurringtasksActive link -> capture download
// Instruments report-request statuses so we see exactly what 200s/500s.

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
  const netlog: any[] = []
  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36", acceptDownloads: true })
    let popupDownload: any = null
    context.on("page", (pop: any) => {
      pop.on("response", (res: any) => { const u = res.url(); if (/reports\/|Recurringtasks|serviceEvents/i.test(u)) netlog.push({ from: "popup", status: res.status(), url: u.replace("https://ionpoolcare.com","").slice(0,90) }) })
      pop.on("download", (d: any) => { popupDownload = d })
    })
    const page = await context.newPage()
    page.on("response", (res: any) => { const u = res.url(); if (/reports\/|Recurringtasks|serviceEvents/i.test(u)) netlog.push({ from: "main", status: res.status(), url: u.replace("https://ionpoolcare.com","").slice(0,90) }) })

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
    await page.waitForTimeout(2000)
    const today = new Date().toISOString().slice(0, 10)
    const out: any = {}

    // STEP 1: load the Reports landing page the app way
    await page.evaluate(() => { document.querySelectorAll('div.resizable.ui-draggable, div[id*="MyServiceWin"], div[id*="MyPrintWin"]').forEach(el => el.remove()) })
    await page.evaluate(() => { /* @ts-ignore */ ColdFusionNavigate("/reports/reports.cfm", "pageContent") }).catch((e:any)=>{ out.reportsNavErr = String(e).slice(0,100) })
    await page.waitForTimeout(3500)
    out.hasRptDetail = await page.evaluate(() => !!document.getElementById("rptDetail"))

    // STEP 2: load serviceEvents picker into rptDetail (sets session criteria the app way)
    await page.evaluate((u: string) => { /* @ts-ignore */ ColdFusionNavigate(u, "rptDetail") }, `/reports/serviceEvents.cfm?office=0&tech=0&serviceType=0&Start=${today}&end=&set=1`).catch((e:any)=>{ out.seNavErr = String(e).slice(0,100) })
    await page.waitForTimeout(3500)

    // STEP 3: find the rendered RecurringtasksActive link
    const link = await page.evaluate(() => {
      const a = document.querySelector('a[href*="RecurringtasksActive"]') as HTMLAnchorElement | null
      return a ? { found: true, href: a.getAttribute("href"), target: a.getAttribute("target") } : { found: false }
    })
    out.link = link

    // STEP 4: real-click it
    if (link.found) {
      const dlPromise = page.waitForEvent("download", { timeout: 18000 }).catch(() => null)
      await page.click('a[href*="RecurringtasksActive"]', { force: true, timeout: 8000 }).catch((e:any)=>{ out.clickErr = String(e?.message||e).slice(0,100) })
      const dl = (await dlPromise) || popupDownload
      out.gotDownload = Boolean(dl)
      if (dl) {
        const p = await dl.path()
        out.downloadFilename = dl.suggestedFilename()
        if (p) {
          const buf = await readFile(p)
          out.byteLength = buf.length
          const body = buf.subarray(0,2).toString("latin1") === "PK" ? null : buf.toString("utf8")
          if (body) {
            const root = parse(body); const rows = root.querySelectorAll("tr")
            const hdr = rows.find((r:any)=> r.text.includes("Cust ID"))
            out.dataRows = rows.length
            out.headerFound = !!hdr
          }
        }
      }
    }
    out.netlog = netlog
    return out
  } finally {
    await browser.close()
  }
}

//bun-extra-requirements:
//playwright@1.40.0

// Probe (read-only): instrument the FULL browser<->server exchange while driving
// a real click of the "Recurring Task Detail - Active Only" report, to learn how
// the browser fetches it (precursor requests? POST body? headers?) so we can
// replicate it cold for the ION API. Logs every .cfm/report request + response.

import { chromium } from "playwright@1.40.0"
import { readFile } from "fs/promises"

export async function main() {
  const LOGIN_URL = await (await import("windmill-client")).getVariable("f/ION/LOGIN_URL")
  const USERNAME = await (await import("windmill-client")).getVariable("f/ION/USERNAME")
  const PASSWORD = await (await import("windmill-client")).getVariable("f/ION/PASSWORD")

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox","--single-process","--no-zygote","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"],
  })
  const netlog: any[] = []
  const relevant = (u: string) => u.includes("ionpoolcare.com") && /(\/reports\/|taskList\.cfm|Recurringtasks|woReports)/i.test(u)

  try {
    const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })

    const wire = (p: any, label: string) => {
      p.on("request", (req: any) => {
        const u = req.url()
        if (!relevant(u)) return
        const h = req.headers()
        netlog.push({ ev: "req", from: label, method: req.method(), url: u.slice(0, 220),
          referer: h["referer"], secFetchMode: h["sec-fetch-mode"], secFetchDest: h["sec-fetch-dest"],
          accept: h["accept"]?.slice(0, 60), postData: req.postData()?.slice(0, 600) })
      })
      p.on("response", (res: any) => {
        const u = res.url()
        if (!relevant(u)) return
        netlog.push({ ev: "res", from: label, status: res.status(), contentType: res.headers()["content-type"], url: u.slice(0, 220) })
      })
    }

    let popupDownload: any = null
    let popupContentPreview: string | null = null
    context.on("page", async (pop: any) => {
      wire(pop, "popup")
      pop.on("download", (d: any) => { popupDownload = d })
    })

    const page = await context.newPage()
    wire(page, "main")

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

    // Drive UI to load the report-listing page the way the app does
    await page.evaluate(() => {
      // @ts-ignore
      if (typeof ColdFusionNavigate === "function") ColdFusionNavigate("/tasks/taskList.cfm", "pageContent")
    })
    await page.waitForTimeout(4000)

    const linkInfo = await page.evaluate(() => {
      const a = document.querySelector('a[href*="RecurringtasksActive"]') as HTMLAnchorElement | null
      if (a) return { found: true, href: a.getAttribute("href"), target: a.getAttribute("target") }
      const anchors = Array.from(document.querySelectorAll("a")).map(x => (x as HTMLAnchorElement).getAttribute("href") || "").filter(h => h.includes("reports") || h.includes(".cfm")).slice(0, 25)
      return { found: false, bodyLen: document.body.innerHTML.length, reportishLinks: anchors }
    })

    const dlPromise = page.waitForEvent("download", { timeout: 18000 }).catch(() => null)
    const popupPromise = context.waitForEvent("page", { timeout: 18000 }).catch(() => null)
    if (linkInfo.found) {
      await page.evaluate(() => { (document.querySelector('a[href*="RecurringtasksActive"]') as HTMLElement)?.click() })
    }
    const mainDl = await dlPromise
    const popup = mainDl ? null : await popupPromise
    if (popup) {
      await popup.waitForTimeout(3000).catch(() => {})
      try { popupContentPreview = (await popup.content()).slice(0, 600) } catch {}
    }

    const out: any = { ionOrigin, linkInfo, gotMainDownload: Boolean(mainDl), gotPopup: Boolean(popup), gotPopupDownload: Boolean(popupDownload) }
    const dl = mainDl || popupDownload
    if (dl) {
      const p = await dl.path()
      out.downloadFilename = dl.suggestedFilename()
      if (p) {
        const buf = await readFile(p)
        out.byteLength = buf.length
        out.head = buf.subarray(0, 24).toString("latin1")
        out.bodyPreview = buf.subarray(0, 2).toString("latin1") === "PK" ? "(binary xlsx)" : buf.toString("utf8").slice(0, 600)
      }
    } else if (popupContentPreview) {
      out.popupContentPreview = popupContentPreview
    }
    out.netlog = netlog
    return out
  } finally {
    await browser.close()
  }
}

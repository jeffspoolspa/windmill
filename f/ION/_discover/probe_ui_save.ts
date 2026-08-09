/**
 * f/ION/_discover/probe_ui_save — drive a REAL browser save of the
 * boundary-test task's EndsOn and RECORD the exact request the UI fires.
 * Three headless POST variants failed silently (200 + a bounced page);
 * this observes the truth instead of guessing a fourth time.
 *
 * Writes: sets EndsOn=2026-08-09 on the THROWAWAY task 6040821 — the
 * exact save Carter has attempted three times in this boundary test.
 */
import "playwright@1.40.0"
import { chromium } from "playwright@1.40.0"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

type Resource = { ion: object }

const BUNDLED_CHROMIUM = "/usr/lib/ms-playwright/chromium-1091/chrome-linux/chrome"
function chromiumExecutable(): string {
  try {
    const fs = require("fs")
    if (fs.existsSync(BUNDLED_CHROMIUM)) return BUNDLED_CHROMIUM
  } catch { /* fall through */ }
  return "/usr/bin/chromium"
}

export async function main(ion: Resource["ion"], ionTaskId = "6040821", ionCustId = "2581392", endsOn = "2026-08-09") {
  const session = await getOrRefreshSession(ion)
  const browser = await chromium.launch({ headless: true, executablePath: chromiumExecutable(), args: ["--no-sandbox"] })
  try {
    const context = await browser.newContext({ ignoreHTTPSErrors: true })
    await context.addCookies(session.cookies.map((c: { name: string; value: string; domain: string; path?: string }) => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path ?? "/",
    })))
    const page = await context.newPage()
    const captured: { method: string; url: string; postData: string | null }[] = []
    page.on("request", (r) => {
      if (r.method() === "POST" || r.url().includes("addTask")) {
        captured.push({ method: r.method(), url: r.url(), postData: r.postData()?.slice(0, 2500) ?? null })
      }
    })
    await page.goto(`${session.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`, { waitUntil: "domcontentloaded", timeout: 30000 })
    await page.goto(`${session.ionOrigin}/tasks/addTask.cfm?EventID=${ionTaskId}&isIFrame=1`, { waitUntil: "domcontentloaded", timeout: 30000 })
    await page.fill("#EndsOn", endsOn)
    await Promise.all([
      page.waitForLoadState("networkidle", { timeout: 25000 }).catch(() => null),
      page.click('input[name="Submit"]'),
    ])
    await page.waitForTimeout(3000)
    const finalUrl = page.url()
    // read back
    await page.goto(`${session.ionOrigin}/tasks/addTask.cfm?EventID=${ionTaskId}&isIFrame=1`, { waitUntil: "domcontentloaded", timeout: 30000 })
    const endsOnNow = await page.inputValue("#EndsOn").catch(() => "(unreadable)")
    return { captured, finalUrl, endsOnNow }
  } finally {
    await browser.close()
  }
}

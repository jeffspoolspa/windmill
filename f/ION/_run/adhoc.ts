//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
//node-html-parser@6.1.13
import { chromium } from "playwright@1.40.0"
import { parse } from "node-html-parser"
import * as wmill from "windmill-client"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: download ION's consumables_detail report (consumablesDetailByTech.cfm) for ALL of June
// (every day, not just service days) and show SUGARMILL rows -- to find the 8 liquid chlorine and
// see the date they were recorded. Inlined from f/ION/consumables_usage/d (avoids import re-lock).
export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const start_date = "2026-06-01", end_date = "2026-06-30"
  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ['--no-sandbox', '--single-process', '--no-zygote', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
  })
  const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })
  const page = await context.newPage()
  try {
    await page.goto(ion.loginUrl)
    await page.locator('#txtUserName').fill(ion.username)
    await page.locator('#txtPassword').fill(ion.password)
    await page.locator('button:has-text("Log In")').click()
    await page.waitForLoadState('networkidle')
    await page.locator('button[data-bs-target="#navbarToggleContent"]').click()
    await page.locator('text=ION POOL CARE').click()
    await page.waitForLoadState('networkidle')
    try { await page.locator('#MyPrintWin .x-tool-close').click({ timeout: 2000 }) } catch {}
    await page.locator('#menuItem13 a').click()
    await page.locator('.ovalbutton:has-text("Service Reports")').click()
    await page.waitForTimeout(1000)
    await page.locator('#rptStart').evaluate((el, val) => { (el as HTMLInputElement).value = val as string; el.dispatchEvent(new Event('change', { bubbles: true })) }, start_date)
    await page.locator('#rptEnd').evaluate((el, val) => { (el as HTMLInputElement).value = val as string; el.dispatchEvent(new Event('change', { bubbles: true })) }, end_date)
    await page.waitForTimeout(2000)
    const downloadPromise = page.waitForEvent('download')
    await page.locator('a[href*="consumablesDetailByTech.cfm"]').first().click()
    const download = await downloadPromise
    const path = await download.path()
    const html = await Bun.file(path!).text()
    const root = parse(html)
    const table = root.querySelector('table')
    if (!table) throw new Error('No table found')
    const rows = table.querySelectorAll('tr').map((r: any) => r.querySelectorAll('td, th').map((c: any) => c.text.trim()))
    const header = rows.slice(0, 4)
    const sugarmill = rows.filter((row: string[]) => row.some((c) => /sugarmill/i.test(c)))
    const chlorine = sugarmill.filter((row: string[]) => row.some((c) => /chlorine/i.test(c)))
    return { total_rows: rows.length, header, sugarmill_count: sugarmill.length, chlorine, sugarmill }
  } finally {
    await browser.close()
  }
}

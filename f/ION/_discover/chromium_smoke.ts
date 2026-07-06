//bun-extra-requirements:
//playwright@1.40.0

// chromium_smoke — DISCOVERY: which launch config still works on the
// chromium worker? (2026-07-06: every loginToIon crashed at launch with
// 'Target page, context or browser has been closed'; the worker image's
// /usr/bin/chromium appears to have changed.) Tries variants and reports
// pass/fail + the browser version, no ION touch at all.
import { chromium } from "playwright@1.40.0"

const BASE_ARGS = [
  "--no-sandbox",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
]

async function attempt(name: string, opts: any) {
  const rec: any = { name }
  let browser: any = null
  try {
    browser = await chromium.launch(opts)
    const ctx = await browser.newContext()
    const page = await ctx.newPage()
    await page.goto("data:text/html,<title>ok</title>", { timeout: 15000 })
    rec.ok = true
    rec.version = browser.version()
  } catch (e: any) {
    rec.ok = false
    rec.error = String(e?.message ?? e).split("\n")[0].slice(0, 200)
  } finally {
    try { await browser?.close() } catch {}
  }
  return rec
}

export async function main() {
  const results: any[] = []
  // what chromium-ish things exist, and what is the wrapper doing?
  const fs = await import("fs")
  const inventory: any = { candidates: [], wrapper_head: null }
  for (const dir of ["/usr/bin", "/usr/lib/chromium", "/usr/lib/chromium-browser", "/opt/chromium", "/usr/local/bin"]) {
    try {
      for (const f of fs.readdirSync(dir)) {
        if (/chrom/i.test(f)) inventory.candidates.push(`${dir}/${f}`)
      }
    } catch {}
  }
  try {
    inventory.wrapper_head = fs.readFileSync("/usr/bin/chromium", "utf8").slice(0, 800)
  } catch (e: any) {
    inventory.wrapper_head = `unreadable: ${e?.message}`
  }
  results.push(inventory)
  for (const cand of inventory.candidates) {
    if (cand === "/usr/bin/chromium") continue
    try {
      const st = fs.statSync(cand)
      if (!st.isFile()) continue
    } catch { continue }
    results.push(await attempt(`direct:${cand}`, {
      executablePath: cand,
      args: [...BASE_ARGS, "--single-process", "--no-zygote"],
    }))
  }
  results.push(await attempt("system_single_process (current prod config)", {
    executablePath: "/usr/bin/chromium",
    args: [...BASE_ARGS, "--single-process", "--no-zygote"],
  }))
  results.push(await attempt("system_no_single_process", {
    executablePath: "/usr/bin/chromium",
    args: BASE_ARGS,
  }))
  results.push(await attempt("bundled_playwright_chromium", { args: BASE_ARGS }))
  return results
}

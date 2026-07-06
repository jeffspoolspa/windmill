//bun-extra-requirements:
//playwright@1.40.0

// pw_launch_probe — the definitive test: can the PLAYWRIGHT LIBRARY (1.40)
// drive the arm64 chromium 120 already sitting in the cache? This is exactly
// what session.ts does, with playwright's correct multi-process flags — not
// the fragile raw --single-process spawn that SIGTRAP'd. If page.goto +
// content works, pointing session.ts at this executablePath fixes ION.
import { chromium } from "playwright@1.40.0"

const EXE = "/tmp/windmill/cache/pw-browsers/chromium-1091/chrome-linux/chrome"

const SESSION_ARGS = [
  "--no-sandbox",
  "--single-process",
  "--no-zygote",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
]
const SAFE_ARGS = [
  "--no-sandbox",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
]

async function tryLaunch(name: string, args: string[]) {
  const rec: any = { name }
  let browser: any = null
  try {
    browser = await chromium.launch({ executablePath: EXE, args, timeout: 45000 })
    const page = await browser.newContext().then((c: any) => c.newPage())
    await page.setContent("<title>t</title><h1 id=x>pinned works</h1>")
    rec.text = await page.locator("#x").textContent()
    rec.ok = rec.text === "pinned works"
    rec.version = browser.version()
  } catch (e: any) {
    rec.ok = false
    rec.error = String(e?.message ?? e).split("\n").slice(0, 3).join(" | ").slice(0, 300)
  } finally {
    try { await browser?.close() } catch {}
  }
  return rec
}

export async function main() {
  const fs = await import("fs")
  const rec: any = { exe: EXE, exe_exists: fs.existsSync(EXE) }
  if (!rec.exe_exists) return rec
  rec.session_args = await tryLaunch("current session.ts args", SESSION_ARGS)
  rec.safe_args = await tryLaunch("multi-process (no --single-process)", SAFE_ARGS)
  return rec
}

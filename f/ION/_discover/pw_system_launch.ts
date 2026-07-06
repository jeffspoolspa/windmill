//bun-extra-requirements:
//playwright@1.40.0

// pw_system_launch — the cheap high-value test. System chromium (v150) EXECS
// and only SIGTRAPs on render; session.ts passes --single-process, which newer
// chromium handles badly. Launch /usr/bin/chromium via the playwright LIBRARY
// with and without --single-process and render. If multi-process renders, the
// fix is a one-line args change in session.ts — no download, no support.
// Also frees the /tmp cruft my earlier download probes left (ENOSPC).
import { chromium } from "playwright@1.40.0"

async function launch(name: string, args: string[]) {
  const rec: any = { name, args }
  let browser: any = null
  try {
    browser = await chromium.launch({ executablePath: "/usr/bin/chromium", args, timeout: 45000 })
    const page = await browser.newContext().then((c: any) => c.newPage())
    await page.setContent("<h1 id=x>ok</h1>")
    rec.text = await page.locator("#x").textContent()
    rec.ok = rec.text === "ok"
    rec.version = browser.version()
  } catch (e: any) {
    rec.ok = false
    rec.error = String(e?.message ?? e).split("\n").slice(0, 4).join(" | ").slice(0, 400)
  } finally {
    try { await browser?.close() } catch {}
  }
  return rec
}

export async function main() {
  const fs = await import("fs")
  // free the disk my download probes filled
  for (const d of [
    "/tmp/pw-browsers",
    "/tmp/pinned-chromium-1091",
    "/tmp/windmill/cache/pw-browsers",
    "/tmp/windmill/cache/pinned-chromium-1091",
  ]) {
    try { fs.rmSync(d, { recursive: true, force: true }) } catch {}
  }

  const rec: any = {}
  rec.multiprocess = await launch("multi-process (drop --single-process)", [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
  ])
  rec.current_args = await launch("current session.ts args (--single-process)", [
    "--no-sandbox",
    "--single-process",
    "--no-zygote",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
  ])
  return rec
}

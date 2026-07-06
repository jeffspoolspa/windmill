//bun-extra-requirements:
//playwright@1.40.0

// pw_selfcontained — obtain arm64 chromium 120 (playwright 1.40's build) via
// the LIVE CDN (azureedge is dead → cdn.playwright.dev) and launch it via the
// playwright library in the SAME job, so nothing depends on cross-job cache.
// If it renders, session.ts self-provisions the same way and ION is fixed
// app-side.
import { chromium } from "playwright@1.40.0"

async function sh(cmd: string[], env: Record<string, string>, timeoutMs = 240000) {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe", env: { ...process.env, ...env } })
  const t = setTimeout(() => proc.kill(), timeoutMs)
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const code = await proc.exited
  clearTimeout(t)
  return { code, out: out.split("\n").filter(Boolean).slice(-4).join(" | ").slice(0, 400),
           err: err.split("\n").filter(Boolean).slice(-4).join(" | ").slice(0, 400) }
}

function findChrome(fs: any, root: string): string | null {
  try {
    for (const d of fs.readdirSync(root)) {
      const cand = `${root}/${d}/chrome-linux/chrome`
      if (fs.existsSync(cand)) return cand
    }
  } catch {}
  return null
}

export async function main() {
  const fs = await import("fs")
  const rec: any = {}
  const BROWSERS = "/tmp/pw-browsers"
  try { fs.rmSync(BROWSERS, { recursive: true, force: true }) } catch {}
  fs.mkdirSync(BROWSERS, { recursive: true })

  // install via the LIVE cdn (azureedge deprecated)
  rec.install = await sh(
    [process.execPath, "x", "playwright@1.40.0", "install", "chromium"],
    {
      PLAYWRIGHT_BROWSERS_PATH: BROWSERS,
      PLAYWRIGHT_DOWNLOAD_HOST: "https://cdn.playwright.dev",
    },
  )
  const exe = findChrome(fs, BROWSERS)
  rec.exe = exe
  if (!exe) return rec

  rec.version = await sh([exe, "--version"], {}, 20000)
  const args = [
    "--no-sandbox", "--disable-setuid-sandbox",
    "--disable-dev-shm-usage", "--disable-gpu",
  ]
  let browser: any = null
  try {
    browser = await chromium.launch({ executablePath: exe, args, timeout: 45000 })
    const page = await browser.newContext().then((c: any) => c.newPage())
    await page.setContent("<h1 id=x>pinned works</h1>")
    rec.text = await page.locator("#x").textContent()
    rec.render_ok = rec.text === "pinned works"
    rec.browser_version = browser.version()
  } catch (e: any) {
    rec.render_ok = false
    rec.launch_error = String(e?.message ?? e).split("\n").slice(0, 4).join(" | ").slice(0, 400)
  } finally {
    try { await browser?.close() } catch {}
  }
  return rec
}

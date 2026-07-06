//bun-extra-requirements:
//playwright@1.40.0

// chromium120_render — the decisive test: does arm64 chromium 120 (the build
// playwright 1.40 drives, the version that worked historically) RENDER under
// the CURRENT nsjail? Direct zip download (no `bun x` toolchain overhead that
// ENOSPC'd), disk-guarded, extract, delete zip, launch via playwright.
//   renders  -> pure version regression; init-script pin WILL fix it
//   crashes  -> the sandbox itself changed too (even more a platform issue)
import { chromium } from "playwright@1.40.0"

async function sh(cmd: string[], timeoutMs = 120000) {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" })
  const t = setTimeout(() => proc.kill(), timeoutMs)
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const code = await proc.exited
  clearTimeout(t)
  return { code, out: out.slice(0, 200), err: err.split("\n").filter(Boolean).slice(-3).join(" | ").slice(0, 300) }
}

// build-1091 chromium (=120.0.6099.28, the playwright@1.40 match), arm64.
const URLS = [
  "https://cdn.playwright.dev/dbazure/download/playwright/builds/chromium/1091/chromium-linux-arm64.zip",
  "https://playwright.download.prss.microsoft.com/dbazure/download/playwright/builds/chromium/1091/chromium-linux-arm64.zip",
  "https://playwright.azureedge.net/builds/chromium/1091/chromium-linux-arm64.zip",
]

export async function main() {
  const fs = await import("fs")
  const rec: any = {}
  const base = "/tmp/c120"
  // hard clean everything my probes may have left, on whatever pod we land on
  for (const d of ["/tmp/c120", "/tmp/pw-browsers", "/tmp/pinned-chromium-1091",
                   "/tmp/windmill/cache/pinned-chromium-1091", "/tmp/windmill/cache/pw-browsers"]) {
    try { fs.rmSync(d, { recursive: true, force: true }) } catch {}
  }
  const df = await sh(["sh", "-c", "df -m /tmp | tail -1 | awk '{print $4}'"])
  rec.tmp_free_mb = parseInt(df.out.trim(), 10) || null
  if (rec.tmp_free_mb != null && rec.tmp_free_mb < 620) {
    rec.bail = `only ${rec.tmp_free_mb}MB free — need a cleaner pod`
    return rec
  }

  // find a live URL (azureedge is deprecated -> 400)
  let url: string | null = null
  for (const u of URLS) {
    try {
      const h = await fetch(u, { headers: { Range: "bytes=0-0" } })
      rec[`probe_${u.split("/")[2]}`] = h.status
      if (h.status === 200 || h.status === 206) { url = u; break }
    } catch (e: any) { rec[`probe_err`] = String(e?.message).slice(0, 100) }
  }
  rec.url = url
  if (!url) { rec.bail = "no live CDN url for arm64 build 1091"; return rec }

  fs.mkdirSync(base, { recursive: true })
  const zip = `${base}/c.zip`
  const resp = await fetch(url)
  rec.download_status = resp.status
  if (!resp.ok) return rec
  await Bun.write(zip, await resp.arrayBuffer())
  rec.zip_mb = Math.round(fs.statSync(zip).size / 1e6)
  const ex = await sh(["python3", "-m", "zipfile", "-e", zip, base])
  rec.extract_code = ex.code
  try { fs.rmSync(zip) } catch {}       // free the zip before launch
  const exe = `${base}/chrome-linux/chrome`
  try { fs.chmodSync(exe, 0o755) } catch {}
  for (const h of ["chrome_crashpad_handler", "chrome_sandbox"]) {
    try { fs.chmodSync(`${base}/chrome-linux/${h}`, 0o755) } catch {}
  }
  rec.exe_exists = fs.existsSync(exe)
  if (!rec.exe_exists) return rec
  rec.version = (await sh([exe, "--version"], 20000)).out.trim()

  let browser: any = null
  try {
    browser = await chromium.launch({
      executablePath: exe,
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
      timeout: 45000,
    })
    const page = await browser.newContext().then((c: any) => c.newPage())
    await page.setContent("<h1 id=x>chromium 120 renders</h1>")
    rec.text = await page.locator("#x").textContent()
    rec.render_ok = rec.text === "chromium 120 renders"
  } catch (e: any) {
    rec.render_ok = false
    rec.launch_error = String(e?.message ?? e).split("\n").slice(0, 4).join(" | ").slice(0, 400)
  } finally {
    try { await browser?.close() } catch {}
    try { fs.rmSync(base, { recursive: true, force: true }) } catch {}
  }
  return rec
}

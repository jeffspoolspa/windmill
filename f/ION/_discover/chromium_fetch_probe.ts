// chromium_fetch_probe — DISCOVERY: can a JOB self-provision the pinned
// chromium (playwright build 1091, the 1.40 match) without any worker
// config? Downloads the official build archive, extracts it (python3
// zipfile — no unzip dependency), and test-renders. Also probes whether the
// shared worker cache dir is job-writable so the ~130MB download can be
// reused across jobs instead of re-fetched per login.

const BUILD_URL =
  "https://playwright.azureedge.net/builds/chromium/1091/chromium-linux.zip"

async function sh(cmd: string[], timeoutMs = 120000) {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" })
  const t = setTimeout(() => proc.kill(), timeoutMs)
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const code = await proc.exited
  clearTimeout(t)
  return { code, out: out.slice(0, 300), err: err.split("\n").filter(Boolean).slice(-5).join("\n").slice(0, 500) }
}

export async function main() {
  const fs = await import("fs")
  const rec: any = {}

  // 1) which candidate dirs can this job write?
  rec.writable = {}
  for (const d of ["/tmp/windmill/cache", "/tmp/windmill", "/tmp"]) {
    try {
      fs.mkdirSync(`${d}/.probe_w`, { recursive: true })
      fs.rmdirSync(`${d}/.probe_w`)
      rec.writable[d] = true
    } catch {
      rec.writable[d] = false
    }
  }
  const base = rec.writable["/tmp/windmill/cache"]
    ? "/tmp/windmill/cache/pinned-chromium-1091"
    : "/tmp/pinned-chromium-1091"
  rec.install_dir = base
  const exe = `${base}/chrome-linux/chrome`

  // always start clean — a prior partial/corrupt extract poisons the cache
  try { fs.rmSync(base, { recursive: true, force: true }) } catch {}

  // 2) download + extract if missing
  if (!fs.existsSync(exe)) {
    const t0 = Date.now()
    const resp = await fetch(BUILD_URL)
    rec.download_status = resp.status
    if (!resp.ok) return rec
    fs.mkdirSync(base, { recursive: true })
    const zipPath = `${base}/build.zip`
    await Bun.write(zipPath, await resp.arrayBuffer())
    rec.download_mb = Math.round(fs.statSync(zipPath).size / 1e6)
    rec.download_s = Math.round((Date.now() - t0) / 1000)
    rec.extract = await sh(["python3", "-m", "zipfile", "-e", zipPath, base])
    if (rec.extract.code !== 0) return rec
    fs.rmSync(zipPath)
    // zipfile doesn't preserve the executable bit
    await sh(["chmod", "-R", "a+rX", base])
    await sh(["chmod", "+x", exe])
    for (const helper of ["chrome_crashpad_handler", "chrome_sandbox", "chrome-wrapper", "xdg-settings", "xdg-mime"]) {
      try { fs.chmodSync(`${base}/chrome-linux/${helper}`, 0o755) } catch {}
    }
  } else {
    rec.cached = true
  }

  // 3) does it render?
  rec.exe_exists = fs.existsSync(exe)
  if (rec.exe_exists) {
    const st = fs.statSync(exe)
    rec.exe_bytes = st.size
    rec.exe_mode = (st.mode & 0o777).toString(8)
    const fd = fs.openSync(exe, "r")
    const magic = Buffer.alloc(4)
    fs.readSync(fd, magic, 0, 4, 0)
    fs.closeSync(fd)
    rec.exe_magic = magic.toString("hex") // ELF = 7f454c46
    try { rec.version = await sh([exe, "--version"], 20000) } catch (e: any) {
      rec.version = { spawn_error: String(e?.message ?? e).slice(0, 150) }
    }
    try { rec.render = await sh(
      [
        exe,
        "--headless",
        "--no-sandbox",
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--single-process",
        "--no-zygote",
        "--dump-dom",
        "data:text/html,<title>smoketest</title><p>pinned works</p>",
      ],
      60000,
    ) } catch (e: any) {
      rec.render = { spawn_error: String(e?.message ?? e).slice(0, 150) }
    }
    rec.render_ok = !!rec.render?.out?.includes?.("pinned works")
  }
  return rec
}

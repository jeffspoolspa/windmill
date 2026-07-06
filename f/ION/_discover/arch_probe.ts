// arch_probe — what CPU arch is the chromium worker, and does the RIGHT-arch
// playwright chromium build (1091 = the 1.40 match) render from /tmp (which we
// just proved is executable)? If yes, self-provisioning works — no worker
// config, no support.
async function sh(cmd: string[], timeoutMs = 90000) {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" })
  const t = setTimeout(() => proc.kill(), timeoutMs)
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const code = await proc.exited
  clearTimeout(t)
  return { code, out: out.slice(0, 200), err: err.split("\n").filter(Boolean).slice(-4).join("\n").slice(0, 400) }
}

export async function main() {
  const fs = await import("fs")
  const os = await import("os")
  const rec: any = { node_arch: process.arch, os_arch: os.arch(), uname: await sh(["uname", "-m"], 8000) }

  // playwright build 1091 (playwright@1.40 chromium). Pick URL by arch.
  const isArm = /arm|aarch/i.test(process.arch)
  const url = isArm
    ? "https://playwright.azureedge.net/builds/chromium/1091/chromium-linux-arm64.zip"
    : "https://playwright.azureedge.net/builds/chromium/1091/chromium-linux.zip"
  rec.chosen_url = url

  const base = "/tmp/windmill/cache/pinned-chromium-1091"
  const exe = `${base}/chrome-linux/chrome`
  try { fs.rmSync(base, { recursive: true, force: true }) } catch {}
  const resp = await fetch(url)
  rec.download_status = resp.status
  if (!resp.ok) return rec
  fs.mkdirSync(base, { recursive: true })
  const zipPath = `${base}/b.zip`
  await Bun.write(zipPath, await resp.arrayBuffer())
  rec.download_mb = Math.round(fs.statSync(zipPath).size / 1e6)
  rec.extract = await sh(["python3", "-m", "zipfile", "-e", zipPath, base])
  fs.rmSync(zipPath)
  await sh(["chmod", "-R", "a+rX", base])
  try { fs.chmodSync(exe, 0o755) } catch {}
  for (const h of ["chrome_crashpad_handler", "chrome_sandbox"]) {
    try { fs.chmodSync(`${base}/chrome-linux/${h}`, 0o755) } catch {}
  }

  rec.exe_exists = fs.existsSync(exe)
  if (rec.exe_exists) {
    rec.version = await sh([exe, "--version"], 20000)
    rec.render = await sh([
      exe, "--headless", "--no-sandbox", "--disable-gpu",
      "--disable-dev-shm-usage", "--single-process", "--no-zygote",
      "--dump-dom", "data:text/html,<title>t</title><p>pinned works</p>",
    ], 60000)
    rec.render_ok = !!rec.render?.out?.includes?.("pinned works")
  }
  return rec
}

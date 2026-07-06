// pw_install_probe — let playwright@1.40.0's OWN installer fetch the correct
// arm64 chromium into a /tmp path (proven executable), then render-test it.
// Offloads arch+revision+host URL resolution to playwright itself.
async function sh(cmd: string[], env: Record<string, string> = {}, timeoutMs = 240000) {
  const proc = Bun.spawn(cmd, {
    stdout: "pipe",
    stderr: "pipe",
    env: { ...process.env, ...env },
  })
  const t = setTimeout(() => proc.kill(), timeoutMs)
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const code = await proc.exited
  clearTimeout(t)
  return {
    code,
    out: out.split("\n").filter(Boolean).slice(-8).join("\n").slice(0, 600),
    err: err.split("\n").filter(Boolean).slice(-8).join("\n").slice(0, 600),
  }
}

export async function main() {
  const fs = await import("fs")
  const BROWSERS = "/tmp/windmill/cache/pw-browsers"
  const rec: any = { browsers_path: BROWSERS }
  try { fs.rmSync(BROWSERS, { recursive: true, force: true }) } catch {}
  fs.mkdirSync(BROWSERS, { recursive: true })

  // playwright's installer resolves the right arm64 build + CDN.
  // bunx isn't on PATH; use the running bun binary's `x` subcommand.
  rec.bun = process.execPath
  rec.install = await sh(
    [process.execPath, "x", "playwright@1.40.0", "install", "chromium"],
    { PLAYWRIGHT_BROWSERS_PATH: BROWSERS },
  )

  // find the chrome binary it dropped
  let exe: string | null = null
  try {
    for (const d of fs.readdirSync(BROWSERS)) {
      const cand = `${BROWSERS}/${d}/chrome-linux/chrome`
      if (fs.existsSync(cand)) { exe = cand; break }
    }
  } catch {}
  rec.exe = exe

  if (exe) {
    rec.version = await sh([exe, "--version"], {}, 20000)
    rec.render = await sh(
      [exe, "--headless", "--no-sandbox", "--disable-gpu",
       "--disable-dev-shm-usage", "--single-process", "--no-zygote",
       "--dump-dom", "data:text/html,<title>t</title><p>pinned works</p>"],
      {}, 60000,
    )
    rec.render_ok = !!rec.render?.out?.includes?.("pinned works")
  }
  return rec
}

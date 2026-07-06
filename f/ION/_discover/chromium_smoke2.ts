// chromium_smoke2 — DISCOVERY, zero deps: is the worker's chromium binary
// itself functional? Spawns it headless with --dump-dom on a data: URL and
// captures stdout/stderr/exit code. If this works, the browser is fine and
// the failure is playwright 1.40's handshake; if this also dies, the image's
// chromium package is broken and the fix is Windmill-side.

async function tryRun(name: string, cmd: string[], timeoutMs = 30000) {
  const rec: any = { name, cmd: cmd.join(" ") }
  try {
    const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" })
    const timer = setTimeout(() => {
      rec.timed_out = true
      proc.kill()
    }, timeoutMs)
    const [out, err] = await Promise.all([
      new Response(proc.stdout).text(),
      new Response(proc.stderr).text(),
    ])
    rec.exit_code = await proc.exited
    clearTimeout(timer)
    rec.stdout_head = out.slice(0, 300)
    rec.stdout_has_title = out.includes("smoketest")
    rec.stderr_tail = err.split("\n").filter(Boolean).slice(-8).join("\n").slice(0, 900)
  } catch (e: any) {
    rec.spawn_error = String(e?.message ?? e).slice(0, 200)
  }
  return rec
}

export async function main() {
  const base = [
    "--headless",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--dump-dom",
    "data:text/html,<title>smoketest</title><p>hi</p>",
  ]
  const results = []
  results.push(await tryRun("wrapper --dump-dom", ["/usr/bin/chromium", ...base]))
  results.push(
    await tryRun("real binary --dump-dom", ["/usr/lib/chromium/chromium", ...base]),
  )
  results.push(
    await tryRun("real binary single-process", [
      "/usr/lib/chromium/chromium",
      "--single-process",
      "--no-zygote",
      ...base,
    ]),
  )
  results.push(await tryRun("version", ["/usr/lib/chromium/chromium", "--version"], 10000))
  results.push(
    await tryRun("mitigation flags", [
      "/usr/lib/chromium/chromium",
      "--headless",
      "--no-sandbox",
      "--no-zygote",
      "--disable-gpu",
      "--disable-dev-shm-usage",
      "--disable-crashpad",
      "--disable-breakpad",
      "--disable-crash-reporter",
      "--ozone-platform=headless",
      "--disable-features=DBusClient",
      "--in-process-gpu",
      "--dump-dom",
      "data:text/html,<title>smoketest</title>",
    ]),
  )
  return results
}

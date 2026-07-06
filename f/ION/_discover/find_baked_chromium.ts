//bun-extra-requirements:
//playwright@1.40.0

// find_baked_chromium — search the whole (read-only) worker image for a
// pre-baked chromium/chrome executable other than the broken /usr/bin one.
// Browser-automation images often ship playwright's browsers at a fixed path
// (e.g. /ms-playwright/chromium-XXXX/chrome-linux/chrome) — read-only but
// EXECUTABLE, and immune to the apt-150 bump. If one renders via playwright,
// session.ts just points at it: no download, no init script, no support.
import { chromium } from "playwright@1.40.0"

async function sh(cmd: string[], timeoutMs = 60000) {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" })
  const t = setTimeout(() => proc.kill(), timeoutMs)
  const [out, err] = await Promise.all([
    new Response(proc.stdout).text(),
    new Response(proc.stderr).text(),
  ])
  const code = await proc.exited
  clearTimeout(t)
  return { code, out, err: err.slice(0, 200) }
}

export async function main() {
  const rec: any = { found: [], rendered: null }
  // find every chrome/chromium executable on the whole image
  const roots = ["/ms-playwright", "/usr/lib", "/usr/local", "/opt", "/root", "/home"]
  const search = await sh(
    ["sh", "-c",
     `find ${roots.join(" ")} / -maxdepth 6 -type f \\( -name chrome -o -name 'chrome-*' -o -name chromium -o -name headless_shell \\) 2>/dev/null | sort -u | head -40`],
    90000,
  )
  const candidates = search.out.split("\n").map((s) => s.trim()).filter(Boolean)
  rec.raw_candidates = candidates

  const fs = await import("fs")
  for (const c of candidates) {
    const r: any = { path: c }
    try {
      const st = fs.statSync(c)
      r.mb = Math.round(st.size / 1e6)
      r.exec_bit = !!(st.mode & 0o111)
      if (r.mb >= 50) { // an actual chrome binary, not a wrapper
        const v = await sh([c, "--version"], 15000)
        r.version = v.out.trim().slice(0, 60) || v.err.slice(0, 80)
        r.execs = v.code === 0
      }
    } catch (e: any) {
      r.error = String(e?.message).slice(0, 100)
    }
    rec.found.push(r)
  }

  // render-test the first big binary that execs and isn't the broken 150
  const target = rec.found.find(
    (r: any) => r.execs && r.mb >= 50 && r.path !== "/usr/bin/chromium" &&
                !/ 15\d\./.test(r.version || ""),
  )
  rec.render_target = target?.path ?? null
  if (target) {
    let browser: any = null
    try {
      browser = await chromium.launch({
        executablePath: target.path,
        args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        timeout: 45000,
      })
      const page = await browser.newContext().then((c: any) => c.newPage())
      await page.setContent("<h1 id=x>baked chromium renders</h1>")
      rec.rendered = (await page.locator("#x").textContent()) === "baked chromium renders"
      rec.render_version = browser.version()
    } catch (e: any) {
      rec.rendered = false
      rec.render_error = String(e?.message ?? e).split("\n").slice(0, 3).join(" | ").slice(0, 300)
    } finally {
      try { await browser?.close() } catch {}
    }
  }
  return rec
}

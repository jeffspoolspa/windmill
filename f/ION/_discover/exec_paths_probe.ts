// exec_paths_probe — DISCOVERY: is /tmp REALLY the whole writable world, and
// is it REALLY all noexec? Copies a known-good tiny ELF (/bin/true) into every
// plausible dir, chmod 755, and spawns it. exit 0 => that dir is
// writable+executable => we can drop a pinned chromium there, no worker
// config needed. Reports env hints too.
import * as os from "os"

async function canExec(path: string) {
  try {
    const proc = Bun.spawn([path], { stdout: "ignore", stderr: "pipe" })
    const err = await new Response(proc.stderr).text()
    const code = await proc.exited
    return { ok: code === 0, code, err: err.slice(0, 120) }
  } catch (e: any) {
    return { ok: false, spawn_error: String(e?.message ?? e).slice(0, 120) }
  }
}

export async function main() {
  const fs = await import("fs")
  const rec: any = { env: {}, dirs: {} }
  rec.env.HOME = process.env.HOME ?? null
  rec.env.cwd = process.cwd()
  rec.env.TMPDIR = process.env.TMPDIR ?? null
  rec.env.PLAYWRIGHT_BROWSERS_PATH = process.env.PLAYWRIGHT_BROWSERS_PATH ?? null

  const src = "/bin/true"
  const candidates = [
    process.env.HOME ?? "",
    process.cwd(),
    os.tmpdir(),
    "/tmp",
    "/tmp/windmill",
    "/tmp/windmill/cache",
    "/dev/shm",
    "/var/tmp",
    "/run",
    "/run/user/0",
    "/usr/local/share",
    "/opt",
    "/home",
    "/mnt",
    "/data",
  ].filter(Boolean)

  for (const dir of [...new Set(candidates)]) {
    const r: any = {}
    const dst = `${dir}/.execprobe_bin`
    try {
      fs.mkdirSync(dir, { recursive: true })
      fs.copyFileSync(src, dst)
      fs.chmodSync(dst, 0o755)
      r.copied = true
      Object.assign(r, await canExec(dst))
    } catch (e: any) {
      r.error = String(e?.message ?? e).slice(0, 120)
    } finally {
      try { fs.rmSync(dst) } catch {}
    }
    rec.dirs[dir] = r
  }
  return rec
}

// disk_probe — decides whether self-hosting a browser is even possible.
// Cleans my download cruft, then reports free space on every writable mount.
// A ~400MB chromium needs headroom in /tmp; if /tmp is tiny, the fix MUST be
// the worker init script (installs to the roomy image layer).
async function sh(cmd: string[]) {
  const proc = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe" })
  const out = await new Response(proc.stdout).text()
  await proc.exited
  return out.slice(0, 800)
}

export async function main() {
  const fs = await import("fs")
  const rec: any = { cleaned: [] }
  for (const d of [
    "/tmp/pw-browsers",
    "/tmp/pinned-chromium-1091",
    "/tmp/windmill/cache/pw-browsers",
    "/tmp/windmill/cache/pinned-chromium-1091",
  ]) {
    try {
      if (fs.existsSync(d)) {
        fs.rmSync(d, { recursive: true, force: true })
        rec.cleaned.push(d)
      }
    } catch (e: any) {
      rec.cleaned.push(`${d} (err: ${e?.message})`)
    }
  }
  rec.df = await sh(["df", "-h"])
  rec.du_tmp = await sh(["sh", "-c", "du -sh /tmp/* 2>/dev/null | sort -rh | head -12"])
  return rec
}

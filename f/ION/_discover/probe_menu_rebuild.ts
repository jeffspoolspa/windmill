//bun-extra-requirements:
//playwright@1.40.0

// probe_menu — read-only: enumerate main.cfm's menu + sweep for rebuild
// or billing-run wiring, plus the jobManager surface.
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

export async function main(paths: string[] = ["/main.cfm"]) {
  const cred = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s: any = await getOrRefreshSession(cred)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", Referer: `${o}/main.cfm` }
  const out: Record<string, unknown> = {}
  for (const p of paths) {
    const r = await fetch(`${o}${p}`, { headers: H, redirect: "manual" })
    const html = await r.text()
    out[p] = {
      status: r.status, bytes: html.length,
      menu: [...html.matchAll(/menuItem\d+[^>]{0,200}>[^<]{0,60}|ColdFusionNavigate\('([^']+)'[^)]*\)[^>]{0,80}>([^<]{0,50})/g)].map((m) => m[0].replace(/\s+/g, " ").slice(0, 220)).slice(0, 60),
      rebuild: html.split("\n").filter((l) => /rebuild|regen|reprocess|recalc/i.test(l)).map((l) => l.trim().slice(0, 260)).slice(0, 12),
      jobmgr: html.split("\n").filter((l) => /jobManager|jobid/i.test(l)).map((l) => l.trim().slice(0, 240)).slice(0, 8),
    }
  }
  return out
}

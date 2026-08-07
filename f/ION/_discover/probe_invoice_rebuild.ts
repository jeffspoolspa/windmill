//bun-extra-requirements:
//playwright@1.40.0

// probe_invoice_rebuild — DISCOVERY (read-only): find the endpoint behind
// ION's "rebuild invoices" action so the transactions pull can refresh
// ION's own data first (RULED 2026-08-07). Fetches the Accounting /
// receivables surfaces and sweeps for build/rebuild/generate wiring:
// form actions, ColdFusionNavigate targets, JS handlers and their .cfm
// posts. NO clicks, NO posts — this only reads pages.
import "playwright@1.40.0"

import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import type { IonResource } from "/f/ION/_lib/session"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

function sweep(html: string) {
  return {
    bytes: html.length,
    build_lines: html.split("\n")
      .filter((l) => /rebuild|buildinv|generate|createinv|batch/i.test(l) && !/\.css|favicon/i.test(l))
      .map((l) => l.trim().slice(0, 300)).slice(0, 20),
    forms: [...html.matchAll(/<form[^>]{0,300}/gi)].map((m) => m[0].replace(/\s+/g, " ").slice(0, 280)).slice(0, 12),
    nav_targets: [...new Set([...html.matchAll(/ColdFusionNavigate\s*\(\s*['"]([^'"]+)['"]/g)].map((m) => m[1]))].slice(0, 25),
    onclick: [...html.matchAll(/onclick\s*=\s*["'][^"']{0,220}/gi)].map((m) => m[0].replace(/\s+/g, " ").slice(0, 220)).slice(0, 20),
    js_posts: html.split("\n")
      .filter((l) => /\.cfm/.test(l) && /(post|submit|ajax|href|action|location)/i.test(l))
      .map((l) => l.trim().slice(0, 260)).slice(0, 25),
    cfms: [...new Set([...html.matchAll(/([A-Za-z0-9_\/.-]+\.cfm)/g)].map((m) => m[1]))].slice(0, 40),
  }
}

export async function main(extra_paths: string[] = [], ion: IonResource | null = null) {
  const cred = ion ?? {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(cred)
  const o = s.ionOrigin
  const H = {
    Cookie: cookieHeader(s),
    "User-Agent": "Mozilla/5.0",
    Accept: "text/html, */*",
    Referer: `${o}/main.cfm`,
  }
  const targets = [
    "/receivables/receivables.cfm",
    "/receivables/invoices.cfm",
    "/receivables/billing.cfm",
    "/receivables/monthlyBilling.cfm",
    ...extra_paths,
  ]
  const rec: Record<string, unknown> = {}
  for (const t of targets) {
    try {
      const r = await fetch(`${o}${t}`, { headers: H, redirect: "manual" })
      const html = await r.text()
      rec[t] = { status: r.status, ...sweep(html) }
    } catch (e: any) {
      rec[t] = { error: String(e).slice(0, 140) }
    }
  }
  return rec
}

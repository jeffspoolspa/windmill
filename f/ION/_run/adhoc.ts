//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: mine /reports/ServiceRpt.cfm for the consumablesDetailByTech report href + how dates are
// passed (rptStart/rptEnd form or query params). Direct session fetch, no UI.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const around = (body: string, re: RegExp, pad = 260) => {
  const out: string[] = []
  for (const m of body.matchAll(re)) { const i = m.index || 0; out.push(body.slice(Math.max(0, i - pad), i + pad).replace(/\s+/g, " ")) }
  return out.slice(0, 10)
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const grab = async (u: string) => { const r = await fetch(`${o}${u}`, { headers: H, redirect: "manual" }); return { status: r.status, body: await r.text() } }
  const r = await grab("/reports/ServiceRpt.cfm")
  const body = r.body
  const consumablesCtx = around(body, /consumabl/gi)
  const cfmPaths = [...new Set([...body.matchAll(/['"]([\/A-Za-z0-9_.\-]+\.cfm)/g)].map((m) => m[1]))].slice(0, 40)
  const dateCtx = around(body, /rptStart|rptEnd|StartDate|EndDate|name="[^"]*[Dd]ate/gi, 160)
  const hrefs = [...new Set([...body.matchAll(/(?:href|onclick|action)=["'][^"']*consumabl[^"']*["']/gi)].map((m) => m[0]))]
  return { status: r.status, len: body.length, consumables_hrefs: hrefs, consumables_ctx: consumablesCtx, date_ctx: dateCtx, cfm_paths: cfmPaths }
}

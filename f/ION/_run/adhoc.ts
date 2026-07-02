//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: mine /reports/reports.cfm (200) for how the consumables report is invoked -- report path,
// param names, and the Service Reports subpage URL. Direct session fetch, no UI.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const around = (body: string, re: RegExp, pad = 200) => {
  const out: string[] = []
  for (const m of body.matchAll(re)) { const i = m.index || 0; out.push(body.slice(Math.max(0, i - pad), i + pad).replace(/\s+/g, " ")) }
  return out.slice(0, 8)
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const grab = async (u: string) => (await fetch(`${o}${u}`, { headers: H, redirect: "manual" })).text()
  const body = await grab("/reports/reports.cfm")

  const cfmPaths = [...new Set([...body.matchAll(/['"]([\/A-Za-z0-9_.\-]+\.cfm)/g)].map((m) => m[1]))].slice(0, 40)
  const consumablesCtx = around(body, /consumabl/gi)
  const serviceReportCtx = around(body, /service\s*report|ServiceReport|serviceReports/gi)
  const ovalCtx = around(body, /ovalbutton|ColdFusionNavigate|loadReport|rptStart|rptEnd/gi)
  return { cfm_paths: cfmPaths, consumables_ctx: consumablesCtx, service_report_ctx: serviceReportCtx, oval_ctx: ovalCtx }
}

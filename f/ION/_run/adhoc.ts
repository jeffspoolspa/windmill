//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: discover the consumablesDetailByTech.cfm report endpoint by hitting ION directly with the
// saved session (bypasses UI/popups). Report what URLs return, and any consumables report href found.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const cf = s.cfClientId || s.cf_clientid || null
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const get = async (u: string) => {
    const r = await fetch(`${o}${u}`, { headers: H, redirect: "manual" })
    return { url: u, status: r.status, len: (await r.text()).length }
  }
  const grab = async (u: string) => {
    const r = await fetch(`${o}${u}`, { headers: H, redirect: "manual" })
    return await r.text()
  }
  // 1) reports shell + service reports page -- look for the consumables report href/param shape
  const candidates = [
    "/reports/reports.cfm",
    "/reports/serviceReports.cfm",
    `/reports/consumablesDetailByTech.cfm?rptStart=${"2026-06-01"}&rptEnd=${"2026-06-30"}${cf ? `&_cf_clientid=${cf}` : ""}&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1`,
  ]
  const statuses = []
  for (const c of candidates) { try { statuses.push(await get(c)) } catch (e: any) { statuses.push({ url: c, error: String(e?.message ?? e).slice(0, 120) }) } }

  // pull any consumables* hrefs from the two picker pages
  let hrefs: string[] = []
  for (const p of ["/reports/reports.cfm", "/reports/serviceReports.cfm"]) {
    try { const body = await grab(p); hrefs.push(...[...body.matchAll(/href="([^"]*consumabl[^"]*)"/gi)].map((m) => m[1])) } catch {}
  }
  // snippet of the direct report body (first 600 chars) to see if it returned a data table
  let directSnippet = ""
  try { directSnippet = (await grab(candidates[2])).slice(0, 600) } catch (e: any) { directSnippet = "ERR " + String(e?.message ?? e).slice(0, 120) }

  return { has_cf_clientid: !!cf, statuses, consumables_hrefs: [...new Set(hrefs)].slice(0, 20), directSnippet }
}

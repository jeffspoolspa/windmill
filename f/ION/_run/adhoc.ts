//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: mine /reports/transactionRpt.cfm for how the All Transactions XLS link is parameterized --
// any JS around 'allTransactions', the <a> tag itself, the <form> tag, and date-field handlers.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
const around = (body: string, re: RegExp, pad = 300) => {
  const out: string[] = []
  for (const m of body.matchAll(re)) { const i = m.index || 0; out.push(body.slice(Math.max(0, i - pad), i + pad).replace(/\s+/g, " ")) }
  return out.slice(0, 8)
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const body = await (await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H, redirect: "manual" })).text()
  return {
    len: body.length,
    allTransactions_ctx: around(body, /allTransactions/gi, 340),
    form_tag: around(body, /<form[^>]*>/gi, 20),
    xls_anchor: around(body, /_xls\/[A-Za-z]+\.cfm/gi, 220),
    submit_ctx: around(body, /onclick|onsubmit|\.submit\(|type="submit"|SetXLS|buildXLS|reportURL/gi, 160),
  }
}

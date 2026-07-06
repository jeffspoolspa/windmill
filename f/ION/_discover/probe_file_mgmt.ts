//bun-extra-requirements:
//playwright@1.40.0

// probe_file_mgmt — DISCOVERY step 2: addLog.cfm loads /IPC/js/file_management.js
// and photos arrive via AJAX (keyed by EventID hidden input). Fetch that JS,
// extract every .cfm/endpoint URL + ajax call shape, and fetch the addLog page
// raw to pull the exact JS invocation wiring file management to this log.
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

export async function main(
  log_id = "37313791",
  calendar_id = "53802038",
  ion: IonResource | null = null,
) {
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
    Accept: "*/*",
    Referer: `${o}/main.cfm`,
  }
  const rec: any = {}

  // 1) the file-management JS: every URL + fetch/ajax shape in it
  const js = await (await fetch(`${o}/IPC/js/file_management.js`, { headers: H })).text()
  rec.js_bytes = js.length
  rec.js_full = js  // tiny — return it whole

  // 2) the addLog page: JS lines invoking file management (params wiring)
  const url = `${o}/tasks/addLog.cfm?calendarID=${calendar_id}&LogID=${log_id}&source=ServiceLog`
  const html = await (await fetch(url, { headers: H, redirect: "manual" })).text()
  rec.page_bytes = html.length
  // where do baseUrl / module / signed-url service live on the page?
  rec.page_signed = html.split("\n")
    .filter((l) => /getSignedUrl|baseUrl|fileservice|amazonaws|s3|cloudfront|signed/i.test(l))
    .map((l) => l.trim().slice(0, 300)).slice(0, 20)
  // the ajax that fills the Loading... div (any .load/.ajax/.get with a url)
  rec.page_ajax = [...html.matchAll(/\$\.(ajax|get|post|load)\s*\(|\.load\s*\(\s*["'][^"']+["']/g)]
    .slice(0, 25).map((m) => html.slice(Math.max(0, m.index - 60), m.index + 220).replace(/\s+/g, " "))
  // iframes/includes fetched after load
  rec.page_cfm_refs = [...new Set([...html.matchAll(/["'\(]([^"'\(\)]*(?:file|File|upload|photo|image|attach)[^"'\(\)]*\.cfm[^"'\(\)]*)["'\)]/g)].map((m) => m[1]))].slice(0, 15)
  return rec
}

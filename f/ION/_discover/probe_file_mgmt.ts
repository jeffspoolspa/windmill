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
  // every distinct .cfm URL the page references anywhere
  rec.all_cfm = [...new Set([...html.matchAll(/([A-Za-z0-9_\/.-]+\.cfm(?:\?[^"'\s\)<>]*)?)/g)].map((m) => m[1].split("?")[0]))]
  // context around every Loading spinner — what container is it in, what fills it
  rec.loading_ctx = [...html.matchAll(/ajaxWheel/g)].slice(0, 6).map((m) =>
    html.slice(Math.max(0, m.index - 700), m.index + 200).replace(/\s+/g, " ").slice(-850))
  // openFile/downloadFile invocations + ColdFusion ajax bindings
  rec.openfile_calls = [...html.matchAll(/(openFile|downloadFile)\s*\(/g)].length
  rec.cf_bindings = html.split("\n")
    .filter((l) => /ColdFusion\.(Ajax|navigate|Bind)|cfajax|bindTo|AjaxLink/i.test(l))
    .map((l) => l.trim().slice(0, 250)).slice(0, 15)
  return rec
}

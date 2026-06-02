//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main(date_us: string = "05/15/2026") {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` }
  const get = (url: string) => fetch(`${o}${url}`, { headers: H, redirect: "manual" }).then(r => r.text())

  const html = await get(`/home/customerlogs.cfm?_cf_nocache=true&_cf_rc=0`)

  // pull every line/snippet that mentions the grid, its bind, or a cfc data source
  const grab = (re: RegExp) => Array.from(new Set((html.match(re) || []).map(x => x.replace(/\s+/g, " ").trim()))).slice(0, 25)
  const result: any = {
    cfc_refs: grab(/[A-Za-z0-9_\/.]+\.cfc[^"'\s)]*/gi),
    bind_snippets: grab(/bind[^;\n]{0,160}/gi),
    grid_init: grab(/(ColdFusion\.Grid[^;\n]{0,160}|_cf_loadgrid[^;\n]{0,160}|initgrid[^;\n]{0,160})/gi),
    colmodel: grab(/(colmodel|columns?)\s*[:=][^;\n]{0,200}/gi),
    customerlogs_calls: grab(/customerlogs?\.cfm[^"'\s)]*/gi),
    grid_names: grab(/(logsrch|loggrid|grid)[A-Za-z]*\s*[=(.][^;\n]{0,80}/gi),
  }

  // CFGRID bound to a URL fetches with format=...; try the typical cfgrid data call on the page itself
  const tryData = async (qs: string, label: string) => {
    const h = await get(`/home/customerlogs.cfm?${qs}`)
    return { label, bytes: h.length, looks_json: /^[\s]*[\[{]/.test(h) || /"TOTALROWCOUNT"|"DATA"|"COLUMNS"/i.test(h),
             EventID: (h.match(/eventid/gi)||[]).length, taskid: (h.match(/taskid/gi)||[]).length,
             head: h.slice(0, 220) }
  }
  result.data_attempts = [
    await tryData(`gridname=logsrch&page=1&pageSize=50&qdatesel=${encodeURIComponent(date_us)}&_cf_nodebug=true`, "gridname+page"),
    await tryData(`_cf_grid=logsrch&_cf_format=json&qdatesel=${encodeURIComponent(date_us)}`, "cf_grid+format"),
  ]
  return result
}

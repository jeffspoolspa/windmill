//bun-extra-requirements:
//playwright@1.40.0

// probe_daygrid_photos — DISCOVERY step 3: addLog.cfm has no photo section, so
// sweep the day grid (/home/customerLogDetails.cfm) for photo/file markup:
// openFile()/downloadFile() calls (the file_management.js entrypoints), img
// tags, camera icons, and any photo-ish attribute — with context snippets.
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
  date_us = "07/06/2026",
  officeids: string[] = ["1", "2"],
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
    Accept: "text/html, */*",
    "X-Requested-With": "XMLHttpRequest",
    Referer: `${o}/main.cfm`,
  }
  const out: any[] = []
  for (const officeid of officeids) {
    const url = `${o}/home/customerLogDetails.cfm?officeid=${officeid}&techid=0&status=0&logset=1`
      + `&dayindexsel=${encodeURIComponent(date_us)}&dayindex=&_cf_nodebug=true&_cf_nocache=true&_cf_rc=0`
    const rec: any = { officeid }
    try {
      const html = await (await fetch(url, { headers: H, redirect: "manual" })).text()
      rec.bytes = html.length
      rec.openfile_calls = [...html.matchAll(/(openFile|downloadFile)\s*\(\s*\{[^}]{0,300}/g)]
        .slice(0, 8).map((m) => m[0].replace(/\s+/g, " ").slice(0, 320))
      rec.imgs = [...new Set([...html.matchAll(/<img[^>]+src="([^"]+)"/g)].map((m) => m[1]))].slice(0, 20)
      rec.photo_lines = html.split("\n")
        .filter((l) => /photo|camera|attachment|containers|getSignedUrl|fileservice/i.test(l)
                       && !/\.css|favicon|logo/i.test(l))
        .map((l) => l.trim().slice(0, 280)).slice(0, 15)
    } catch (e: any) {
      rec.error = String(e).slice(0, 200)
    }
    out.push(rec)
  }
  return out
}

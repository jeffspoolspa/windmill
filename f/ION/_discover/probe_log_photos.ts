//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@7.0.2

// probe_log_photos — DISCOVERY: do ION service logs expose the tech-uploaded
// photos anywhere reachable from addLog.cfm (or a sibling endpoint)?
// For each {log_id, calendar_id}: fetch the raw addLog page and report every
// <img>, photo/attachment-ish link, iframe, and any script/url mentioning
// photo|image|attach|upload|file — with surrounding snippets so we can see
// the markup shape. Read-only GETs on an existing session pattern.
import "playwright@1.40.0"

import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import type { IonResource } from "/f/ION/_lib/session"
import { parse } from "node-html-parser@7.0.2"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => {
      const d = c.domain.replace(/^\./, "")
      return host === d || host.endsWith("." + d)
    })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

const HINT = /photo|image|attach|upload|file|img|gallery|picture/i

export async function main(
  logs: { log_id: string; calendar_id?: string }[] = [],
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
  for (const lg of logs) {
    const url = `${o}/tasks/addLog.cfm?calendarID=${lg.calendar_id || ""}&LogID=${lg.log_id}&source=ServiceLog`
    const rec: any = { log_id: lg.log_id, url, imgs: [], links: [], iframes: [], hints: [] }
    try {
      const html = await (await fetch(url, { headers: H, redirect: "manual" })).text()
      rec.bytes = html.length
      const r = parse(html)
      for (const img of r.querySelectorAll("img")) {
        rec.imgs.push({ src: img.getAttribute("src"), alt: img.getAttribute("alt") ?? null })
      }
      for (const a of r.querySelectorAll("a")) {
        const href = a.getAttribute("href") ?? ""
        const onclick = a.getAttribute("onclick") ?? ""
        if (HINT.test(href) || HINT.test(onclick) || HINT.test(a.text)) {
          rec.links.push({ href, onclick: onclick.slice(0, 160), text: a.text.trim().slice(0, 60) })
        }
      }
      for (const f of r.querySelectorAll("iframe")) {
        rec.iframes.push(f.getAttribute("src"))
      }
      // raw-text sweep: every line mentioning a photo-ish word, trimmed
      const lines = html.split("\n")
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i]
        if (HINT.test(line) && !/\.css|font|favicon|logo|icon|background-image/i.test(line)) {
          rec.hints.push(line.trim().slice(0, 220))
          if (rec.hints.length >= 25) break
        }
      }
    } catch (e: any) {
      rec.error = String(e).slice(0, 200)
    }
    out.push(rec)
  }
  return out
}

//bun-extra-requirements:
//playwright@1.40.0

// probe_log_images_e2e — DISCOVERY final: prove the full photo pipeline.
//  1. /mobileImage/uploadList.cfm?RefID=<LogID>&TypeID=2 -> per-LOG image list
//     (GUIDs, uploader, dates) — RefID is our ion_log_id.
//  2. S3 thumbnail t_<GUID>.jpg -> fetch WITHOUT auth (is the bucket public?)
//  3. ipc.proedgesoftware.com/v1/Containers/getSignedUrl -> does our ION
//     session grant signed full-size URLs, and does the signed URL download?
import "playwright@1.40.0"

import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import type { IonResource } from "/f/ION/_lib/session"

function cookieHeader(s: any, forHost: string) {
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return forHost === d || forHost.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

const unesc = (t: string) => t
  .replace(/&#x3a;/gi, ":").replace(/&#x2f;/gi, "/").replace(/&#x2d;/gi, "-")
  .replace(/\\x2D/gi, "-").replace(/&amp;/g, "&")

export async function main(log_id = "37312626", ion: IonResource | null = null) {
  const cred = ion ?? {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(cred)
  const o = s.ionOrigin
  const ionHost = new URL(o).hostname
  const H = {
    Cookie: cookieHeader(s, ionHost),
    "User-Agent": "Mozilla/5.0",
    Accept: "text/html, */*",
    Referer: `${o}/main.cfm`,
  }
  const rec: any = { log_id }

  // 1) per-log image list
  const listUrl = `${o}/mobileImage/uploadList.cfm?RefID=${log_id}&TypeID=2&Source=Customers&IsArchived=0`
  const html = unesc(await (await fetch(listUrl, { headers: H, redirect: "manual" })).text())
  rec.list_bytes = html.length
  rec.thumbs = [...new Set([...html.matchAll(/https:\/\/[a-z0-9.-]*s3[a-z0-9.-]*\.amazonaws\.com\/[^"'\s<>]+/gi)].map((m) => m[0]))].slice(0, 10)
  rec.openfile_params = [...html.matchAll(/openFile\(\{baseUrl:"([^"]+)",\s*path:"([^"]+)",\s*serverName:"([^"]+)"/g)]
    .slice(0, 10).map((m) => ({ baseUrl: m[1], path: m[2], serverName: m[3] }))
  rec.meta_rows = html.split("\n")
    .filter((l) => /Uploaded|\d{2}\/\d{2}\/\d{4}/.test(l) && !/<table|<style/i.test(l))
    .map((l) => l.trim().replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 120))
    .filter((l) => l.length > 5).slice(0, 12)

  // 2) thumbnail: plain GET, NO cookies
  if (rec.thumbs.length) {
    const r = await fetch(rec.thumbs[0])
    rec.thumb_fetch = { url: rec.thumbs[0].slice(-60), status: r.status,
      type: r.headers.get("content-type"), bytes: (await r.arrayBuffer()).byteLength }
  }

  // 3) signed URL for the full-size image
  const p = rec.openfile_params[0]
  if (p) {
    const su = `${p.baseUrl}/Containers/getSignedUrl?key=${p.path}/${encodeURIComponent(p.serverName)}`
      + `&server_name=${encodeURIComponent(p.serverName)}&local_name=image.jpg&redirect=false`
    // try with the proedge-domain cookies our session carries (if any)
    const peHost = new URL(p.baseUrl).hostname
    const peCookies = cookieHeader(s, peHost)
    rec.pe_cookie_count = peCookies ? peCookies.split(";").length : 0
    const r = await fetch(su, { headers: { Cookie: peCookies, "User-Agent": "Mozilla/5.0", Referer: `${o}/`, Origin: o } })
    const body = await r.text()
    rec.signed = { status: r.status, body_head: body.slice(0, 200) }
    if (r.ok && /^https?:\/\//.test(body.trim().replace(/^"|"$/g, ""))) {
      const fullUrl = body.trim().replace(/^"|"$/g, "")
      const fr = await fetch(fullUrl)
      rec.full_fetch = { status: fr.status, type: fr.headers.get("content-type"),
        bytes: (await fr.arrayBuffer()).byteLength }
    }
  }
  return rec
}

//bun-extra-requirements:
//playwright@1.40.0
//postgres@3.4.4

// SERVICE-LOG PHOTO INGESTION. For every visit (ion_log_id) in the window,
// GET /mobileImage/uploadList.cfm?RefID=<LogID>&TypeID=2 (raw HTTP on the
// cached session — no browser) and UPSERT maintenance.visit_photos on
// (ion_log_id, guid). Thumbnails are public S3 (stored as-is, hot-linkable);
// s3_key feeds ProEdge getSignedUrl for full-size on demand.
// Discovery + endpoint mechanics: docs/integrations/ion.md "Service-log photos".
// Most logs have zero photos — the fetch is still one cheap 20KB GET; re-runs
// are idempotent and pick up late uploads (same rationale as the chem re-scrape).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

const CONCURRENCY = 6

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

const unesc = (t: string) => t
  .replace(/&#x3a;/gi, ":").replace(/&#x2f;/gi, "/").replace(/&#x2d;/gi, "-")
  .replace(/\\x2D/gi, "-").replace(/&amp;/g, "&")

// One log's photo rows from the uploadList HTML. openFile() calls carry
// path + serverName (=GUID.jpg); each image block also has an
// "Uploaded MM/DD/YYYY by TECH" line, aligned by order.
function parsePhotos(html: string) {
  const t = unesc(html)
  const files = [...t.matchAll(/openFile\(\{baseUrl:"[^"]+",\s*path:"([^"]+)",\s*serverName:"([^"]+)"/g)]
    .map((m) => ({ path: m[1], serverName: m[2] }))
  const uploads = [...t.matchAll(/Uploaded\s+(\d{2}\/\d{2}\/\d{4})\s+by\s+([^<]{1,60})/g)]
    .map((m) => ({ date: m[1], by: m[2].trim() }))
  return files.map((f, i) => {
    const guid = f.serverName.replace(/\.[a-z0-9]+$/i, "")
    const [mm, dd, yy] = (uploads[i]?.date ?? "").split("/")
    return {
      guid,
      s3_key: `${f.path}/${f.serverName}`,
      thumb_url: `https://ionpoolcare.s3.us-west-2.amazonaws.com/${f.path}/t_${f.serverName}`,
      uploaded_by: uploads[i]?.by ?? null,
      uploaded_on: yy ? `${yy}-${mm}-${dd}` : null,
    }
  })
}

export async function main(start_date = "", end_date = "", dry_run = true, sess: any = null) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = sess ?? await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = {
    Cookie: cookieHeader(s),
    "User-Agent": "Mozilla/5.0",
    Accept: "text/html, */*",
    Referer: `${o}/main.cfm`,
  }

  const sb = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user,
                         password: sb.password, ssl: "require", max: 1 })
  try {
    // default window: last 3 days (matches the visit-ingest lookback)
    const start = start_date || new Date(Date.now() - 3 * 86400000).toISOString().slice(0, 10)
    const end = end_date || new Date().toISOString().slice(0, 10)
    const logs = await sql`
      SELECT DISTINCT ion_log_id, ion_cust_id
      FROM maintenance.visits
      WHERE visit_date >= ${start} AND visit_date <= ${end} AND ion_log_id IS NOT NULL`
    const out = { window: { start, end }, logs: logs.length, with_photos: 0, photos: 0, upserted: 0, errors: [] as string[] }

    for (let i = 0; i < logs.length; i += CONCURRENCY) {
      const batch = logs.slice(i, i + CONCURRENCY)
      const results = await Promise.all(batch.map(async (lg: any) => {
        try {
          const url = `${o}/mobileImage/uploadList.cfm?RefID=${lg.ion_log_id}&TypeID=2&Source=Customers&IsArchived=0`
          const html = await (await fetch(url, { headers: H, redirect: "manual" })).text()
          return { lg, photos: parsePhotos(html) }
        } catch (e: any) {
          out.errors.push(`${lg.ion_log_id}: ${String(e).slice(0, 100)}`)
          return { lg, photos: [] }
        }
      }))
      for (const r of results) {
        if (!r.photos.length) continue
        out.with_photos++
        out.photos += r.photos.length
        if (dry_run) continue
        for (const p of r.photos) {
          await sql`
            INSERT INTO maintenance.visit_photos (ion_log_id, guid, ion_cust_id, s3_key, thumb_url, uploaded_by, uploaded_on)
            VALUES (${r.lg.ion_log_id}, ${p.guid}, ${r.lg.ion_cust_id}, ${p.s3_key}, ${p.thumb_url}, ${p.uploaded_by}, ${p.uploaded_on})
            ON CONFLICT (ion_log_id, guid) DO UPDATE
              SET uploaded_by = EXCLUDED.uploaded_by, uploaded_on = EXCLUDED.uploaded_on`
          out.upserted++
        }
      }
    }
    return out
  } finally {
    await sql.end()
  }
}

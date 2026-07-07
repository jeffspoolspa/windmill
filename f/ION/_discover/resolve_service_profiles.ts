//bun-extra-requirements:
//playwright@1.40.0
//postgres@3.4.4

// resolve_service_profiles — DISCOVERY: visits.service_profile holds ION
// ProfileIDs (22 distinct). Fetch one addLog page per distinct id and scrape
// the visible "Service Profile" name so we can label them.
import "playwright@1.40.0"

import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", Referer: `${o}/main.cfm` }

  const sb = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user,
                         password: sb.password, ssl: "require", max: 1 })
  try {
    const reps = await sql`
      SELECT DISTINCT ON (service_profile) service_profile, ion_log_id, ion_calendar_id
      FROM maintenance.visits
      WHERE nullif(trim(service_profile),'') IS NOT NULL AND ion_log_id IS NOT NULL
      ORDER BY service_profile, visit_date DESC`
    const out: Record<string, string | null> = {}
    for (const r of reps) {
      const url = `${o}/tasks/addLog.cfm?calendarID=${r.ion_calendar_id}&LogID=${r.ion_log_id}&source=ServiceLog`
      const html = await (await fetch(url, { headers: H, redirect: "manual" })).text()
      // the page shows: <td class="celldata">Service Profile</td><td ...>NAME</td>
      const m = html.match(/Service Profile<\/td>\s*<td[^>]*>([\s\S]*?)<\/td>/i)
      out[r.service_profile] = m
        ? m[1].replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) || null
        : null
    }
    return out
  } finally {
    await sql.end()
  }
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4

// One-off recovery: for visits still missing tech (actual_tech_id NULL) where ion_submitted_by is
// also blank, pull the authoritative day-grid tech from list_day_logs, fill ion_submitted_by, then
// relink actual_tech_id from public.employees.ion_username aliases. Idempotent (only touches NULLs),
// self-discovers the affected dates from the DB, async-safe (run via runScriptByPath, not a sync wait).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { main as listDayLogs } from "/f/ION/api/list_day_logs"

function isoToMdy(iso: string) { const m = iso.match(/(\d{4})-(\d{2})-(\d{2})/)!; return `${m[2]}/${m[3]}/${m[1]}` }

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const res: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: res.host, port: res.port, database: res.dbname, username: res.user, password: res.password, ssl: "require", max: 4 })
  const out: any = { dates: 0, filled: 0, not_in_grid: 0, blank_in_grid: 0 }
  try {
    const rows = await sql<any[]>`SELECT to_char(scheduled_date,'YYYY-MM-DD') d, array_agg(ion_log_id) logids
      FROM maintenance.visits
      WHERE actual_tech_id IS NULL AND ion_submitted_by IS NULL AND ion_log_id IS NOT NULL
      GROUP BY scheduled_date ORDER BY scheduled_date`
    out.dates = rows.length
    for (const r of rows) {
      const enr: any = await listDayLogs(isoToMdy(r.d), 0, s)
      const techByLog: Record<string, string | null> = {}
      for (const l of (enr.logs ?? [])) techByLog[String(l.log_id)] = ((l.tech ?? "") + "").trim() || null
      const want = (r.logids as string[]).map(String)
      const upLogs: string[] = [], upTechs: string[] = []
      for (const id of want) {
        if (!(id in techByLog)) { out.not_in_grid++; continue }
        const t = techByLog[id]; if (!t) { out.blank_in_grid++; continue }
        upLogs.push(id); upTechs.push(t)
      }
      if (upLogs.length) {
        await sql`UPDATE maintenance.visits v SET ion_submitted_by=data.tech, updated_at=now()
          FROM (SELECT * FROM unnest(${upLogs}::text[], ${upTechs}::text[]) AS t(log_id,tech)) data
          WHERE v.ion_log_id=data.log_id AND v.ion_submitted_by IS NULL`
        out.filled += upLogs.length
      }
    }
    const linked = await sql`UPDATE maintenance.visits v SET actual_tech_id=e.id, updated_at=now()
      FROM public.employees e WHERE v.actual_tech_id IS NULL AND v.ion_submitted_by IS NOT NULL AND v.ion_submitted_by = ANY(e.ion_username)`
    out.newly_tech_linked = linked.count
    out.visits_still_no_tech = (await sql<any[]>`SELECT count(*)::int n FROM maintenance.visits WHERE actual_tech_id IS NULL`)[0].n
    out.unresolved_aliases = await sql<any[]>`SELECT ion_submitted_by alias, count(*)::int n FROM maintenance.visits
      WHERE actual_tech_id IS NULL AND ion_submitted_by IS NOT NULL GROUP BY 1 ORDER BY n DESC LIMIT 25`
  } finally { await sql.end() }
  return out
}

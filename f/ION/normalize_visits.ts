//bun-extra-requirements:
//postgres@3.4.4

// NORMALIZE RECONCILER — owns "raw -> canonical FK" for the visits domain. Pure DB (no ION),
// idempotent, only touches NULL FKs, so it is safe to re-run any time aliases/definitions/
// task_schedules change (append an alias -> re-run -> historical rows resolve, no re-scrape).
// Resolves: actual_tech_id (employee aliases), consumables canonical_name+base_quantity,
// visit_readings.reading_id, visit_tasks.checklist_id, and task_id/task_schedule_id/scheduled_tech_id
// from the best matching task_schedule. Returns counts + a drift snapshot (top unmapped raw names).

import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"

export async function main(sb: any = null) {
  const res: any = sb ?? await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: res.host, port: res.port, database: res.dbname, username: res.user, password: res.password, ssl: "require", max: 4 })
  const out: any = {}
  try {
    out.tech_linked = (await sql`
      UPDATE maintenance.visits v SET actual_tech_id = e.id, updated_at = now()
      FROM public.employees e
      WHERE v.actual_tech_id IS NULL AND v.ion_submitted_by IS NOT NULL AND v.ion_submitted_by = ANY(e.ion_username)`).count

    out.consumables_tied = (await sql`
      UPDATE maintenance.consumables_usage cu SET canonical_name = a.canonical_name, base_quantity = cu.quantity * a.to_base_factor
      FROM ion.consumable_aliases a
      WHERE a.ion_item_id = cu.ion_item_id AND a.canonical_name IS NOT NULL AND cu.canonical_name IS NULL`).count

    out.readings_resolved = (await sql`
      WITH rmap AS (
        SELECT DISTINCT ON (k) k, id FROM (
          SELECT lower(btrim(canonical_name)) k, id FROM ion.reading_definitions WHERE canonical_name IS NOT NULL
          UNION ALL SELECT lower(btrim(display_name)), id FROM ion.reading_definitions WHERE display_name IS NOT NULL
          UNION ALL SELECT lower(btrim(ra.raw_name)), rd.id FROM ion.reading_aliases ra JOIN ion.reading_definitions rd ON rd.canonical_name = ra.canonical_name
        ) u WHERE k <> '' ORDER BY k
      )
      UPDATE maintenance.visit_readings vr SET reading_id = rmap.id
      FROM rmap WHERE vr.reading_id IS NULL AND lower(btrim(vr.name)) = rmap.k`).count

    out.checklist_resolved = (await sql`
      WITH cmap AS (
        SELECT DISTINCT ON (k) k, id FROM (
          SELECT lower(btrim(canonical_name)) k, id FROM ion.task_definitions WHERE canonical_name IS NOT NULL
          UNION ALL SELECT lower(btrim(display_name)), id FROM ion.task_definitions WHERE display_name IS NOT NULL
          UNION ALL SELECT lower(btrim(ta.raw_name)), td.id FROM ion.task_aliases ta JOIN ion.task_definitions td ON td.canonical_name = ta.canonical_name
        ) u WHERE k <> '' ORDER BY k
      )
      UPDATE maintenance.visit_tasks vt SET checklist_id = cmap.id
      FROM cmap WHERE vt.checklist_id IS NULL AND lower(btrim(vt.task_name)) = cmap.k`).count

    out.task_linked = (await sql`
      WITH best AS (
        SELECT DISTINCT ON (ts.ion_task_id) ts.ion_task_id, ts.task_id, ts.id AS schedule_id, ts.tech_employee_id, t.service_location_id
        FROM maintenance.task_schedules ts JOIN maintenance.tasks t ON t.id = ts.task_id
        WHERE ts.ion_task_id IS NOT NULL
        ORDER BY ts.ion_task_id, ts.active DESC, ts.updated_at DESC
      )
      UPDATE maintenance.visits v SET task_id = best.task_id, task_schedule_id = best.schedule_id,
        scheduled_tech_id = COALESCE(v.scheduled_tech_id, best.tech_employee_id),
        service_location_id = COALESCE(v.service_location_id, best.service_location_id), updated_at = now()
      FROM best WHERE v.task_id IS NULL AND v.ion_task_id IS NOT NULL AND best.ion_task_id = v.ion_task_id`).count

    out.remaining = {
      visits_no_tech: (await sql`SELECT count(*)::int n FROM maintenance.visits WHERE actual_tech_id IS NULL`)[0].n,
      visits_no_task: (await sql`SELECT count(*)::int n FROM maintenance.visits WHERE task_id IS NULL`)[0].n,
      readings_unresolved: (await sql`SELECT count(*)::int n FROM maintenance.visit_readings WHERE reading_id IS NULL`)[0].n,
      checklist_unresolved: (await sql`SELECT count(*)::int n FROM maintenance.visit_tasks WHERE checklist_id IS NULL`)[0].n,
      consumables_unmapped: (await sql`SELECT count(*)::int n FROM maintenance.consumables_usage WHERE canonical_name IS NULL AND ion_item_id IS NOT NULL`)[0].n,
    }
    out.drift_readings = await sql`SELECT name, count(*)::int n FROM maintenance.visit_readings WHERE reading_id IS NULL GROUP BY 1 ORDER BY n DESC LIMIT 20`
    out.drift_checklist = await sql`SELECT task_name, count(*)::int n FROM maintenance.visit_tasks WHERE checklist_id IS NULL GROUP BY 1 ORDER BY n DESC LIMIT 20`
  } finally { await sql.end() }
  return out
}

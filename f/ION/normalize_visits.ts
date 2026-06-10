//bun-extra-requirements:
//postgres@3.4.4

// NORMALIZE RECONCILER — owns "raw -> canonical FK" for the visits domain. Pure DB (no ION),
// idempotent, only touches NULL FKs -> safe to re-run any time aliases/definitions/task_schedules
// change (append an alias -> re-run -> historical rows resolve, no re-scrape). Each child resolves
// by a SINGLE join to its alias table's definition_id (the alias table is the sole lookup):
//   visit_readings.name      -> ion.reading_aliases.definition_id   (reading_definitions.id)
//   visit_tasks.task_name    -> ion.task_aliases.definition_id      (task_definitions.id)
//   consumables_usage.ion_item_id -> ion.consumable_aliases (canonical_name + to_base_factor)
//   visits.ion_submitted_by  -> employees.ion_username  (actual_tech_id)
//   visits.ion_task_id       -> task_schedules           (task_id/schedule/scheduled_tech)
// Drift snapshot = raw values present on the children with NO alias row (the watcher's queue).

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
      UPDATE maintenance.visit_readings vr SET reading_id = ra.definition_id
      FROM ion.reading_aliases ra
      WHERE vr.reading_id IS NULL AND ra.definition_id IS NOT NULL
        AND lower(btrim(vr.name)) = lower(btrim(ra.raw_name))`).count

    out.checklist_resolved = (await sql`
      UPDATE maintenance.visit_tasks vt SET checklist_id = ta.definition_id
      FROM ion.task_aliases ta
      WHERE vt.checklist_id IS NULL AND ta.definition_id IS NOT NULL
        AND lower(btrim(vt.task_name)) = lower(btrim(ta.raw_name))`).count

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
    out.drift_readings = await sql`
      SELECT vr.name, count(*)::int n FROM maintenance.visit_readings vr
      WHERE NOT EXISTS (SELECT 1 FROM ion.reading_aliases ra WHERE lower(btrim(ra.raw_name)) = lower(btrim(vr.name)))
      GROUP BY vr.name ORDER BY n DESC LIMIT 30`
    out.drift_checklist = await sql`
      SELECT vt.task_name, count(*)::int n FROM maintenance.visit_tasks vt
      WHERE NOT EXISTS (SELECT 1 FROM ion.task_aliases ta WHERE lower(btrim(ta.raw_name)) = lower(btrim(vt.task_name)))
      GROUP BY vt.task_name ORDER BY n DESC LIMIT 30`
    out.drift_consumables = await sql`
      SELECT cu.item_name, cu.ion_item_id, count(*)::int n FROM maintenance.consumables_usage cu
      WHERE cu.ion_item_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM ion.consumable_aliases a WHERE a.ion_item_id = cu.ion_item_id AND a.definition_id IS NOT NULL)
      GROUP BY cu.item_name, cu.ion_item_id ORDER BY n DESC LIMIT 30`
  } finally { await sql.end() }
  return out
}

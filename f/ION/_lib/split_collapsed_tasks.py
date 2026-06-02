# requirements:
# psycopg2-binary

"""
f/ION/_lib/split_collapsed_tasks

One-off cleanup for the task-model refactor (per-ion-task). #58's
tasks_one_open_per_loc model collapsed multiple ION recurring tasks at one
service_location into ONE maintenance.tasks row with the extra ION tasks attached
as schedules. That breaks billing (one promise per task, max(rate)). This splits
each collapsed task into one task per ion_task_id and re-attributes its visits.

Per collapsed task T (schedules carry >1 distinct ion_task_id):
  - primary = the ion_task_id with the most linked visits -> keeps T's row
    (T.ion_task_id := primary; minimizes visit re-points).
  - each OTHER ion_task_id -> a NEW maintenance.tasks row (copied from T, its own
    ion_task_id); that ion_task's schedule rows move to the new task.
  - visits re-point to follow their schedule's (now possibly new) task_id; visits
    with no task_schedule_id stay on T (primary) -- EventID refinement is a later
    pass.

SAFETY: dry_run=True default -> all writes in one transaction, gather stats, then
ROLLBACK. Set dry_run=False to commit.
"""

from f.ION._lib.upsert import _connect


def main(supabase_connection, dry_run=True):
    conn = _connect(supabase_connection)
    stats = {"collapsed_tasks": 0, "new_tasks": 0, "schedules_moved": 0,
             "visits_repointed": 0, "visits_no_schedule_kept": 0,
             "examples": [], "dry_run": dry_run, "committed": False}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT task_id, array_agg(DISTINCT ion_task_id) AS ions
                FROM maintenance.task_schedules
                WHERE ion_task_id IS NOT NULL
                GROUP BY task_id HAVING count(DISTINCT ion_task_id) > 1
            """)
            collapsed = cur.fetchall()
            stats["collapsed_tasks"] = len(collapsed)

            for task_id, ions in collapsed:
                cur.execute("""
                    SELECT s.ion_task_id, count(v.id) AS vc
                    FROM maintenance.task_schedules s
                    LEFT JOIN maintenance.visits v ON v.task_schedule_id = s.id
                    WHERE s.task_id = %s AND s.ion_task_id IS NOT NULL
                    GROUP BY s.ion_task_id
                """, (task_id,))
                counts = {r[0]: r[1] for r in cur.fetchall()}
                primary = max(ions, key=lambda i: (counts.get(i, 0), i))

                cur.execute("UPDATE maintenance.tasks SET ion_task_id=%s, updated_at=now() WHERE id=%s",
                            (primary, task_id))
                new_for_task = 0
                for ion in ions:
                    if ion == primary:
                        continue
                    cur.execute("""
                        INSERT INTO maintenance.tasks
                          (service_location_id, chem_budget_cents, included_items, status,
                           pause_reason, starts_on, ends_on, notes, external_source, external_data, ion_task_id)
                        SELECT service_location_id, chem_budget_cents, included_items, status,
                           pause_reason, starts_on, ends_on, notes, external_source, external_data, %s
                        FROM maintenance.tasks WHERE id=%s
                        RETURNING id
                    """, (ion, task_id))
                    new_id = cur.fetchone()[0]
                    stats["new_tasks"] += 1
                    new_for_task += 1
                    cur.execute("UPDATE maintenance.task_schedules SET task_id=%s, updated_at=now() WHERE task_id=%s AND ion_task_id=%s",
                                (new_id, task_id, ion))
                    stats["schedules_moved"] += cur.rowcount
                if len(stats["examples"]) < 12:
                    stats["examples"].append({"task_id": str(task_id), "n_ions": len(ions),
                                              "primary": primary, "new_tasks": new_for_task})

            collapsed_ids = tuple(r[0] for r in collapsed)
            if collapsed_ids:
                cur.execute("""
                    UPDATE maintenance.visits v
                    SET task_id = s.task_id, updated_at=now()
                    FROM maintenance.task_schedules s
                    WHERE v.task_schedule_id = s.id
                      AND v.task_id IN %s
                      AND v.task_id <> s.task_id
                """, (collapsed_ids,))
                stats["visits_repointed"] = cur.rowcount
                cur.execute("SELECT count(*) FROM maintenance.visits WHERE task_id IN %s AND task_schedule_id IS NULL",
                            (collapsed_ids,))
                stats["visits_no_schedule_kept"] = cur.fetchone()[0]

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True
        return stats
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

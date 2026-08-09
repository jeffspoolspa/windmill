/**
 * f/ION/api/write_tasks_batch — the publish executor: a MOVE's ops (1-2
 * form POSTs) through ONE warm session, results as data, dry-run BY
 * DEFAULT. Live writes are an explicit dry_run=false (Carter arms them).
 * Mirrors get_task_forms_batch's session pattern.
 */

import "playwright@1.40.0"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { updateTask, createTask } from "/f/ION/_lib/task_detail"

type Resource = { ion: object }

export async function main(
  ion: Resource["ion"],
  ops: {
    op: "update" | "create"
    ionCustId: string
    ionTaskId?: string
    changes?: Record<string, string>
    fields?: Record<string, string>
  }[],
  dry_run = true,
) {
  const session = await getOrRefreshSession(ion)
  const results: unknown[] = []
  for (const o of ops ?? []) {
    try {
      if (o.op === "update") {
        if (!o.ionTaskId) throw new Error("update requires ionTaskId")
        results.push(await updateTask(session, o.ionTaskId, o.ionCustId, o.changes ?? {}, dry_run))
      } else {
        results.push(await createTask(session, o.ionCustId, { ...(o.fields ?? {}), ...(o.changes ?? {}) }, dry_run))
      }
    } catch (e) {
      results.push({ ok: false, committed: false, error: String(e).slice(0, 300) })
    }
  }
  return { count: results.length, dry_run, results }
}

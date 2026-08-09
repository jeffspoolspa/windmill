/**
 * f/ION/api/write_task — THE outbound write surface for ION tasks.
 *
 * One script, both ops, dry-run BY DEFAULT (a live write is an explicit
 * dry_run=false, never a default — Carter arms live writes):
 *   op "update": POST changes onto an existing task's form (amend / EndsOn)
 *   op "create": prime customer, fill the blank form, POST (new incarnation)
 *
 * The result is the ECHO the caller records — committed status, response
 * preview, and for creates the response that carries the new task id.
 * Callers never assume; they read the echo (echo over prediction, RULED).
 */

import "playwright@1.40.0"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { updateTask, createTask, deleteTask } from "/f/ION/_lib/task_detail"

type Resource = { ion: object }

export async function main(
  ion: Resource["ion"],
  op: "update" | "create" | "delete",
  ionCustId: string,
  ionTaskId: string = "",
  changes: Record<string, string> = {},
  fields: Record<string, string> = {},
  dry_run = true,
) {
  const session = await getOrRefreshSession(ion)
  if (op === "update") {
    if (!ionTaskId) throw new Error("update requires ionTaskId")
    return updateTask(session, ionTaskId, ionCustId, changes, dry_run)
  }
  if (op === "delete") {
    if (!ionTaskId) throw new Error("delete requires ionTaskId")
    return deleteTask(session, ionTaskId, ionCustId, dry_run)
  }
  return createTask(session, ionCustId, { ...fields, ...changes }, dry_run)
}

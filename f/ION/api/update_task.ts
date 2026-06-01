//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint (WRITE-BACK, ADR 002): edit one task in ION.
//
// dry_run defaults to TRUE: re-reads the task edit form, applies `changes`
// (a name->value map of form fields, e.g. {tasknote: "...", day2: "<techId>",
// ServiceRepeat: "3", EndsOn: "2026-07-01"}), and returns the EXACT POST payload
// it WOULD send -- WITHOUT submitting. Set dry_run=false to actually write.
//
// Guardrails (ADR 002): this is the single ION write path; idempotent via the
// form's Old* change-detection; the next recurring_tasks/schedule_slots sync is
// the [reflection] that pulls the change back into our cache. chromium only if
// the cached session is stale.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { updateTask } from "/f/ION/_lib/task_detail"

export async function main(
  ionTaskId: string | number,
  ionCustId: string | number,
  changes: Record<string, string> = {},
  dry_run = true,
) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  return updateTask(session, ionTaskId, ionCustId, changes, dry_run)
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint (bulk): the FULL active-recurring-tasks array.
//
// Sibling of f/ION/api/get_recurring_tasks. get_recurring_tasks returns
// {count, sample} (light, for ad-hoc/health checks). This one returns the
// complete normalized array (~487 rows, ~350 KB) for BULK consumers -- chiefly
// the recurring_tasks sync flow's upsert step. Same composition: reuse the
// cached background session (chromium only if stale) -> prime once -> fetch ->
// normalize. Change the normalizer in /f/ION/_lib/reports and this endpoint
// returns the new shape without touching any caller.
//
// chromium-tagged so it CAN log in, but launches the browser only when the
// cached session is stale; otherwise it's pure HTTP.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getRecurringTasks } from "/f/ION/_lib/reports"

export async function main(filters: Record<string, string | number> = {}) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)        // reuse cached session, or login if stale
  const tasks = await getRecurringTasks(session, filters) // prime (once) + fetch + normalize
  return tasks                                          // full array for bulk consumers
}

//bun-extra-requirements:
//playwright@1.40.0

// ION API endpoint: active recurring tasks. Single composition point: filter args ->
// get/refresh background session -> prime + fetch + normalize -> structured rows. Each
// step is a swappable imported function (change the normalizer -> same endpoint, new
// shape). chromium-tagged but only launches the browser when the cached session is stale.
// Returns count + sample; bulk consumers import getRecurringTasks from /f/ION/_lib/reports.

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
  const session = await getOrRefreshSession(ion)
  const tasks = await getRecurringTasks(session, filters)
  return { count: tasks.length, sample: tasks.slice(0, 2) }
}

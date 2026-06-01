//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint: one customer's task list (the rich schedule source).
//
// Returns every task for an ION customer with day-of-week + tech + recurrence +
// expiry, parsed from /tasks/taskList.cfm. Composition: cached session (chromium
// only if stale) -> prime customer context -> POST taskList -> normalize. Change
// the normalizer in /f/ION/_lib/customer_tasks and this endpoint returns the new
// shape. chromium-tagged so it CAN log in; browser only fires on a stale session.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getCustomerTasks } from "/f/ION/_lib/customer_tasks"

export async function main(ionCustId: string | number) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const tasks = await getCustomerTasks(session, ionCustId)
  return { ionCustId: String(ionCustId), count: tasks.length, tasks }
}

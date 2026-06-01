//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API (bulk): per-customer tasks for many customers -> flat schedule rows.
//
// Flow step 2 for the schedule-slot sync (#59). Loops the given ION customer ids
// SEQUENTIALLY (REQUIRED: customerTabs.cfm sets one server-side "current customer"
// per session, so concurrent prime->POST would interleave and return the wrong
// customer's tasks). Reuses the cached session; chromium only if stale.
//
// Returns slim rows for the Python upsert step: {ionCustId, ionTaskId, activeDays,
// recurrence, weekParity, assignedTo, expired}.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getCustomerTasks } from "/f/ION/_lib/customer_tasks"

export async function main(cust_ids: (string | number)[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const rows: any[] = []
  const errors: any[] = []
  for (const cid of cust_ids) {
    try {
      const tasks = await getCustomerTasks(session, cid)   // sequential — do not parallelize
      for (const t of tasks) {
        rows.push({
          ionCustId: String(cid), ionTaskId: t.ionTaskId, activeDays: t.activeDays,
          recurrence: t.recurrence, weekParity: t.weekParity, assignedTo: t.assignedTo, expired: t.expired,
        })
      }
    } catch (e: any) {
      errors.push({ cust_id: String(cid), error: String(e?.message ?? e).slice(0, 160) })
    }
  }
  return { customers: cust_ids.length, errors_count: errors.length, errors: errors.slice(0, 20), task_rows: rows.length, rows }
}

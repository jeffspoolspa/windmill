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
// recurrence, weekParity, assignedTo, expired, perDayTech?}.
//
// perDayTech: taskList's "Assigned To" cell CONCATENATES the assignees of a task
// whose days are covered by different techs ("MNT-RH CB, CALEB MNT-RH TC, TONY"),
// which no single ion_username can match. For those rows we fetch the task DETAIL
// form (day1..day7 = one tech select per day, via _lib/task_detail) and attach
// {dow: techName} so the upsert can resolve each day slot individually.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getCustomerTasks } from "/f/ION/_lib/customer_tasks"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// >=2 org-prefix hits = multiple assignees concatenated in one cell
function isMultiTech(assignedTo: string): boolean {
  const hits = (assignedTo || "").toUpperCase().match(/\b(MNT|OFC|EST|RTL|INV|MAINT|ADMIN)\b/g)
  return (hits?.length ?? 0) >= 2
}

export async function main(cust_ids: (string | number)[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const rows: any[] = []
  const errors: any[] = []
  let detail_fetches = 0
  for (const cid of cust_ids) {
    try {
      const tasks = await getCustomerTasks(session, cid)   // sequential — do not parallelize
      for (const t of tasks) {
        const row: any = {
          ionCustId: String(cid), ionTaskId: t.ionTaskId, activeDays: t.activeDays,
          recurrence: t.recurrence, weekParity: t.weekParity, assignedTo: t.assignedTo, expired: t.expired,
        }
        if (!t.expired && isMultiTech(t.assignedTo)) {
          try {
            const { detail } = await getTaskDetail(session, t.ionTaskId, cid)  // sequential too
            row.perDayTech = Object.fromEntries(detail.perDayTech.map((p) => [String(p.dow), p.techName]))
            detail_fetches++
          } catch { /* keep the row without perDayTech; upsert preserves existing tech */ }
        }
        rows.push(row)
      }
    } catch (e: any) {
      errors.push({ cust_id: String(cid), error: String(e?.message ?? e).slice(0, 160) })
    }
  }
  return { customers: cust_ids.length, errors_count: errors.length, errors: errors.slice(0, 20), task_rows: rows.length, detail_fetches, rows }
}

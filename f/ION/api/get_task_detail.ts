//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint: full detail for one task (the edit form).
//
// Returns the decoded task config (per-day tech day1..7, ServiceType / profile /
// ServiceRepeat / InvoiceType / InvoiceDate enums, dates, notes, flags) plus the
// dayRoster (ION employee-id -> name map from the tech dropdown). Read-only.
// Pass ionCustId to prime the customer context (recommended; the form is loaded
// from within a customer page). chromium only if the cached session is stale.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

export async function main(ionTaskId: string | number, ionCustId: string | number = "") {
  // LEARNED 2026-08-08 (Deen, task 5764017): addTask.cfm returns HTTP 500
  // when the customer context is not primed first — the form only renders
  // from within a customer page. A bare EventID fetch is NOT a valid read;
  // refusing here with the reason beats every future caller rediscovering
  // a mystery 500. (get_customer_tasks resolves the ionCustId cheaply.)
  if (!String(ionCustId).trim()) {
    throw new Error(
      "ionCustId is required: ION's addTask.cfm 500s without customer-context priming. " +
      "Resolve it via f/ION/api/get_customer_tasks or the customers mirror.")
  }
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const { detail, dayRoster } = await getTaskDetail(session, ionTaskId, ionCustId)
  return { detail, dayRoster }
}

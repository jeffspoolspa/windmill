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
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const { detail, dayRoster } = await getTaskDetail(session, ionTaskId, ionCustId)
  return { detail, dayRoster }
}

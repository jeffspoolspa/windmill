//bun-extra-requirements:
//playwright@1.40.0

// ION API endpoint: get all active recurring tasks (typed).
// Reusable data-retrieval call: wmill.run_script("f/ION/api/get_recurring_tasks")
// returns clean typed rows, hiding ION's session-priming + HTML scraping.
// Today logs in per run; NEXT = cached background session (ADR 002). Chromium-tagged
// because it may need to log in; the data path itself is pure HTTP.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { loginToIon } from "/f/ION/_lib/session"
import { primeReportsContext, fetchRecurringTasks } from "/f/ION/_lib/reports"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await loginToIon(ion)
  await primeReportsContext(session)
  const tasks = await fetchRecurringTasks(session)
  return { count: tasks.length, tasks }
}

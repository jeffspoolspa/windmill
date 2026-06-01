//bun-extra-requirements:
//playwright@1.40.0

// ION API endpoint: active recurring tasks. Returns a count + small sample so the
// inline result stays light (the full set is ~487 rows). BULK consumers (e.g. the
// task sync) should import { primeReportsContext, fetchRecurringTasks } from
// "/f/ION/_lib/reports" and process the array in-process. Today logs in per run;
// NEXT = cached background session (ADR 002). Chromium-tagged for the login step.

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
  return { count: tasks.length, sample: tasks.slice(0, 2) }
}

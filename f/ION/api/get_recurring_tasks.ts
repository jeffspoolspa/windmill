//bun-extra-requirements:
//playwright@1.40.0

// ION API endpoint: active recurring tasks. Returns count + sample (full set ~487 rows;
// bulk consumers import getRecurringTasks from /f/ION/_lib/reports and process in-process).
// chromium-tagged for the login step; the data path itself is pure HTTP.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { loginToIon } from "/f/ION/_lib/session"
import { getRecurringTasks } from "/f/ION/_lib/reports"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await loginToIon(ion)
  const tasks = await getRecurringTasks(session)
  return { count: tasks.length, sample: tasks.slice(0, 2) }
}

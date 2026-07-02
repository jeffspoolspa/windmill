//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. CURRENT: ALTMAN (task 5664059 / cust 1124217) -- our task_schedules
// carry 3 ACTIVE day rows (Mon/Tue/Thu) but she is serviced 1x/week (Tuesdays). Dump ION's live
// day1-7 roster + ServiceRepeat to see whether ION has 3 days or our sync drifted.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const { detail }: any = await getTaskDetail(s, "5664059", "1124217")
  return { serviceType: detail.serviceType?.text, serviceRepeat: detail.serviceRepeat?.text,
           perDayTech: detail.perDayTech, startsOn: detail.startsOn, endsOn: detail.endsOn }
}

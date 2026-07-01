//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: dump the LIVE ION task config for a few closed tasks to check for stale config
// (the recurring sync only re-syncs ACTIVE tasks, so a task edited after it closed keeps its
// old config in our DB).
export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const cases: [string, string, string][] = [
    ["BARTH", "5978326", "2075596"],
    ["OLSON", "5937721", "2507622"],
    ["HAYES", "5939498", "2503879"],
  ]
  const out: any = {}
  for (const [label, tid, cid] of cases) {
    const { detail } = await getTaskDetail(session, tid, cid)
    out[label] = { serviceType: detail.serviceType?.text, invoiceType: detail.invoiceType?.text, itemCost: detail.itemCost, startsOn: detail.startsOn, endsOn: detail.endsOn }
  }
  return out
}

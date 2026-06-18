//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const ids = ["5208146", "5310468", "5210240"] // top orphan tasks (expired, 2025)
  const out: any[] = []
  for (const id of ids) {
    try {
      const { detail } = await getTaskDetail(session, id) // no ionCustId -> does customerId still resolve?
      out.push({
        id,
        customerId: detail.customerId,
        serviceType: detail.serviceType?.text,
        serviceRepeat: detail.serviceRepeat?.text,
        startsOn: detail.startsOn,
        endsOn: detail.endsOn,
        perDayTech: detail.perDayTech?.filter((d: any) => d.techId),
        note: (detail.taskNote || "").slice(0, 60),
      })
    } catch (e: any) {
      out.push({ id, error: String(e?.message ?? e) })
    }
  }
  return out
}

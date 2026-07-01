//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml, parseTaskForm } from "/f/ION/_lib/task_detail"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const cases: [string, string, string][] = [
    ["FARM_flat_1190", "5722925", "2439456"],
    ["HERRIN_greenpool_85", "5921652", "2567423"],
  ]
  const out: any = {}
  for (const [label, tid, cid] of cases) {
    const html = await fetchTaskFormHtml(session, tid, cid)
    const { fields, detail } = parseTaskForm(html)
    const priceKeys = Object.keys(fields).filter((k) => /price|cost|pay|fixed|flat|amount|rate|stop|item/i.test(k))
    const picked: any = {}
    for (const k of priceKeys) picked[k] = fields[k]
    out[label] = {
      serviceType: detail.serviceType?.text, invoiceType: detail.invoiceType?.text,
      stopPayFixed: detail.stopPayFixed, itemCost: detail.itemCost, price_fields: picked,
    }
  }
  return out
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { updateTask, getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: LIVE-write WAITES (5954394 / cust 1128297) InvoiceType -> 9 (Per Visit Itemized list
// consumables), then re-read the task to confirm ION reflects the change.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const write: any = await updateTask(s, "5954394", "1128297", { InvoiceType: "9" }, false)
  const { detail }: any = await getTaskDetail(s, "5954394", "1128297")
  return { committed: write.committed, status: write.status, changed: write.changed,
    now_invoice_type: detail.invoiceType, now_service_type: detail.serviceType?.text, itemcost: detail.itemCost }
}

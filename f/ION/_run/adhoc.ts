//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { updateTask, getTaskDetail } from "/f/ION/_lib/task_detail"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: flip the 19 Do-Not-Invoice June tasks to InvoiceType 9 = "Per Visit Itemized (list
// consumables)" via the single write path (updateTask), then re-read each to verify. Skips tasks
// already at 9 (idempotent).
const TASKS: [string, string, string][] = [
  ["5920890","2524871","LOST PLANTATION"], ["5920897","2439456","The Farm"], ["5944610","2559049","PARRISH"],
  ["5947131","2545504","Vicen"], ["5956603","1842162","DANIEL"], ["5956770","1126713","MCCALL"],
  ["5968102","2266449","ROGERS"], ["5968206","2528467","THOMAS"], ["5968505","1460599","TURER"],
  ["5969577","1125987","HOOKER"], ["5970486","1128154","THOTA"], ["5971051","1126406","Revels"],
  ["5971053","1127442","REVELS"], ["5973123","1127893","SPIKES"], ["5973225","1128504","WILLIAMS"],
  ["5975292","2176524","WETHERINGTON"], ["5983191","1127893","SPIKES"], ["5983213","1126268","JUMP"],
  ["5984076","2396021","RESERVE AT DEMERE"],
]

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const results: any[] = []
  let flipped = 0, already = 0, failed = 0
  for (const [tid, cid, name] of TASKS) {
    try {
      const { detail: before }: any = await getTaskDetail(s, tid, cid)
      if (before.invoiceType?.value === "9") { already++; results.push({ tid, name, status: "already_9" }); continue }
      const w: any = await updateTask(s, tid, cid, { InvoiceType: "9" }, false)
      const { detail: after }: any = await getTaskDetail(s, tid, cid)
      const ok = w.committed && after.invoiceType?.value === "9"
      if (ok) flipped++; else failed++
      results.push({ tid, name, status: ok ? "flipped" : "VERIFY_FAILED", from: before.invoiceType?.text, now: after.invoiceType?.text })
    } catch (e: any) {
      failed++
      results.push({ tid, name, status: "ERROR", error: String(e?.message ?? e).slice(0, 120) })
    }
  }
  return { total: TASKS.length, flipped, already, failed, results }
}

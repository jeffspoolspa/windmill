//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml } from "/f/ION/_lib/task_detail"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: dump the InvoiceType select options (value->text) for WAITES task 5954394 so we can pick
// the value for "Per Visit Itemized (list consumables)".
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const html = await fetchTaskFormHtml(s, "5954394", "1128297")
  const form = parse(html)
  const sel = form.querySelector('select[name="InvoiceType"]')
  const opts = sel ? sel.querySelectorAll("option").map((o: any) => ({ value: o.getAttribute("value"), selected: o.getAttribute("selected") != null, text: (o.text || "").replace(/\s+/g, " ").trim() })) : []
  return { invoice_type_options: opts }
}

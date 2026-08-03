//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
// StartsOn writer, UI-faithful v2. Carter's browser trace showed the working
// recipe our replay lacked: the proxy set carries the FULL ColdFusion AJAX
// envelope (_cf_clientid + containerId + nodebug/nocache/rc) and fires from a
// session primed through customerTabs. Read-first (skip if already right),
// read-back after (the only proof that counts).
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession, ionFetchText, ionFetch } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml, parseTaskForm } from "/f/ION/_lib/task_detail"

const clientId = () => Array.from({length:32},()=> "0123456789ABCDEF"[Math.floor(Math.random()*16)]).join("")

export async function main(writes: { ionTaskId: string; ionCustId: string; date: string }[] = [], dry_run = true) {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const session = await getOrRefreshSession(ion)
  const cid = clientId()
  const out: Record<string, unknown>[] = []
  let rc = 1
  for (const w of writes) {
    try {
      // prime the customer context exactly as the UI does
      await ionFetchText(session, `${session.ionOrigin}/customers/customerTabs.cfm?customerid=${w.ionCustId}`)
      const { fields } = parseTaskForm(await fetchTaskFormHtml(session, w.ionTaskId, ""))
      const before = fields["StartsOn"] ?? ""
      if (before === w.date) { out.push({ id: w.ionTaskId, before, ok: true, detail: "already correct" }); continue }
      if (dry_run) { out.push({ id: w.ionTaskId, before, wanted: w.date, detail: "dry" }); continue }
      // the UI's bind: server-side date set, full CF AJAX envelope
      await ionFetchText(session,
        `${session.ionOrigin}/includes/_proxy.cfm?source=addtask&date=${encodeURIComponent(w.date)}&set=1&_cf_containerId=csttasks&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=${rc++}`)
      const payload: Record<string, string> = { ...fields, StartsOn: w.date, LinkUsed: fields["LinkUsed"] || "Save", Submit: fields["Submit"] || "Submit" }
      await ionFetch(session, `${session.ionOrigin}/tasks/addTask.cfm?EventID=${w.ionTaskId}&isIFrame=1`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "X-Requested-With": "XMLHttpRequest", Referer: `${session.ionOrigin}/main.cfm`, Origin: session.ionOrigin },
        body: new URLSearchParams(payload).toString(),
      })
      const { fields: f2 } = parseTaskForm(await fetchTaskFormHtml(session, w.ionTaskId, ""))
      out.push({ id: w.ionTaskId, before, wanted: w.date, after: f2["StartsOn"] ?? "", ok: (f2["StartsOn"] ?? "") === w.date })
    } catch (err) { out.push({ id: w.ionTaskId, ok: false, detail: String(err).slice(0,150) }) }
  }
  return { dry_run, fixed: out.filter(o => o.ok).length, total: writes.length, results: out }
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
// StartsOn writer that mimics the UI exactly: the date field carries a
// ColdFusion AJAX bind that server-side SETS the date via _proxy.cfm on
// change, BEFORE submit. A bare form POST is silently refused for backdated
// values; the proxy set is the knob the browser has and headless replay lacked.
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession, ionFetchText } from "/f/ION/_lib/session_cache"
import { ionFetch } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml, parseTaskForm } from "/f/ION/_lib/task_detail"

export async function main(writes: { ionTaskId: string; date: string }[] = [], dry_run = true) {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const session = await getOrRefreshSession(ion)
  const out: { ionTaskId: string; before: string; wanted: string; after?: string; ok?: boolean; detail?: string }[] = []
  for (const w of writes) {
    try {
      const { fields } = parseTaskForm(await fetchTaskFormHtml(session, w.ionTaskId, ""))
      const before = fields["StartsOn"] ?? ""
      if (dry_run) { out.push({ ionTaskId: w.ionTaskId, before, wanted: w.date, detail: "dry" }); continue }
      // 1) the UI's bind: server-side set of the date for this form session
      await ionFetchText(session, `${session.ionOrigin}/includes/_proxy.cfm?source=addtask&date=${encodeURIComponent(w.date)}&set=1`)
      // 2) the ordinary form POST with the new date
      const payload: Record<string, string> = { ...fields, StartsOn: w.date }
      if (!payload["LinkUsed"]) payload["LinkUsed"] = "Save"
      if (!payload["Submit"]) payload["Submit"] = "Submit"
      const res = await ionFetch(session, `${session.ionOrigin}/tasks/addTask.cfm?EventID=${w.ionTaskId}&isIFrame=1`, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "X-Requested-With": "XMLHttpRequest", Referer: `${session.ionOrigin}/main.cfm`, Origin: session.ionOrigin },
        body: new URLSearchParams(payload).toString(),
      })
      // 3) read back — the only proof that counts
      const { fields: f2 } = parseTaskForm(await fetchTaskFormHtml(session, w.ionTaskId, ""))
      const after = f2["StartsOn"] ?? ""
      out.push({ ionTaskId: w.ionTaskId, before, wanted: w.date, after, ok: after === w.date, detail: `post ${res.status}` })
    } catch (err) {
      out.push({ ionTaskId: w.ionTaskId, before: "?", wanted: w.date, ok: false, detail: String(err).slice(0, 150) })
    }
  }
  return { dry_run, results: out, fixed: out.filter(o => o.ok).length }
}

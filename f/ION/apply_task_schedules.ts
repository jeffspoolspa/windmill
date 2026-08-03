//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION WRITE-BACK, batched (ADR 002). Apply many task edits in ONE session.
//
// The sibling of read_task_days, and for the same reason: one job, one login,
// one form fetch per task. Calling f/ION/api/update_task per task meant a
// Windmill job per task, each with its own cold start and its own timeout —
// 78 jobs, 156 fetches, and 504s on the slow ones.
//
// `preserve` is the last-moment safety check, and it is deliberately NARROW:
// only the days this write is CARRYING OVER unchanged. A day the write is
// deliberately setting does not matter — we are replacing it, so whoever is on
// it now is irrelevant. Checking the whole week instead refused legitimate
// moves (an admin placeholder sitting on the very day we were replacing).
// Checked against the SAME form read the merge uses, so it costs nothing and
// there is no gap between the check and the write.
//
// rehearse=true dry-runs every write and reports; nothing is posted. That is
// how the caller gets all-or-nothing without two passes of N jobs.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
// One module only: importing BOTH /f/ION/_lib/session and _lib/session_cache
// makes the bun resolver mangle the path (session.ts_cache.ts) and the lock
// job fails to bundle. session_cache re-exports ionFetch for this reason.
import { getOrRefreshSession, ionFetch } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml, parseTaskForm } from "/f/ION/_lib/task_detail"

export interface ScheduleWrite {
  key: string
  ionTaskId: string
  ionCustId: string
  /** ION form fields to apply (day1..day7, or StartsOn/AssignedTo). */
  changes: Record<string, string>
  /** weekday -> ION employee id for days being carried over unchanged. */
  preserve?: Record<string, string>
}

export async function main(writes: ScheduleWrite[] = [], dry_run = true) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)

  const results: {
    key: string
    accepted: boolean
    detail: string
    changed?: { field: string; from: string | null; to: string }[]
    drift?: { weekday: string; ion: string | null; expected: string }[]
  }[] = []

  for (const w of writes) {
    try {
      // ONE read. It serves the preserve check and the merge both.
      const html = await fetchTaskFormHtml(session, w.ionTaskId, w.ionCustId)
      const { fields, detail } = parseTaskForm(html)

      if (w.preserve && Object.keys(w.preserve).length > 0) {
        const actual: Record<string, string> = {}
        for (const d of detail.perDayTech) actual[String(d.dow)] = d.techId
        const drift = Object.entries(w.preserve)
          .filter(([day, tech]) => (actual[day] ?? null) !== tech)
          .map(([day, tech]) => ({ weekday: day, ion: actual[day] ?? null, expected: tech }))
        if (drift.length > 0) {
          results.push({
            key: w.key,
            accepted: false,
            detail: "refused: a day this write carries over unchanged is not what ION holds",
            drift,
          })
          continue
        }
      }

      const payload: Record<string, string> = { ...fields, ...w.changes }
      if (!payload["LinkUsed"]) payload["LinkUsed"] = "Save"
      if (!payload["Submit"]) payload["Submit"] = "Submit"
      const changed = Object.keys(w.changes)
        .filter((k) => fields[k] !== w.changes[k])
        .map((k) => ({ field: k, from: fields[k] ?? null, to: w.changes[k] }))

      if (dry_run) {
        results.push({
          key: w.key,
          accepted: true,
          detail: `dry run: ${changed.length} field(s) would change`,
          changed,
        })
        continue
      }

      const res = await ionFetch(
        session,
        `${session.ionOrigin}/tasks/addTask.cfm?EventID=${w.ionTaskId}&isIFrame=1`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            Referer: `${session.ionOrigin}/main.cfm`,
            Origin: session.ionOrigin,
          },
          body: new URLSearchParams(payload).toString(),
        },
      )
      results.push({
        key: w.key,
        accepted: res.ok,
        detail: res.ok ? `wrote ${changed.length} field(s)` : `ION refused (${res.status})`,
        changed,
      })
    } catch (err) {
      results.push({
        key: w.key,
        accepted: false,
        detail: err instanceof Error ? err.message : String(err),
      })
    }
  }

  return {
    dry_run,
    total: writes.length,
    accepted: results.filter((r) => r.accepted).length,
    results,
  }
}

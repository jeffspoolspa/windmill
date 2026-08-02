//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint (WRITE-BACK, ADR 002): edit one task in ION.
//
// dry_run defaults to TRUE: re-reads the task edit form, applies `changes`
// (a name->value map of form fields, e.g. {tasknote: "...", day2: "<techId>",
// ServiceRepeat: "3", EndsOn: "2026-07-01"}), and returns the EXACT POST payload
// it WOULD send -- WITHOUT submitting. Set dry_run=false to actually write.
//
// expect_days (OPTIONAL, strongly recommended for any schedule write): the
// per-day tech map the CALLER BELIEVES ION currently holds, as
// {"<weekday 0-6>": "<ION employee id>"} -- e.g. {"2":"32419"} meaning
// "Tuesday, Elaina, and no other day". When supplied, this compares it against
// what the form actually says and REFUSES the write on any disagreement.
//
// Why: a schedule write states the COMPLETE week (a day left out is a day ION
// keeps as-is), so it is only safe when the caller's picture of the current
// week is correct. Our cache has been observed holding ghost days ION had
// already dropped -- writing from that picture would silently re-add them and
// double somebody's service. This turns a silent corruption into a loud
// refusal. Checked in the same session immediately before the POST.
//
// Guardrails (ADR 002): this is the single ION write path; idempotent via the
// form's Old* change-detection; the next recurring_tasks/schedule_slots sync is
// the [reflection] that pulls the change back into our cache. chromium only if
// the cached session is stale.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml, parseTaskForm, updateTask } from "/f/ION/_lib/task_detail"

export async function main(
  ionTaskId: string | number,
  ionCustId: string | number,
  changes: Record<string, string> = {},
  dry_run = true,
  expect_days: Record<string, string> | null = null,
) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)

  if (expect_days) {
    // What ION actually holds right now: weekday -> ION employee id.
    const { detail } = parseTaskForm(await fetchTaskFormHtml(session, ionTaskId, ionCustId))
    const actual: Record<string, string> = {}
    for (const d of detail.perDayTech) actual[String(d.dow)] = d.techId

    const drift: { weekday: string; ion: string | null; expected: string | null }[] = []
    for (const day of new Set([...Object.keys(actual), ...Object.keys(expect_days)])) {
      const a = actual[day] ?? null
      const e = expect_days[day] ?? null
      if (a !== e) drift.push({ weekday: day, ion: a, expected: e })
    }
    if (drift.length > 0) {
      return {
        dry_run,
        committed: false,
        refused: "stale_picture",
        ionTaskId: String(ionTaskId),
        detail:
          "refused: ION's current schedule is not what the caller believed, so a " +
          "complete-week write would corrupt it — reconcile the cache first",
        drift,
        ion_now: actual,
        expected: expect_days,
      }
    }
  }

  return updateTask(session, ionTaskId, ionCustId, changes, dry_run)
}

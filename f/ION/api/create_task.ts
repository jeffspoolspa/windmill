//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint (WRITE-BACK, ADR 002): create ONE recurring task.
//
// The sibling of update_task. ION uses the SAME form for both -- the only
// difference is that the create form is addressed by CustomerID with EventID
// empty, while an edit is addressed by EventID. So this re-reads the create
// form, merges `fields` over its defaults, and POSTs it back, exactly as
// update_task does.
//
// dry_run defaults to TRUE: returns the EXACT payload it WOULD post, without
// submitting. Set dry_run=false to actually create.
//
// After a live create ION does not hand back the new id, so we re-read the
// customer's task list and return the ion_task_id that was not there before.
// That is the identity the caller must record -- a created task we cannot name
// is an orphan (see recover_orphan_tasks).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
// One module only — see the resolver collision note in apply_task_schedules.
import { getOrRefreshSession, ionFetchText, ionFetch } from "/f/ION/_lib/session_cache"
import { parseTaskForm } from "/f/ION/_lib/task_detail"

/** Every EventID currently on this customer's task list. */
async function taskIdsFor(session: any, ionCustId: string | number): Promise<Set<string>> {
  const html = await ionFetchText(
    session,
    `${session.ionOrigin}/tasks/taskList.cfm?customerid=${ionCustId}`,
  )
  const ids = new Set<string>()
  for (const m of html.matchAll(/EventID=(\d+)/gi)) ids.add(m[1])
  return ids
}

export async function main(
  ionCustId: string | number,
  fields: Record<string, string> = {},
  dry_run = true,
) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)

  // Prime the customer, then read the CREATE form (CustomerID, no EventID).
  await ionFetchText(session, `${session.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`)
  const createUrl = `${session.ionOrigin}/tasks/addTask.cfm?CustomerID=${ionCustId}&isIFrame=1`
  const html = await ionFetchText(session, createUrl)
  const { fields: blank } = parseTaskForm(html)

  const payload: Record<string, string> = { ...blank, ...fields }
  payload["CustomerID"] = String(ionCustId)
  payload["EventID"] = "" // empty EventID is what makes this a create
  if (!payload["LinkUsed"]) payload["LinkUsed"] = "Save"
  if (!payload["Submit"]) payload["Submit"] = "Submit"

  const stated = Object.keys(fields)
    .filter((k) => blank[k] !== fields[k])
    .map((k) => ({ field: k, from: blank[k] ?? null, to: fields[k] }))

  if (dry_run) {
    return {
      dry_run: true,
      committed: false,
      would_post_to: `/tasks/addTask.cfm?CustomerID=${ionCustId}&isIFrame=1`,
      stated,
      field_count: Object.keys(payload).length,
      payload_preview: payload,
    }
  }

  const before = await taskIdsFor(session, ionCustId)
  const res = await ionFetch(session, createUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
      "X-Requested-With": "XMLHttpRequest",
      Referer: `${session.ionOrigin}/main.cfm`,
      Origin: session.ionOrigin,
    },
    body: new URLSearchParams(payload).toString(),
  })
  const txt = await res.text()

  // Name what we just made, or say plainly that we could not.
  const after = await taskIdsFor(session, ionCustId)
  const minted = [...after].filter((id) => !before.has(id))
  return {
    dry_run: false,
    committed: res.ok,
    status: res.status,
    ionTaskId: minted.length === 1 ? minted[0] : null,
    minted,
    ambiguous: minted.length > 1,
    stated,
    response_preview: txt.slice(0, 400),
  }
}

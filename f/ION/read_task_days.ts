//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// READ-ONLY: what days does ION actually serve these tasks on?
//
// The authority for reconciling maintenance.task_schedules. Our sync
// (upsert_schedules) reconciles forward only -- it updates or inserts the days
// ION reports but deactivates surplus slots only under full_reconcile -- so a
// day dropped in ION stays active with us indefinitely. Those ghost days are
// dangerous now that routing writes the COMPLETE week back: publishing from a
// picture containing a ghost re-adds it and doubles somebody's service.
//
// One session, many tasks. Writes nothing, anywhere.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml, parseTaskForm } from "/f/ION/_lib/task_detail"

export async function main(ionTaskIds: string[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)

  const days: Record<string, { dow: number; techId: string; techName: string }[]> = {}
  const failed: Record<string, string> = {}
  for (const id of ionTaskIds) {
    try {
      const { detail } = parseTaskForm(await fetchTaskFormHtml(session, id, ""))
      days[id] = detail.perDayTech.map((d) => ({
        dow: d.dow,
        techId: d.techId,
        techName: d.techName,
      }))
    } catch (err) {
      failed[id] = err instanceof Error ? err.message : String(err)
    }
  }
  return { read: Object.keys(days).length, failed_count: Object.keys(failed).length, days, failed }
}

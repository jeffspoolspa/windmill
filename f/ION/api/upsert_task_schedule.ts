//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail, updateTask } from "/f/ION/_lib/task_detail"

// Part A of the scheduling pipeline — task schedule UPSERT (tech reassignment + day-of-week move) via
// the ION write-back path (ADR 002, f/ION/api/update_task). Edits an EXISTING ION task's per-day tech.
//
//   day_assignments: { "<dow>": "<ION emp id>" }   dow 0=Sun .. 6=Sat (our task_schedules convention,
//                                                  == ION day<dow+1>); "" clears the day (not serviced).
//   The ION emp id is the value from the task's own day<N> <select> -- see the returned `roster`.
//
// Only days that DIFFER from ION's current form are written (idempotent; ION's Old* fields detect the
// change). dry_run (default true) returns the EXACT POST payload + the diff WITHOUT submitting; set
// dry_run=false to write. The next recurring_tasks/schedule_slots sync is the [reflection] that pulls
// the change back into maintenance.task_schedules. Scope: tech reassignment + day move only (frequency,
// add/remove days, dates are out of scope here -- they live on other fields of the same form).
export async function main(
  ionTaskId: string | number,
  ionCustId: string | number,
  day_assignments: Record<string, string> = {},
  dry_run = true,
) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const { detail, dayRoster } = await getTaskDetail(session, ionTaskId, ionCustId)

  const DOW = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
  const current: Record<string, string> = {}
  for (const p of detail.perDayTech) current[String(p.dow)] = p.techId

  const changes: Record<string, string> = {}
  const plan: any[] = []
  const invalid: any[] = []
  for (const [dowStr, empIdRaw] of Object.entries(day_assignments)) {
    const dow = Number(dowStr)
    if (!Number.isInteger(dow) || dow < 0 || dow > 6) { invalid.push({ dow: dowStr, reason: "dow must be an integer 0..6" }); continue }
    const empId = String(empIdRaw ?? "")
    if (empId && !dayRoster[empId]) { invalid.push({ dow, emp_id: empId, reason: "emp id not in this task's roster" }); continue }
    const field = `day${dow + 1}`
    const cur = current[String(dow)] ?? ""
    if (empId !== cur) {
      changes[field] = empId
      plan.push({ dow, day: DOW[dow], field, from: cur || null, from_tech: cur ? dayRoster[cur] : null, to: empId || null, to_tech: empId ? dayRoster[empId] : null })
    }
  }
  if (invalid.length) return { ok: false, error: "invalid assignments", invalid, roster: dayRoster }

  const result = await updateTask(session, ionTaskId, ionCustId, changes, dry_run)
  return {
    ok: true, ionTaskId: String(ionTaskId), ionCustId: String(ionCustId), dry_run,
    no_changes: plan.length === 0,
    current_per_day: detail.perDayTech.map((p: any) => ({ dow: p.dow, day: p.dayName, tech_id: p.techId, tech: p.techName })),
    roster: dayRoster,
    plan,
    changes_sent: changes,
    result,
  }
}

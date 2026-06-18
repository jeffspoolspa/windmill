//bun-extra-requirements:
//playwright@1.40.0
//postgres@3.4.5
import "playwright@1.40.0"
import postgres from "postgres@3.4.5"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getRecurringTasks } from "/f/ION/_lib/reports"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

const LOCK_KEY = 916273 // SHARED with recover_orphan_tasks: both prime the shared ION session
const MIN_REPORT = 200  // safety floor: the active report is ~490 rows; a smaller pull = failure

// Close "dropped" tasks — active in our DB but ABSENT from the ION "Active Only" recurring-tasks
// report. ION drops a task from that report the moment it is given an end date, so absence implies
// the task has an end date. We FETCH that end date (get_task_detail) and close by it — the source of
// truth — honoring the invariant "no visits after the end date" (if a visit falls after ION's end,
// keep ends_on at the last visit). If ION shows NO end date, the task is absent for another reason
// (report quirk / unusual task type) -> left ACTIVE. Committing, idempotent, batched, advisory-locked
// (serializes with recover_orphan_tasks; both prime the session). dry_run default (no writes).
export async function main(limit = 60, dry_run = true) {
  const cfg = (await wmill.getResource("u/carter/supabase")) as any
  const conn = { host: cfg.host, port: cfg.port, database: cfg.dbname, username: cfg.user, password: cfg.password, ssl: "require" as const, prepare: false }
  const lock = postgres({ ...conn, max: 1, idle_timeout: 30, connect_timeout: 15 })
  if (!(await lock`select pg_try_advisory_lock(${LOCK_KEY}) as ok`)[0].ok) { await lock.end(); return { skipped: "another ION-session run in progress" } }

  const sql = postgres({ ...conn, max: 3, idle_timeout: 20, connect_timeout: 15 })
  const today = new Date().toISOString().slice(0, 10)
  const stats: any = {
    dry_run, report_rows: 0, dropped_candidates: 0, checked: 0,
    closed: 0, ends_today_or_future: 0, kept_no_end_date: 0,
    visits_after_ion_end: 0, no_ion_cust: 0, errors: 0,
    closed_examples: [], kept_examples: [], error_examples: [],
  }
  try {
    const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
    const s = await getOrRefreshSession(ion)

    // 1) Live active report -> the ion_task_ids ION still considers OPEN (no end date).
    const report = await getRecurringTasks(s, {})
    const openSet = new Set((report || []).map((r: any) => String(r.ionTaskId)).filter(Boolean))
    stats.report_rows = openSet.size
    if (openSet.size < MIN_REPORT) return { ...stats, aborted: `active report too small (${openSet.size}) — refusing to reconcile on a likely-failed fetch` }

    // 2) Active ION-sourced tasks ABSENT from that report = dropped = have an end date in ION.
    const cands = await sql`
      select t.id, t.ion_task_id,
             coalesce(t.external_data->>'ion_cust_id', rt.ion_cust_id) as ion_cust_id,
             (select max(v.visit_date)::text from maintenance.visits v where v.task_id = t.id) as last_visit
      from maintenance.tasks t
      left join ion.recurring_tasks rt on rt.ion_task_id = t.ion_task_id
      where t.status in ('active','paused')
        and t.external_source in ('ion','ion_log')
        and t.ion_task_id is not null
      order by t.updated_at asc`
    const dropped = cands.filter((t: any) => !openSet.has(String(t.ion_task_id)))
    stats.dropped_candidates = dropped.length
    const batch = dropped.slice(0, limit)

    // 3) Per dropped task: ask ION for its end date, close by it (invariant-guarded).
    for (const t of batch) {
      const eid = String(t.ion_task_id)
      const custId = t.ion_cust_id ? String(t.ion_cust_id) : ""
      if (!custId) { stats.no_ion_cust++; continue }       // get_task_detail needs the customer to prime
      try {
        const { detail } = await getTaskDetail(s, eid, custId)
        stats.checked++
        const endsOn = detail.endsOn || null
        if (!endsOn) {                                       // ION has no end date -> not actually ended
          stats.kept_no_end_date++
          if (stats.kept_examples.length < 30) stats.kept_examples.push({ eid, reason: "ION shows no end date" })
          continue
        }
        const lastVisit = t.last_visit || null
        const visitsAfter = !!(lastVisit && lastVisit > endsOn)
        const writeEnds = visitsAfter ? lastVisit : endsOn  // invariant: never before the last visit
        if (visitsAfter) stats.visits_after_ion_end++
        if (writeEnds >= today) {                            // ends today/future -> still active, record the date
          stats.ends_today_or_future++
          if (!dry_run) {
            await sql`update maintenance.tasks set ends_on = ${writeEnds}::date, updated_at = now() where id = ${t.id}`
            await sql`update maintenance.task_schedules set ends_on = ${writeEnds}::date, updated_at = now() where ion_task_id = ${eid}`
          }
          if (stats.kept_examples.length < 30) stats.kept_examples.push({ eid, ends_on: writeEnds, last_visit: lastVisit, reason: "ends today/future" })
          continue
        }
        // past end date -> close
        if (!dry_run) {
          await sql`update maintenance.tasks set status = 'closed', ends_on = ${writeEnds}::date, updated_at = now() where id = ${t.id}`
          await sql`update maintenance.task_schedules set active = false, ends_on = ${writeEnds}::date, updated_at = now() where ion_task_id = ${eid}`
        }
        stats.closed++
        if (stats.closed_examples.length < 60) stats.closed_examples.push({ eid, ion_end: endsOn, ends_on: writeEnds, last_visit: lastVisit, visits_after_ion_end: visitsAfter })
      } catch (e: any) {
        stats.errors++
        if (stats.error_examples.length < 12) stats.error_examples.push({ eid, error: String(e?.message ?? e).slice(0, 160) })
      }
    }
    stats.remaining_after_batch = Math.max(0, dropped.length - batch.length)
    return stats
  } finally {
    await sql.end().catch(() => {})
    try { await lock`select pg_advisory_unlock(${LOCK_KEY})` } catch {}
    await lock.end().catch(() => {})
  }
}

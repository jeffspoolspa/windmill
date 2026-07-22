//bun-extra-requirements:
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import postgres from "postgres@3.4.4"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { main as ingestDayLogs } from "/f/ION/ingest_day_logs"
import { main as recoverOrphanTasks } from "/f/ION/recover_orphan_tasks"

const pad = (n: number) => String(n).padStart(2, "0")
const mdy = (d: Date) => `${pad(d.getUTCMonth() + 1)}/${pad(d.getUTCDate())}/${d.getUTCFullYear()}`

// The standard daily visit sync (log-detail grain; LogID is the unique key). Three steps, all proven:
//   1. ingest_day_logs(window): per day list_day_logs -> get_log_detail -> UPSERT visit on ion_log_id
//      (+ readings/checklist/consumables); every visit carries event_id + customer_id, links its task.
//   2. recover_orphan_tasks(): create tasks for EventIDs not yet in our DB + link -> self-healing.
//   3. reconcile_visit_locations(): a visit's service_location_id is NOT set by the ingester (ADR 007
//      §9 -- a task carries no location); this fills it from the customer's confirmed link-table
//      address (single confirmed -> take it; several -> fuzzy on raw_service_address).
// Supersedes the dead CompletedLogDetail flow (which produced 0 rows). See docs/flows/sync/ion-visits.md.
// dry_run default writes nothing. The schedule passes {lookback_days:3, dry_run:false}.
export async function main(lookback_days = 7, dry_run = true) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  // Fetch the DB resource BEFORE the ION login, while the job token is fresh, then pass sb + sess to
  // every sub-step. No Windmill API read happens after this point, so a slow ION scrape can never
  // outlive the token and 401 (recover_orphan_tasks used to re-fetch and die ~15 min in).
  const sb = dry_run ? null : await wmill.getResource("u/carter/supabase")
  let sess = await getOrRefreshSession(ion)

  const end = new Date()
  const start = new Date(end.getTime() - lookback_days * 86400000)
  const window = { start: mdy(start), end: mdy(end), lookback_days }

  // A 3-day window over a live route always has logs, so a whole-window empty scrape (or a thrown
  // session-expired error) means the ION session died, NOT a real zero. Force a fresh login and retry once;
  // if it is STILL empty/failing, throw -- a dead session must never be reported as a successful 0-visit run.
  let ingest: any, attempt = 0
  for (;;) {
    attempt++
    try {
      ingest = await ingestDayLogs(window.start, window.end, dry_run, sess, sb)
      if (dry_run || (ingest.logs_built ?? 0) > 0) break
    } catch (e) {
      if (attempt >= 2) throw e // already retried with a fresh session -> a real failure, surface it loudly
    }
    if (attempt >= 2) throw new Error(`ION scrape returned 0 logs for ${window.start}..${window.end} even after a fresh login -- treating as a dead-session failure, not a real zero`)
    sess = await getOrRefreshSession(ion, { forceRefresh: true }) // self-heal, then retry once
  }
  const recover = dry_run ? { skipped: "dry_run" } : await recoverOrphanTasks(250, sess, sb)

  let reconcile: any = { skipped: "dry_run" }
  if (!dry_run) {
    const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user, password: sb.password, ssl: "require", max: 1 })
    try { reconcile = (await sql`select public.reconcile_visit_locations() as r`)[0].r } finally { await sql.end() }
  }

  return { window, ingest, recover, reconcile }
}

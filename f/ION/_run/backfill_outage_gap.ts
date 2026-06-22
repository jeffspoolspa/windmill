//bun-extra-requirements:
//playwright@1.40.0
import "playwright@1.40.0"
import { main as dailyVisitIngest } from "/f/ION/daily_visit_ingest"

// One-shot async backfill of the 2026-06-19 ingest outage (ingest_day_logs read the dropped
// task_schedules.billing_method and failed every run). Runs the STANDARD daily visit sync over a
// wide window for real (idempotent on ion_log_id, so re-ingesting already-present days is a no-op).
// Confirms the fixed ingest_day_logs (visits.customer_id from the task, location left to reconcile)
// + recover_orphan_tasks. Safe to re-run or delete.
export async function main(lookback_days = 5) {
  return await dailyVisitIngest(lookback_days, false)
}

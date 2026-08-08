//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint: fetch + parse MANY task edit forms in one job — the
// refresh/backfill transport (tier 1 at population scale). Sequential on
// one warm session; customer context primed per task (addTask.cfm 500s
// without it — LEARNED 2026-08-08). Per-task failures are DATA, not job
// failures: the caller's quarantine owns them.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { getTaskDetail } from "/f/ION/_lib/task_detail"

export async function main(tasks: { ionTaskId: string; ionCustId: string }[]) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const out: unknown[] = []
  for (const t of tasks ?? []) {
    try {
      const { fields, detail, dayRoster } = await getTaskDetail(session, t.ionTaskId, t.ionCustId)
      out.push({ ionTaskId: t.ionTaskId, ok: true, fields, detail, dayRoster })
    } catch (e) {
      out.push({ ionTaskId: t.ionTaskId, ok: false, error: String(e).slice(0, 300) })
    }
  }
  return { count: out.length, results: out }
}

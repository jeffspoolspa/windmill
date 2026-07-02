//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import { main as refreshClosedTaskConfig } from "/f/ION/refresh_closed_task_config"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: APPLY the pre-billing config refresh for June (dry_run=false) -- rewrites stale financial
// terms on expired tasks to match ION.
export async function main() {
  return await refreshClosedTaskConfig("2026-06", false)
}

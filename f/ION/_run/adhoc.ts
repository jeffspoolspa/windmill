//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import { main as refreshClosedTaskConfig } from "/f/ION/refresh_closed_task_config"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// (runScriptByPath passes NO args, so hardcode params here.)
// CURRENT: dry-run the pre-billing config refresh for June 2026.
export async function main() {
  return await refreshClosedTaskConfig("2026-06", true)
}

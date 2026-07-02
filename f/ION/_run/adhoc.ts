//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import { main as transactionsReport } from "/f/ION/transactions_report"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: retry the June transactions load (ION report 500 appears transient).
export async function main() {
  let lastErr: any = null
  for (let i = 0; i < 4; i++) {
    try { return await transactionsReport("2026-06", false, true) }
    catch (e: any) { lastErr = String(e?.message ?? e); await new Promise((r) => setTimeout(r, 2500)) }
  }
  return { failed: true, error: lastErr }
}

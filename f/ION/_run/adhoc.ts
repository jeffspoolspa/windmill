//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import { main as transactionsReport } from "/f/ION/transactions_report"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: re-pull + LOAD June transactions after the 19 DNI-flip invoice rebuilds.
export async function main() {
  return await transactionsReport("2026-06", false, true)
}

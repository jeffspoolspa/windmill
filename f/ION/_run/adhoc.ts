//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
//postgres@3.4.4
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { main as transactionsReport } from "/f/ION/transactions_report"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: force a FRESH ION login (cache freshness is time-based and can be stale vs ION's server),
// re-caching it, then re-pull + load June transactions.
export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  await getOrRefreshSession(ion, { forceRefresh: true })
  return await transactionsReport("2026-06", false, true)
}

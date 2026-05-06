//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@7.0.2

// Force playwright version pin (transitive via _lib/session would otherwise
// pull latest 1.59 and break chromium-bidi resolution).
import "playwright@1.40.0"

import { loginToIon, ionFetchText, type IonResource } from "/f/ION/_lib/session"
import { parse } from "node-html-parser@7.0.2"

async function pullCompletedLogs(session: any, lookback_days: number) {
  const start = new Date(Date.now() - lookback_days * 86_400_000).toISOString().slice(0, 10)
  const pickerUrl = `${session.ionOrigin}/reports/serviceLogs.cfm?` + new URLSearchParams({
    office: "", tech: "", Start: start, end: "", set: "1",
    _cf_containerId: "rptDetail", _cf_nodebug: "true",
    _cf_nocache: "true", _cf_clientid: session.cfClientId ?? "", _cf_rc: "1",
  }).toString()
  const dataUrl = `${session.ionOrigin}/reports/_xls/CompletedLogDetail.cfm`

  const t0 = performance.now()
  const pickerHtml = await ionFetchText(session, pickerUrl)
  const t1 = performance.now()
  const dataHtml = await ionFetchText(session, dataUrl)
  const t2 = performance.now()
  const root = parse(dataHtml)
  const tables = root.querySelectorAll("table")
  let dataTable: any = null
  let maxRows = 0
  for (const t of tables) {
    const c = t.querySelectorAll("tr").length
    if (c > maxRows) { maxRows = c; dataTable = t }
  }
  const rowCount = dataTable ? dataTable.querySelectorAll("tr").length : 0
  const t3 = performance.now()

  return {
    lookback_days,
    startUsed: start,
    pickerBytes: pickerHtml.length,
    dataBytes: dataHtml.length,
    rowCount,
    timings_ms: {
      picker_fetch: Math.round(t1 - t0),
      data_fetch: Math.round(t2 - t1),
      parse: Math.round(t3 - t2),
      total: Math.round(t3 - t0),
    },
  }
}

export async function main(ion: IonResource) {
  // STAGE A: login (one-time fixed cost — chromium cold-start lives here)
  const tA0 = performance.now()
  const session = await loginToIon(ion)
  const tA1 = performance.now()
  const loginMs = Math.round(tA1 - tA0)

  // STAGE B: 7-day pull
  const seven = await pullCompletedLogs(session, 7)

  // STAGE C: 30-day pull (same session, no re-login)
  const thirty = await pullCompletedLogs(session, 30)

  // Derived insights
  const sizeRatio = thirty.dataBytes / seven.dataBytes
  const fetchRatio = thirty.timings_ms.data_fetch / Math.max(1, seven.timings_ms.data_fetch)
  const parseRatio = thirty.timings_ms.parse / Math.max(1, seven.timings_ms.parse)

  return {
    login_ms: loginMs,
    seven_day_pull: seven,
    thirty_day_pull: thirty,
    analysis: {
      "size_30d/7d": Number(sizeRatio.toFixed(2)),
      "data_fetch_30d/7d": Number(fetchRatio.toFixed(2)),
      "parse_30d/7d": Number(parseRatio.toFixed(2)),
      // If size_ratio == fetch_ratio, transfer is purely bandwidth-bound.
      // If fetch_ratio < size_ratio, fixed-overhead per request dominates.
      // If parse_ratio == size_ratio, parser is linear in input size.
      interpretation: sizeRatio.toFixed(1) + "x more data; " +
        fetchRatio.toFixed(1) + "x slower fetch; " +
        parseRatio.toFixed(1) + "x slower parse",
    },
    total_wall_time_ms: loginMs + seven.timings_ms.total + thirty.timings_ms.total,
  }
}

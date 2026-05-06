//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@7.0.2

// Force playwright to resolve at the version session.ts uses. Without an
// explicit version-pinned import in THIS file, Bun resolves the transitive
// playwright dep (via _lib/session) at the latest 1.59.x, which needs
// chromium-bidi peer deps that aren't installed -> bun build fails. The
// `@version` syntax in import strings is the only thing that pins reliably.
import "playwright@1.40.0"

import { loginToIon, ionFetchText, type IonResource } from "/f/ION/_lib/session"
import { parse } from "node-html-parser@7.0.2"

export async function main(ion: IonResource, lookback_days = 30) {
  const session = await loginToIon(ion)
  const start = new Date(Date.now() - lookback_days * 86_400_000).toISOString().slice(0, 10)

  // STEP 1: prime session with filters via picker
  const pickerUrl = `${session.ionOrigin}/reports/serviceLogs.cfm?` + new URLSearchParams({
    office: "", tech: "", Start: start, end: "", set: "1",
    _cf_containerId: "rptDetail", _cf_nodebug: "true",
    _cf_nocache: "true", _cf_clientid: session.cfClientId ?? "", _cf_rc: "1",
  }).toString()
  console.log(`STEP 1 (prime picker): ${pickerUrl}`)
  const pickerHtml = await ionFetchText(session, pickerUrl)
  console.log(`  picker OK (${pickerHtml.length} bytes)`)

  // STEP 2: bare data URL - should read filters from session state
  const dataUrl = `${session.ionOrigin}/reports/_xls/CompletedLogDetail.cfm`
  console.log(`STEP 2 (fetch data): ${dataUrl}`)
  const dataHtml = await ionFetchText(session, dataUrl)
  console.log(`  data OK (${dataHtml.length} bytes)`)

  const root = parse(dataHtml)
  const tables = root.querySelectorAll("table")
  let dataTable: any = null
  let maxRows = 0
  for (const t of tables) {
    const c = t.querySelectorAll("tr").length
    if (c > maxRows) { maxRows = c; dataTable = t }
  }
  if (!dataTable) {
    return {
      ok: false,
      reason: "no data table",
      pickerLen: pickerHtml.length,
      dataLen: dataHtml.length,
      preview: dataHtml.slice(0, 1000),
    }
  }
  const rows = dataTable.querySelectorAll("tr").map((tr: any) =>
    tr.querySelectorAll("td, th").map((c: any) => c.text.trim().replace(/\s+/g, " "))
  )
  return {
    ok: true,
    startUsed: start,
    lookbackDays: lookback_days,
    pickerLen: pickerHtml.length,
    dataLen: dataHtml.length,
    columnCount: (rows[3] ?? []).length,
    headers: rows[3] ?? [],
    metaRows: rows.slice(0, 4),
    totalDataRows: Math.max(0, rows.length - 4),
    sampleRows: rows.slice(4, 12),
    lastRows: rows.slice(-3),
  }
}

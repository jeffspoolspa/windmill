//bun-extra-requirements:
//node-html-parser@6.1.13

// The ION report choreography, extracted once: every report is a picker page
// whose real download link we follow, returning an HTML table dressed as an
// .xls. Consumers: customer_sync (first); work_orders, transactions_report,
// event_summary, consumables to migrate.
//
// This lib REFUSES with evidence instead of guessing — a drifted report shape
// throws carrying what was actually seen, so the fix is a one-line pattern or
// header change, not an afternoon of debugging a silent wrong dump.

import { parse } from "node-html-parser"
import { ionFetchText, type IonSession } from "/f/ION/_lib/ion_session"

export interface ReportRequest {
  /** e.g. "/reports/customers.cfm" */
  pickerPath: string
  /** which <a href> on the picker is the data download */
  linkPattern: RegExp
  /** optional: hrefs to skip even if they match (e.g. the picker itself) */
  excludePattern?: RegExp
  /** report filter params; the _cf_* plumbing is added automatically */
  params?: Record<string, string>
}

export async function fetchReportGrid(
  session: IonSession,
  req: ReportRequest,
): Promise<{ dataUrl: string; grid: string[][] }> {
  const params = new URLSearchParams({
    ...(req.params ?? {}),
    _cf_containerId: "rptDetail",
    _cf_nodebug: "true",
    _cf_nocache: "true",
    _cf_rc: "1",
  })
  if (session.cfClientId) params.set("_cf_clientid", session.cfClientId)

  const pickerUrl = `${session.ionOrigin}${req.pickerPath}?${params}`
  const pickerBody = await ionFetchText(session, pickerUrl)

  let dataHref: string | null = null
  for (const a of parse(pickerBody).querySelectorAll("a")) {
    const href = a.getAttribute("href") || ""
    if (!req.linkPattern.test(href)) continue
    if (req.excludePattern?.test(href)) continue
    dataHref = href
    break
  }
  if (!dataHref) {
    const links = [...pickerBody.matchAll(/href\s*=\s*["']([^"']*\.cfm[^"']*)["']/gi)]
      .map((m) => m[1])
    throw new Error(
      `no data link matching ${req.linkPattern} on ${req.pickerPath}; ` +
      `links seen: ${JSON.stringify(links.slice(0, 20))}`)
  }

  const dataUrl = dataHref.startsWith("http")
    ? dataHref
    : `${session.ionOrigin}${dataHref.startsWith("/") ? "" : "/reports/"}${dataHref}`

  const body = await ionFetchText(session, dataUrl)
  const tables = parse(body).querySelectorAll("table")
  let table = tables[0] ?? null
  for (const t of tables) {
    if (t.querySelectorAll("tr").length > (table?.querySelectorAll("tr").length ?? 0)) table = t
  }
  if (!table) {
    throw new Error(`no table in report response (${body.length} bytes): ${body.slice(0, 500)}`)
  }

  const grid = table.querySelectorAll("tr").map((tr) =>
    tr.querySelectorAll("td, th").map((td) => td.text.trim()))
  return { dataUrl, grid }
}

/**
 * ION report convention: row[0] company/title, row[3] headers, row[4:] data.
 * Data rows may carry MORE cells than the header row (ION pads a trailing
 * empty td per data row — AllCustomers: 36 header cells, 37 per data row), so
 * rows with at least the header's cell count are kept and mapped by header
 * index; only genuinely short rows (spacers, footers) are dropped. Rows whose
 * extra cells fall mid-row misalign — the caller's key-column validation
 * (e.g. "ion_cust_id must be numeric") is what catches those.
 */
export function tableFromGrid(
  grid: string[][],
  headerRow = 3,
): { headers: string[]; rows: string[][]; dropped: number } {
  if (grid.length < headerRow + 2) {
    throw new Error(`report grid too short (${grid.length} rows) — expected headers at row ${headerRow}`)
  }
  const headers = grid[headerRow]
  const all = grid.slice(headerRow + 1)
  const rows = all.filter((r) => r.length >= headers.length)
  return { headers, rows, dropped: all.length - rows.length }
}

/**
 * Strict column mapping: dbColumn -> acceptable header spellings, first match
 * wins. Any column named in `required` that fails to map throws with the full
 * header list — the boundary refuses, never guesses.
 */
export function mapColumns(
  headers: string[],
  map: [string, string[]][],
  required: string[] = [],
): { colIndex: Map<string, number>; missingDbCols: string[]; unmappedHeaders: string[] } {
  const colIndex = new Map<string, number>()
  const missingDbCols: string[] = []
  for (const [col, candidates] of map) {
    const i = headers.findIndex((h) =>
      candidates.some((c) => h.toLowerCase() === c.toLowerCase()))
    if (i >= 0) colIndex.set(col, i)
    else missingDbCols.push(col)
  }
  for (const col of required) {
    if (!colIndex.has(col)) {
      throw new Error(`required column '${col}' not found; report headers: ${JSON.stringify(headers)}`)
    }
  }
  const used = new Set(colIndex.values())
  const unmappedHeaders = headers.filter((_, i) => !used.has(i))
  return { colIndex, missingDbCols, unmappedHeaders }
}

// import-only lib; main exists so the script deploys cleanly
export async function main() {
  return "import-only: fetchReportGrid / tableFromGrid / mapColumns"
}

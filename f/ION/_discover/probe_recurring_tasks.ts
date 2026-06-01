//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe: fetch ION's "Recurring Task Detail - Active Only" report and report
// its structure, so we can map it into maintenance.tasks / task_schedules for
// the recurring task sync. Read-only.
//
// Endpoint (from /tasks/taskList.cfm "Service Events Reports" page):
//   /reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0
//   = "Customer and Task details for all active recurring tasks" (XLS export)
// Detects HTML-table-as-xls (typical for ColdFusion _xls) vs binary xlsx.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { loginToIon, ionFetch } from "/f/ION/_lib/session"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await loginToIon(ion)
  const url = `${session.ionOrigin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`

  const res = await ionFetch(session, url, { headers: { Accept: "*/*" } })
  const contentType = res.headers.get("content-type") ?? "(none)"
  const buf = Buffer.from(await res.arrayBuffer())
  const head = buf.subarray(0, 8).toString("latin1")
  const isZipXlsx = head.startsWith("PK")
  const text = buf.toString("utf8")
  const looksHtml = /<table|<html|<!doctype/i.test(text.slice(0, 2000))

  const result: any = {
    url,
    status: res.status,
    contentType,
    byteLength: buf.length,
    format: isZipXlsx ? "binary_xlsx" : looksHtml ? "html_table" : "unknown",
  }

  if (looksHtml && !isZipXlsx) {
    const root = parse(text)
    const tables = root.querySelectorAll("table")
    let best: any = null
    let bestRows = 0
    for (const t of tables) {
      const rows = t.querySelectorAll("tr").length
      if (rows > bestRows) {
        bestRows = rows
        best = t
      }
    }
    if (best) {
      const trs = best.querySelectorAll("tr")
      const cellText = (tr: any) =>
        tr.querySelectorAll("th,td").map((c: any) => c.text.replace(/\s+/g, " ").trim())
      result.tableCount = tables.length
      result.dataTableRows = trs.length
      result.headerRow = trs[0] ? cellText(trs[0]) : []
      result.columnCount = result.headerRow.length
      result.sampleRows = trs.slice(1, 6).map(cellText)
    }
  } else {
    result.preview = text.slice(0, 500)
  }

  return result
}

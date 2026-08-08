//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// TEMP PROBE v2: EventSummary extract answers plain fetch (25KB, 19 cols).
// Capture: the real header row + a sample row + the Schedule.cfm picker's
// form fields (window/office/tech params). Read-only.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession, ionFetchText } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser@6.1.13"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const out: Record<string, unknown> = {}

  // picker: what does Schedule.cfm's form post?
  try {
    const html = await ionFetchText(s, `${o}/reports/Schedule.cfm`)
    const root = parse(html)
    out["picker_fields"] = root.querySelectorAll("input, select").slice(0, 30).map((el) => ({
      tag: el.tagName, name: el.getAttribute("name"), type: el.getAttribute("type"),
      value: (el.getAttribute("value") ?? "").slice(0, 30),
    })).filter((f) => f.name)
    out["picker_form_action"] = root.querySelectorAll("form").map((f) => f.getAttribute("action"))
  } catch (e) { out["picker"] = String(e).slice(0, 150) }

  // extract: header row + one data row + row count
  const body = await ionFetchText(s, `${o}/reports/_xls/EventSummary.cfm`)
  const root = parse(body)
  const rows = root.querySelectorAll("tr")
  out["row_count"] = rows.length
  const texts = (tr: any) => tr.querySelectorAll("td, th").map((c: any) => c.text.trim().replace(/\s+/g, " "))
  const headerIdx = rows.findIndex((r) => texts(r).some((t: string) => /customer|tech|date|day/i.test(t)) && texts(r).length > 5)
  out["header_row_index"] = headerIdx
  if (headerIdx >= 0) {
    out["headers"] = texts(rows[headerIdx])
    out["sample_row_1"] = headerIdx + 1 < rows.length ? texts(rows[headerIdx + 1]) : null
    out["sample_row_2"] = headerIdx + 2 < rows.length ? texts(rows[headerIdx + 2]) : null
  } else {
    out["first_rows"] = rows.slice(0, 6).map(texts)
  }
  return out
}

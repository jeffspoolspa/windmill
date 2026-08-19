//bun-extra-requirements:
//node-html-parser@6.1.13
//postgres@3.4.5

// f/ION/customer_sync — ION's all-customers report -> ion.customers, keyed on
// ion_cust_id. Pure transport + dump: no business logic. Matching and linking
// live in the .NET application; api_notify tells it fresh data landed.
//
// The customer report picker is /reports/customers.cfm (CustomerRpt.cfm is
// only the FILTER FORM; its rptDetail div is URL-bound to customers.cfm).
// The data link is /reports/_xls/AllCustomers.cfm — active & inactive, the
// same source the June 17 load used. ~9.8k rows, 36 header columns.
//
// Two timestamps, written by what the words mean:
//   checked_at — every comparison against ION touches it, sweep or individual.
//   updated_at — only when the comparison found the data actually changed.
//   (updated_at <= checked_at by definition, not by rule.)
// Missing = a row the pull stopped touching: checked_at goes stale on its own.
// Nothing is ever deleted.
//
// ponytail: every sweep rewrites ~9.8k rows to stamp checked_at. Fine here —
// no triggers/audit/CDC on this table and it runs nightly. If any of those
// attach, or refreshes go high-frequency: compress the sweep's stamp into a
// run-log row and derive checked_at (greatest(updated_at, last_run.at)).

import * as wmill from "windmill-client"
import { loginToIon } from "/f/ION/_lib/ion_session"
import { fetchReportGrid, tableFromGrid, mapColumns } from "/f/ION/_lib/report"
import { supabaseSql } from "/f/ION/_lib/db"
import { notifyApi } from "/f/ION/_lib/notify_api"

// db column -> the report's actual header spellings (verified live 2026-08-19).
// ion_cust_id is required: a dump that can't key is not a dump.
const HEADER_MAP: [string, string[]][] = [
  ["ion_cust_id",   ["CustomerID", "Customer ID"]],
  ["full_name",     ["Full Name", "Customer"]],
  ["first_name",    ["First Name"]],
  ["last_name",     ["Last Name"]],
  ["business_name", ["Business Name", "Company"]],
  ["office",        ["Office"]],
  ["zone",          ["Zone"]],
  ["status",        ["Status"]],
  ["created_raw",   ["Created"]],
  ["source",        ["Source"]],          // ION's lead source — its data, not our marker
  ["bill_line1",    ["Bill Line 1"]],
  ["bill_line2",    ["Bill Line 2"]],
  ["bill_line3",    ["Bill Line 3"]],
  ["bill_city",     ["Bill City"]],
  ["bill_state",    ["Bill State"]],
  ["bill_postal",   ["Bill Postal"]],
  ["service_line1", ["Service Line 1"]],
  ["service_line2", ["Service Line 2"]],
  ["service_line3", ["Service Line 3"]],
  ["service_city",  ["Service City"]],
  ["service_state", ["Service State"]],
  ["service_postal",["Service Postal"]],
  ["community",     ["Community"]],
  ["map_no",        ["Map No.", "Map No", "Map #"]],
  ["home_phone",    ["Home Phone"]],
  ["mobile_phone",  ["Mobile Phone"]],
  ["fax",           ["Fax"]],
  ["email",         ["Email Address", "Email"]],
  ["site_contact",  ["Site Contact"]],
  ["contact_phone", ["Site Phone", "Contact Phone"]],
  ["technician",    ["Technician"]],
  ["route_name",    ["Route"]],
  ["customer_type", ["Customer Type"]],
]

export async function main(dry_run = true, api_notify = false) {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })

  const { dataUrl, grid } = await fetchReportGrid(session, {
    pickerPath: "/reports/customers.cfm",
    linkPattern: /_xls\/AllCustomers\.cfm/i,
    params: { office: "0", zone: "0", tech: "0", Start: "", End: "", typeID: "0", set: "1" },
  })
  console.log(`data url: ${dataUrl.slice(0, 140)}`)

  const { headers, rows, dropped } = tableFromGrid(grid)
  const { colIndex, missingDbCols, unmappedHeaders } =
    mapColumns(headers, HEADER_MAP, ["ion_cust_id"])
  console.log(`mapped ${colIndex.size} cols; db cols unmapped: ${missingDbCols.join(", ") || "none"}`)
  console.log(`report headers unmapped: ${unmappedHeaders.join(" | ") || "none"}`)
  if (dropped > 0) console.log(`dropped ${dropped} short rows (spacers/footers)`)

  const all = rows.map((r) => {
    const o: Record<string, string | null> = {}
    for (const [col, i] of colIndex) o[col] = r[i] || null
    return o
  })
  const records = all.filter((o) => o.ion_cust_id && /^\d+$/.test(o.ion_cust_id))
  const misaligned = all.length - records.length
  console.log(`${records.length} customers parsed of ${rows.length} rows` +
    (misaligned ? `; ${misaligned} rows failed key validation (misaligned cells?)` : ""))

  if (dry_run) {
    return {
      dry_run: true, parsed: records.length, misaligned,
      mapped_cols: [...colIndex.keys()], missing_db_cols: missingDbCols,
      unmapped_report_headers: unmappedHeaders, sample: records.slice(0, 3),
    }
  }

  const sql = await supabaseSql()
  const cols = [...colIndex.keys()]
  const dataCols = cols.filter((c) => c !== "ion_cust_id")
  let upserted = 0
  let changed = 0
  try {
    for (let i = 0; i < records.length; i += 500) {
      const chunk = records.slice(i, i + 500)
      const updates = dataCols.map((c) => `"${c}" = excluded."${c}"`).join(", ")
      // updated_at moves only when a data column really differs (null-safe);
      // checked_at moves every time, because a comparison happened every time.
      const differs = dataCols
        .map((c) => `ion.customers."${c}" is distinct from excluded."${c}"`)
        .join(" or ")
      const res = await sql.unsafe(
        `insert into ion.customers (${cols.map((c) => `"${c}"`).join(", ")}, checked_at, updated_at)
         select ${cols.map((c) => `r->>'${c}'`).join(", ")}, now(), now()
         from jsonb_array_elements($1::jsonb) r
         on conflict (ion_cust_id) do update
           set ${updates}, checked_at = now(),
               updated_at = case when ${differs} then now() else ion.customers.updated_at end
         returning (updated_at = now()) as touched`,
        [JSON.stringify(chunk)])
      upserted += res.count
      changed += res.filter((r: any) => r.touched).length
    }
  } finally {
    await sql.end()
  }
  console.log(`upserted ${upserted}; data actually changed on ${changed}`)

  let notified: number | null = null
  if (api_notify) {
    notified = (await notifyApi("/maintenance/routing/customers/link-run")).status
  }

  return {
    dry_run: false, parsed: records.length, misaligned,
    upserted, data_changed: changed, notified, missing_db_cols: missingDbCols,
  }
}

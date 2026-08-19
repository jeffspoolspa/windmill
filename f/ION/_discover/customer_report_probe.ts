// Probe v5: (a) every .cfm string anywhere in the picker (the rptDetail div
// is BOUND to some detail url in inline JS); (b) does maxrows/startrow lift
// customerlist's 500-row cap; (c) a raw slice around one customer row.

import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  const out: any = {}

  // (a) the picker's every .cfm mention + context around each rptDetail
  const picker = await ionFetchText(session,
    `${session.ionOrigin}/reports/CustomerRpt.cfm?_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1`)
  out.all_cfm_strings = [...new Set(
    [...picker.matchAll(/["']([^"']{0,120}\.cfm[^"']{0,120})["']/g)].map(m => m[1])
  )].slice(0, 30)
  out.rptDetail_contexts = [...picker.matchAll(/rptDetail/g)]
    .map(m => picker.slice(Math.max(0, m.index! - 150), m.index! + 200).replace(/\s+/g, " "))
    .slice(0, 8)

  // (b) cap-lifting attempts on customerlist
  const base = `${session.ionOrigin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=&reset=1`
  const count = (b: string) => new Set([...b.matchAll(/customerTabs\.cfm\?customerid=(\d+)/g)].map(m => m[1])).size
  for (const [label, extra] of [
    ["maxrows", "&maxrows=20000"],
    ["startrow", "&startrow=501"],
    ["start_limit", "&start=500&limit=500"],
  ] as [string, string][]) {
    try {
      const b = await ionFetchText(session, base + extra)
      out[`list_${label}`] = { uniqueIds: count(b), len: b.length }
    } catch (e: any) {
      out[`list_${label}`] = { error: String(e?.message ?? e).slice(0, 200) }
    }
  }

  // (c) raw slice around the first row of the plain list
  const plain = await ionFetchText(session, base)
  const i = plain.indexOf("customerTabs.cfm?customerid=")
  out.row_slice = i >= 0 ? plain.slice(Math.max(0, i - 500), i + 1500).replace(/\s+/g, " ") : null

  return out
}

// Probe v4: June asked "is customerlist the full base?" — answer it, and see
// what columns one row carries. Also: _cf_clientid is CLIENT-generated in CF's
// ajax js, so fabricate one and see if CustomerRpt's container mode wakes up.

import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  const out: any = {}

  // 1) customer list, empty search — full base?
  const list = await ionFetchText(session,
    `${session.ionOrigin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=&reset=1`)
  const ids = [...list.matchAll(/customerTabs\.cfm\?customerid=(\d+)/g)].map(m => m[1])
  const uniq = new Set(ids)
  out.list = { len: list.length, idMentions: ids.length, uniqueIds: uniq.size }
  // one full row, verbatim-ish, to see the columns
  const i = list.indexOf("customerTabs.cfm?customerid=")
  if (i >= 0) {
    const rowStart = list.lastIndexOf("<tr", i)
    const rowEnd = list.indexOf("</tr>", i)
    out.list.sample_row = list.slice(rowStart, rowEnd + 5).replace(/\s+/g, " ").slice(0, 1800)
    // header row: last <tr before the table's first data row
    const tableStart = list.lastIndexOf("<table", i)
    out.list.pre_table_slice = list.slice(tableStart, rowStart).replace(/\s+/g, " ").slice(-1200)
  }
  out.list.pagingHints = [...list.matchAll(/(startrow|page|maxrows|next|more)[^<>]{0,40}/gi)]
    .map(m => m[0].slice(0, 60)).slice(0, 10)

  // 2) CustomerRpt with a fabricated client id
  const fakeId = "AB12CD34EF56AB12CD34EF56AB12CD34"
  const url = `${session.ionOrigin}/reports/CustomerRpt.cfm?_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${fakeId}&_cf_rc=2`
  const rpt = await ionFetchText(session, url, {
    method: "POST",
    body: new URLSearchParams({ rptOffice: "0", rptZone: "0", rptTech: "0", rptTypeID: "0", rptStart: "", rptEnd: "" }),
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  })
  out.rpt_fake_clientid = {
    len: rpt.length,
    isPickerAgain: /name="rptOffice"/i.test(rpt),
    trCount: (rpt.match(/<tr[\s>]/gi) || []).length,
    head: rpt.slice(0, 400).replace(/\s+/g, " "),
  }

  return out
}

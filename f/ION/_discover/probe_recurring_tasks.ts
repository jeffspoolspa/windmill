//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): prove the ION-API dream -- chromium ONLY for login, then the
// entire prime + report fetch via RAW ionFetch (server-side HTTP, no browser nav).
// If RecurringtasksActive returns 200 + data here, the data path is pure HTTP and
// a cached session can drive a library of direct-HTTP endpoints.

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
  const session = await loginToIon(ion) // chromium login, browser then closed
  const origin = session.ionOrigin
  const cid = session.cfClientId || ""
  const today = new Date().toISOString().slice(0, 10)
  const out: any = { cfClientId: cid, cookieCount: session.cookies.length }

  // --- ALL raw HTTP from here (no browser) ---
  const r1 = await ionFetch(session, `${origin}/reports/reports.cfm?_cf_containerId=pageContent&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=1`)
  out.reports_status = r1.status
  await r1.text()

  const r2 = await ionFetch(session, `${origin}/reports/serviceEvents.cfm?office=0&tech=0&serviceType=0&Start=${today}&end=&set=1&_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=2`)
  out.serviceEvents_status = r2.status
  await r2.text()

  const r3 = await ionFetch(session, `${origin}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`)
  out.report_status = r3.status
  const body = await r3.text()
  out.report_len = body.length

  if (r3.status === 200) {
    const root = parse(body)
    const rows = root.querySelectorAll("tr")
    const hdr = rows.find((x: any) => x.text.includes("Cust ID"))
    out.dataRows = rows.length
    out.headerFound = Boolean(hdr)
    if (hdr) out.header = hdr.querySelectorAll("td,th").map((c: any) => c.text.replace(/\s+/g, " ").trim())
  } else {
    out.report_preview = body.slice(0, 300)
  }
  return out
}

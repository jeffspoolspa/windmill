//bun-extra-requirements:
//playwright@1.40.0
//node-html-parser@6.1.13

// Probe (read-only): replicate the FULL browser request chain via RAW ionFetch
// (chromium only for login). v15 showed reports.cfm auto-chains CustomerRpt.cfm +
// customers.cfm?set=1 before serviceEvents. Replay all of it raw, then fetch.
// If RecurringtasksActive -> 200, the data path is pure HTTP (the ION-API dream).

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
  const o = session.ionOrigin
  const cid = session.cfClientId || ""
  const today = new Date().toISOString().slice(0, 10)
  let rc = 1
  const cf = (cont: string) => `_cf_containerId=${cont}&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=${rc++}`
  const out: any = { cfClientId: cid, steps: [] }

  async function step(name: string, url: string) {
    const r = await ionFetch(session, url)
    const body = await r.text()
    out.steps.push({ name, status: r.status, len: body.length })
    return body
  }

  // Full chain, raw HTTP, in browser order:
  await step("reports", `${o}/reports/reports.cfm?${cf("pageContent")}`)
  await step("CustomerRpt", `${o}/reports/CustomerRpt.cfm?${cf("cf_layoutareacenterreports")}`)
  await step("customers_set", `${o}/reports/customers.cfm?office=0&zone=0&tech=0&Start=&end=&typeid=0&set=1&${cf("rptDetail")}`)
  await step("serviceEvents_set", `${o}/reports/serviceEvents.cfm?office=0&tech=0&serviceType=0&Start=${today}&end=&set=1&${cf("rptDetail")}`)
  const body = await step("report", `${o}/reports/_xls/RecurringtasksActive.cfm?techid=0&OfficeID=0&serviceType=0`)

  const last = out.steps[out.steps.length - 1]
  if (last.status === 200) {
    const root = parse(body)
    const rows = root.querySelectorAll("tr")
    const hdr = rows.find((x: any) => x.text.includes("Cust ID"))
    out.dataRows = rows.length
    out.headerFound = Boolean(hdr)
  } else {
    out.report_preview = body.slice(0, 200)
  }
  return out
}

// Probe v6: the picker's bind target is /reports/customers.cfm — hit it with
// all-filters-off in a few param spellings and see which returns the base.

import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  const out: any = {}

  const variants: [string, string][] = [
    ["typeID", "office=0&zone=0&tech=0&Start=&End=&typeID=0&set=1"],
    ["type",   "office=0&zone=0&tech=0&Start=&End=&type=0&set=1"],
    ["minimal","office=0&zone=0&tech=0&set=1"],
  ]
  for (const [label, qs] of variants) {
    try {
      const body = await ionFetchText(session, `${session.ionOrigin}/reports/customers.cfm?${qs}`)
      const trs = [...body.matchAll(/<tr[\s>][\s\S]*?<\/tr>/gi)]
      out[label] = {
        len: body.length,
        trCount: trs.length,
        firstRows: trs.slice(0, 4).map(r =>
          r[0].replace(/<[^>]+>/g, "|").replace(/\s+/g, " ").slice(0, 300)),
        custIdMentions: (body.match(/customerid=/gi) || []).length,
      }
      if (trs.length > 100) break // found it — stop probing
    } catch (e: any) {
      out[label] = { error: String(e?.message ?? e).slice(0, 250) }
    }
  }
  return out
}

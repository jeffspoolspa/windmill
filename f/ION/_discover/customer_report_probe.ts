// Read-only probe: June's discovery never recorded its findings, so ask ION
// directly. Three fetches, no writes.

import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  const out: any = { cfClientId: Boolean(session.cfClientId) }

  const cf = session.cfClientId ? `&_cf_clientid=${session.cfClientId}` : ""
  const probe = async (label: string, url: string) => {
    try {
      const body = await ionFetchText(session, url)
      out[label] = {
        len: body.length,
        trCount: (body.match(/<tr[\s>]/gi) || []).length,
        links: [...body.matchAll(/href\s*=\s*["']([^"']*\.cfm[^"']*)["']/gi)].map(m => m[1]).slice(0, 15),
        forms: [...body.matchAll(/<form[^>]*action\s*=\s*["']([^"']*)["']/gi)].map(m => m[1]).slice(0, 5),
        inputs: [...body.matchAll(/<(?:input|select)[^>]*name\s*=\s*["']([^"']+)["']/gi)].map(m => m[1]).slice(0, 25),
        head: body.slice(0, 700).replace(/\s+/g, " "),
      }
    } catch (e: any) {
      out[label] = { error: String(e?.message ?? e) }
    }
  }

  await probe("picker_bare", `${session.ionOrigin}/reports/CustomerRpt.cfm?_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1${cf}`)
  await probe("xls_direct", `${session.ionOrigin}/reports/_xls/CustomerRpt.cfm?Office=&Technician=&Route=&Status=&_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1${cf}`)
  await probe("reports_menu", `${session.ionOrigin}/reports/reports.cfm?_cf_containerId=pageContent&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1${cf}`)

  return out
}

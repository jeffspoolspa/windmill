// Read-only probe v2: the customer report is a self-POSTing form. Dump its
// controls verbatim, then submit with empty filters and measure the response.

import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  const out: any = {}

  const pickerUrl = `${session.ionOrigin}/reports/CustomerRpt.cfm?_cf_containerId=rptDetail&_cf_nodebug=true&_cf_nocache=true&_cf_rc=1`
  const picker = await ionFetchText(session, pickerUrl)

  const formMatch = picker.match(/<form[^>]*CustomerRpt[^>]*>([\s\S]*?)<\/form>/i)
  out.form_html = formMatch
    ? formMatch[0].replace(/\s+/g, " ").slice(0, 5000)
    : `NO FORM MATCHED; body head: ${picker.slice(0, 500)}`

  // collect every control name (input/select/textarea/button), with type
  const controls: any[] = []
  for (const m of (formMatch?.[0] ?? picker).matchAll(
    /<(input|select|button|textarea)([^>]*)>/gi)) {
    const attrs = m[2]
    controls.push({
      tag: m[1].toLowerCase(),
      name: attrs.match(/name\s*=\s*["']([^"']+)["']/i)?.[1] ?? null,
      type: attrs.match(/type\s*=\s*["']([^"']+)["']/i)?.[1] ?? null,
      value: attrs.match(/value\s*=\s*["']([^"']*)["']/i)?.[1] ?? null,
    })
  }
  out.controls = controls.slice(0, 30)

  // POST with every named control empty (selects default to ""), plus any
  // submit control's own name=value
  const fields: Record<string, string> = {}
  for (const c of controls) {
    if (!c.name) continue
    fields[c.name] = c.type?.toLowerCase() === "submit" ? (c.value ?? "Submit") : ""
  }
  out.posted_fields = fields

  const res = await ionFetchText(session, pickerUrl, {
    method: "POST",
    body: new URLSearchParams(fields),
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  })
  const trs = (res.match(/<tr[\s>]/gi) || []).length
  out.post_response = {
    len: res.length,
    trCount: trs,
    links: [...res.matchAll(/href\s*=\s*["']([^"']*\.cfm[^"']*)["']/gi)].map(m => m[1]).slice(0, 10),
    head: res.slice(0, 600).replace(/\s+/g, " "),
    // if it looks like a data table, show the header-ish rows
    firstRows: trs > 3
      ? [...res.matchAll(/<tr[\s>][\s\S]*?<\/tr>/gi)].slice(0, 6)
          .map(r => r[0].replace(/<[^>]+>/g, "|").replace(/\s+/g, " ").slice(0, 250))
      : [],
  }

  return out
}

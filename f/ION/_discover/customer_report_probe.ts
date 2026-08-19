// Probe v3: the '- All' options are value "0" not "" — resubmit correctly,
// surface the form's tail (submit control lives past the earlier truncation),
// and try the GET-with-params variant too.

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
  const form = picker.match(/<form[^>]*CustomerRpt[^>]*>([\s\S]*?)<\/form>/i)?.[0] ?? picker

  out.form_tail = form.replace(/\s+/g, " ").slice(4500, 9500)

  const controls: any[] = []
  for (const m of form.matchAll(/<(input|select|button)([^>]*)>/gi)) {
    controls.push({
      tag: m[1].toLowerCase(),
      name: m[2].match(/name\s*=\s*["']([^"']+)["']/i)?.[1] ?? null,
      type: m[2].match(/type\s*=\s*["']([^"']+)["']/i)?.[1] ?? null,
      value: m[2].match(/value\s*=\s*["']([^"']*)["']/i)?.[1] ?? null,
    })
  }
  out.all_controls = controls

  const fields: Record<string, string> = {
    rptOffice: "0", rptZone: "0", rptTech: "0", rptTypeID: "0",
    rptStart: "", rptEnd: "",
  }
  for (const c of controls) {
    if (c.name && (c.type?.toLowerCase() === "submit" || c.tag === "button")) {
      fields[c.name] = c.value ?? "Submit"
    }
    if (c.name && c.type?.toLowerCase() === "hidden") {
      fields[c.name] = c.value ?? ""
    }
  }
  out.posted_fields = fields

  const summarize = (body: string) => {
    const trs = (body.match(/<tr[\s>]/gi) || []).length
    return {
      len: body.length,
      trCount: trs,
      isPickerAgain: /name="rptOffice"/i.test(body),
      firstRows: [...body.matchAll(/<tr[\s>][\s\S]*?<\/tr>/gi)].slice(0, 5)
        .map(r => r[0].replace(/<[^>]+>/g, "|").replace(/\s+/g, " ").slice(0, 220)),
    }
  }

  const post = await ionFetchText(session, pickerUrl, {
    method: "POST",
    body: new URLSearchParams(fields),
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
  })
  out.post_zero = summarize(post)

  const qs = new URLSearchParams({ ...fields, _cf_containerId: "rptDetail", _cf_nodebug: "true", _cf_nocache: "true", _cf_rc: "2" })
  const get = await ionFetchText(session, `${session.ionOrigin}/reports/CustomerRpt.cfm?${qs}`)
  out.get_with_params = summarize(get)

  return out
}

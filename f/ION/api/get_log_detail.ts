//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Authoritative per-log detail from addLog.cfm. For a batch of {log_id, calendar_id}
// (from list_day_logs), reads each log's form and returns the ground-truth fields:
//   event_id        = EventID = the parent TASK (ion_task_id)
//   task_invoice_id = TaskInvoiceID = the QBO invoice DocNumber the log billed under
//   ion_customer_id, loc_id, scheduled_date, time_in/out, serviceable, invoice_type,
//   service_profile, original_failure_id, consumables {item_id: qty}.
//
// SERVICEABLE RULE (validated 2026-06-03 against the ION transactions report):
//   A visit was PERFORMED (and ION bills it) iff it has a time_in. The time_OUT may be
//   missing (tech never clocked out -- e.g. HILTON 05/11) or reversed/garbled (AM/PM
//   typo -- MASSEY 05/18 in 14:52/out 11:11) yet still be a real billed visit. So:
//     serviceable = has time_in AND NOT (time_out present AND time_out == time_in)
//   Only an explicit ZERO-duration log (in == out) is a genuine skip/no-access.
//   (no time_in -> not performed -> serviceable false; the ingestion also skips it.)

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"

function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}
function toMin(t: string | null): number | null {
  const m = String(t || "").match(/(\d+):(\d+)\s*(AM|PM)/i)
  if (!m) return null
  let h = (+m[1]) % 12; if (/pm/i.test(m[3])) h += 12
  return h * 60 + (+m[2])
}

export async function main(logs: { log_id: string; calendar_id?: string }[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*", "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` }

  const out: any[] = []
  for (const lg of logs) {
    const rec: any = { log_id: lg.log_id, calendar_id: lg.calendar_id ?? null }
    try {
      const html = await (await fetch(`${o}/tasks/addLog.cfm?calendarID=${lg.calendar_id || ""}&LogID=${lg.log_id}&source=ServiceLog`, { headers: H, redirect: "manual" })).text()
      const r = parse(html)
      const v = (n: string) => r.querySelector(`input[name="${n}"]`)?.getAttribute("value") ?? null
      const tin = v("timeinvalue"), tout = v("timeoutvalue")
      const mi = toMin(tin), mo = toMin(tout)
      rec.event_id = v("EventID")
      rec.task_invoice_id = v("TaskInvoiceID")
      rec.consumable_invoice_id = v("ConsumableInvoiceID")
      rec.ion_customer_id = v("CustomerID")
      rec.loc_id = v("LocID")
      rec.scheduled_date = v("ScheduledDate") || v("LogDate")
      rec.time_in = tin; rec.time_out = tout
      // performed iff time_in present; non-serviceable ONLY when an explicit zero-duration (in==out)
      rec.serviceable = (mi == null) ? false : !(mo != null && mo === mi)
      rec.invoice_type = v("InvoiceType")
      rec.service_profile = v("ServiceProfile")
      rec.original_failure_id = v("OriginalFailureID") || null
      const cons: Record<string, number> = {}
      for (const inp of r.querySelectorAll('input[name^="item"]')) {
        const nm = inp.getAttribute("name") || ""
        const m = nm.match(/^item(\d+)$/); if (!m) continue
        const q = parseFloat(inp.getAttribute("value") || "")
        if (!isNaN(q) && q > 0) cons[m[1]] = (cons[m[1]] || 0) + q
      }
      rec.consumables = cons
      if (!rec.event_id) rec.error = "no EventID (not a service log?)"
    } catch (e: any) {
      rec.error = String(e?.message ?? e).slice(0, 140)
    }
    out.push(rec)
  }
  return { count: out.length, with_event: out.filter(d => d.event_id).length, performed: out.filter(d => d.time_in).length, details: out }
}

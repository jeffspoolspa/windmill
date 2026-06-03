//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Authoritative per-log detail from addLog.cfm. For a batch of {log_id, calendar_id}
// (from list_day_logs), reads each log's form and returns the ground-truth fields:
//   event_id        = EventID = the parent TASK (ion_task_id)
//   task_invoice_id = TaskInvoiceID = the QBO invoice DocNumber the log billed under
//                     -> the AUTHORITATIVE qbo customer (no address/dup inference)
//   ion_customer_id, loc_id, scheduled_date, time_in/out, serviceable (out>in),
//   invoice_type, service_profile, original_failure_id, consumables {item_id: qty}.
// Sequential (one GET per log; fast). Returns nulls + an error per log on failure.

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
      rec.serviceable = (mi != null && mo != null) ? (mo > mi) : null
      rec.invoice_type = v("InvoiceType")
      rec.service_profile = v("ServiceProfile")
      rec.original_failure_id = v("OriginalFailureID") || null
      // consumables: item{qbo_item_id}=qty inputs with a non-empty numeric value
      const cons: Record<string, number> = {}
      for (const inp of r.querySelectorAll('input[name^="item"]')) {
        const nm = inp.getAttribute("name") || ""
        const m = nm.match(/^item(\d+)$/); if (!m) continue
        const q = parseFloat(inp.getAttribute("value") || "")
        if (!isNaN(q) && q > 0) cons[m[1]] = (cons[m[1]] || 0) + q
      }
      rec.consumables = cons
      if (!rec.event_id) rec.error = "no EventID (not a completed log?)"
    } catch (e: any) {
      rec.error = String(e?.message ?? e).slice(0, 140)
    }
    out.push(rec)
  }
  return { count: out.length, with_event: out.filter(d => d.event_id).length, details: out }
}

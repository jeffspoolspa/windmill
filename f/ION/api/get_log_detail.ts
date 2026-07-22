//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// Authoritative per-log detail from addLog.cfm. For a batch of {log_id, calendar_id}
// (from list_day_logs), reads each log's form and returns the ground-truth fields.
//   GENERAL  : event_id(=task), task_invoice_id, consumable_invoice_id, ion_customer_id,
//              loc_id, scheduled_date, time_in/out, serviceable, invoice_type, service_profile,
//              original_failure_id, submitted_by(=tech), comment(=notes), failure_reason
//   READINGS : [{name,value}] from field<n> SELECT or TEXT controls (anything not yes/no), label-keyed
//   CHECKLIST: [{name,completed}] from field<n> Yes/blank RADIO groups
//   CONSUMABLES: [{ion_item_id,name,quantity}] from item<n> qty>0; name read off the row.
// Pass an existing `sess` to REUSE it and skip the per-call f/ION variable reads (which
// degrade ~15 min into a long job). Classify by control: radio=checklist, select/text=reading.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession, ionFetchText, IonSessionExpiredError } from "/f/ION/_lib/session_cache"

function toMin(t: string | null): number | null {
  const m = String(t || "").match(/(\d+):(\d+)\s*(AM|PM)/i)
  if (!m) return null
  let h = (+m[1]) % 12; if (/pm/i.test(m[3])) h += 12
  return h * 60 + (+m[2])
}
function rowLabel(el: any): string | null {
  let tr: any = el
  for (let k = 0; k < 8 && tr && tr.tagName !== "TR"; k++) tr = tr.parentNode
  const cell = tr?.querySelector("td,th")
  return cell ? cell.text.replace(/\s+/g, " ").trim() : null
}
const EMPTY = new Set(["", "-", "--"])

export async function main(logs: { log_id: string; calendar_id?: string }[] = [], sess: any = null) {
  let s = sess
  if (!s) {
    const ion = {
      loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
      username: await wmill.getVariable("f/ION/USERNAME"),
      password: await wmill.getVariable("f/ION/PASSWORD"),
    }
    s = await getOrRefreshSession(ion)
  }
  const o = s.ionOrigin

  const out: any[] = []
  for (const lg of logs) {
    const rec: any = { log_id: lg.log_id, calendar_id: lg.calendar_id ?? null }
    try {
      // ionFetchText adds the session cookie, throws IonSessionExpiredError on a login redirect, and
      // bounds each request. A dead session must NOT be buried as a per-log error (that becomes a silent
      // 0-visit run) -- it is rethrown below so the caller can self-heal.
      const html = await ionFetchText(s, `${o}/tasks/addLog.cfm?calendarID=${lg.calendar_id || ""}&LogID=${lg.log_id}&source=ServiceLog`, {
        headers: { "X-Requested-With": "XMLHttpRequest", Referer: `${o}/main.cfm` },
        signal: AbortSignal.timeout(20000),
      })
      const r = parse(html)
      const v = (n: string) => r.querySelector(`input[name="${n}"]`)?.getAttribute("value") ?? null
      const selText = (n: string) => r.querySelector(`select[name="${n}"] option[selected]`)?.text?.trim() ?? null
      const tin = v("timeinvalue"), tout = v("timeoutvalue")
      const mi = toMin(tin), mo = toMin(tout)
      rec.event_id = v("EventID")
      rec.task_invoice_id = v("TaskInvoiceID")
      rec.consumable_invoice_id = v("ConsumableInvoiceID")
      rec.ion_customer_id = v("CustomerID")
      rec.loc_id = v("LocID")
      rec.scheduled_date = v("ScheduledDate") || v("LogDate")
      rec.time_in = tin; rec.time_out = tout
      rec.serviceable = (mi == null) ? false : !(mo != null && mo === mi)
      rec.invoice_type = v("InvoiceType")
      rec.service_profile = v("ServiceProfile")
      rec.original_failure_id = v("OriginalFailureID") || null
      rec.submitted_by = selText("submittedBy")
      rec.failure_reason = selText("failureid")
      rec.comment = r.querySelector('textarea[name="comment"]')?.text.replace(/\s+/g, " ").trim() || null
      const cons: { ion_item_id: string; name: string | null; quantity: number }[] = []
      for (const inp of r.querySelectorAll('input[name^="item"]')) {
        const nm = inp.getAttribute("name") || ""
        const m = nm.match(/^item(\d+)$/); if (!m) continue
        const q = parseFloat(inp.getAttribute("value") || ""); if (isNaN(q) || q <= 0) continue
        let tr: any = inp; for (let k = 0; k < 8 && tr && tr.tagName !== "TR"; k++) tr = tr.parentNode
        const cell = tr?.querySelector("td")
        const name = cell?.querySelector("strong")?.text?.replace(/\s+/g, " ").trim() || null
        cons.push({ ion_item_id: m[1], name, quantity: q })
      }
      rec.consumables = cons
      const readings: { name: string; value: string }[] = []
      for (const sel of r.querySelectorAll("select")) {
        const nm = sel.getAttribute("name") || ""; if (!/^field\d+$/.test(nm)) continue
        const label = rowLabel(sel); if (!label) continue
        const value = (sel.querySelector("option[selected]")?.text || "").trim()
        if (!EMPTY.has(value)) readings.push({ name: label, value })
      }
      for (const inp of r.querySelectorAll('input[type="text"]')) {
        const nm = inp.getAttribute("name") || ""; if (!/^field\d+$/.test(nm)) continue
        const label = rowLabel(inp); if (!label) continue
        const value = (inp.getAttribute("value") || "").trim()
        if (!EMPTY.has(value)) readings.push({ name: label, value })
      }
      rec.readings = readings
      const checklist: { name: string; completed: boolean }[] = []
      const seenChk = new Set<string>()
      for (const inp of r.querySelectorAll('input[type="radio"]')) {
        const nm = inp.getAttribute("name") || ""
        if (!/^field\d+$/.test(nm) || seenChk.has(nm)) continue
        seenChk.add(nm)
        const label = rowLabel(inp); if (!label) continue
        let done = false
        for (const g of r.querySelectorAll(`input[name="${nm}"]`))
          if ((g.getAttribute("checked") != null || /checked/i.test(g.toString())) && g.getAttribute("value") === "Yes") done = true
        checklist.push({ name: label, completed: done })
      }
      rec.task_checklist = checklist
      if (!rec.event_id) rec.error = "no EventID (not a service log?)"
    } catch (e: any) {
      if (e instanceof IonSessionExpiredError) throw e // dead session -> fail loud, don't bury it as a per-log error
      rec.error = String(e?.message ?? e).slice(0, 140)
    }
    out.push(rec)
  }
  return {
    count: out.length,
    with_event: out.filter(d => d.event_id).length,
    performed: out.filter(d => d.time_in).length,
    with_readings: out.filter(d => d.readings?.length).length,
    with_checklist: out.filter(d => d.task_checklist?.length).length,
    with_consumables: out.filter(d => d.consumables?.length).length,
    details: out,
  }
}

//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// ION API endpoint: the Technician Event Summary over a date window — the
// ScheduleSweep's transport (tier 0 of the intake: detection only, never
// convergence). One request ≈ the whole book's scheduled events.
//
// Mechanics (probed 2026-08-08): GET /reports/Schedule.cfm with
// rptStart/rptEnd primes the server-side report state; GET
// /reports/_xls/EventSummary.cfm streams the extract as an HTML table —
// plain session fetch, no browser. Columns (19, verbatim): Office,
// Technician, Date, Seq., Customer, Address, City, State, Postal, Service
// Description, Customer Type, Price, Invoice Type, Community, Comm. Code,
// Lock/Combo, Facility Description, Volume, Tech Pay.
//
// NOTE: rows carry NO ion task id — correlation is customer-grained.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession, ionFetch, ionFetchText } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser@6.1.13"

export async function main(start: string = "", end: string = "") {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const rptStart = start || new Date().toISOString().slice(0, 10)
  const rptEnd = end || new Date(Date.now() + 28 * 86_400_000).toISOString().slice(0, 10)

  // prime the report window (server-side state) — the picker FORM POSTS to
  // itself (action="/reports/Schedule.cfm"); a GET with query params does
  // not take (probed: extract stayed on the default today-window)
  const primeBody = new URLSearchParams({
    rptOffice: "", rptTech: "", rptServiceType: "", rptStart, rptEnd, set: "1",
  }).toString()
  const primed = await ionFetch(s, `${o}/reports/Schedule.cfm`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: primeBody,
  })
  if (!primed.ok) throw new Error(`Schedule.cfm prime -> HTTP ${primed.status}`)
  const body = await ionFetchText(s, `${o}/reports/_xls/EventSummary.cfm`)

  const root = parse(body)
  const rows = root.querySelectorAll("tr")
  const texts = (tr: any): string[] => tr.querySelectorAll("td, th").map((c: any) => c.text.trim().replace(/\s+/g, " "))
  const headerIdx = rows.findIndex((r) => {
    const t = texts(r)
    return t.length > 10 && t.includes("Technician") && t.includes("Date")
  })
  if (headerIdx < 0) throw new Error(`EventSummary: header row not found (${rows.length} rows) — form shape changed?`)
  const headers = texts(rows[headerIdx])
  const col = (name: string) => headers.indexOf(name)
  const [iOff, iTech, iDate, iCust, iAddr, iSvc, iPrice, iInv] =
    ["Office", "Technician", "Date", "Customer", "Address", "Service Description", "Price", "Invoice Type"].map(col)
  if ([iOff, iTech, iDate, iCust, iAddr, iSvc, iPrice, iInv].some((i) => i < 0)) {
    throw new Error(`EventSummary: expected columns missing from [${headers.join("; ")}] — form shape changed`)
  }

  const events: Record<string, string>[] = []
  for (const tr of rows.slice(headerIdx + 1)) {
    const t = texts(tr)
    if (t.length < headers.length || !t[iDate] || !/^\d{2}\/\d{2}\/\d{4}$/.test(t[iDate])) continue
    const [mm, dd, yyyy] = t[iDate].split("/")
    events.push({
      office: t[iOff], techName: t[iTech], date: `${yyyy}-${mm}-${dd}`,
      customer: t[iCust], address: t[iAddr], serviceDescription: t[iSvc],
      price: t[iPrice], invoiceType: t[iInv],
    })
  }
  return { window: { start: rptStart, end: rptEnd }, headers, eventCount: events.length, events }
}

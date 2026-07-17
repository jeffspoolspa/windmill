// requirements:
// windmill-client
// f/ION/route_sequence_push — TEMP one-time route re-sequencing tool. Reuses the
// cached ION session (f/ION/session_cache) over pure HTTP. Two modes:
//   dryrun (default): prime + GET each customer's addRoute.cfm form, parse every
//     route slot (label, RouteID, route name, sequence), report. NO WRITES.
//   live: same read, then find the slot whose RouteID == target_route_id, patch
//     ONLY its sequence field to new_seq, POST the full form back, re-GET to verify.
// Delete this script when the reroute is done.
import * as wmill from "windmill-client"

const CACHE = "f/ION/session_cache"

function cookieHeader(s: any): string {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

async function ionGet(s: any, url: string): Promise<{ status: number; body: string }> {
  const resp = await fetch(url, {
    headers: {
      Cookie: cookieHeader(s),
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      Accept: "text/html, */*",
      Referer: `${s.ionOrigin}/main.cfm`,
    },
    redirect: "manual",
  })
  return { status: resp.status, body: await resp.text() }
}

async function ionPost(s: any, url: string, form: Record<string, string>): Promise<{ status: number; body: string }> {
  const body = Object.entries(form)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&")
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Cookie: cookieHeader(s),
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      Accept: "text/html, */*",
      Referer: url,
      "Content-Type": "application/x-www-form-urlencoded",
      Origin: s.ionOrigin,
    },
    redirect: "manual",
    body,
  })
  return { status: resp.status, body: await resp.text() }
}

interface Slot { rowLabel: string; routeField: string; routeId: string; routeName: string; seqField: string; seqValue: string }

// Parse the addcustroute form: every field (for POST replay) + structured slots.
function parseForm(html: string): { fields: Record<string, string>; slots: Slot[] } {
  const fields: Record<string, string> = {}
  const slots: Slot[] = []
  const fstart = html.search(/<form[^>]*addcustroute/i)
  const fend = html.toLowerCase().indexOf("</form>", fstart)
  const form = html.slice(fstart < 0 ? 0 : fstart, fend < 0 ? html.length : fend)

  // inputs (text/hidden): name -> value
  for (const m of form.matchAll(/<input\b[^>]*>/gi)) {
    const tag = m[0]
    const name = (/\bname\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1]
    if (!name) continue
    const type = ((/\btype\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1] || "text").toLowerCase()
    const value = (/\bvalue\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1] ?? ""
    if (type === "checkbox" || type === "radio") {
      if (/\bchecked\b/i.test(tag)) fields[name] = value
    } else if (type !== "button") {
      fields[name] = value // includes submit; harmless to always send
    }
  }
  // selects: selected option value + label
  const selMeta: Record<string, { value: string; label: string }> = {}
  for (const m of form.matchAll(/<select\b[^>]*>/gi)) {
    const tag = m[0]
    const name = (/\bname\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1]
    if (!name) continue
    const close = form.toLowerCase().indexOf("</select>", m.index!)
    const inner = form.slice(m.index!, close < 0 ? m.index! + 5000 : close)
    let value = "", label = ""
    const sel = /<option\b([^>]*\bselected\b[^>]*)>([^<]*)</i.exec(inner)
    if (sel) {
      value = (/\bvalue\s*=\s*["']([^"']*)["']/i.exec(sel[1]) || [])[1] ?? ""
      label = sel[2].trim()
    }
    fields[name] = value
    selMeta[name] = { value, label }
  }
  // slots: rows pairing a label cell with RouteID*/sequence* fields
  for (const m of form.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)) {
    const row = m[1]
    const rname = (/<select\b[^>]*\bname\s*=\s*["'](RouteID\d*)["']/i.exec(row) || [])[1]
    const sname = (/<input\b[^>]*\bname\s*=\s*["'](sequence\d*)["']/i.exec(row) || [])[1]
    if (!rname && !sname) continue
    const labelM = /<td[^>]*>([^<]{0,60})</i.exec(row)
    const rowLabel = (labelM?.[1] ?? "").trim()
    const meta = rname ? selMeta[rname] : undefined
    slots.push({
      rowLabel,
      routeField: rname ?? "",
      routeId: meta?.value ?? "",
      routeName: meta?.label ?? "",
      seqField: sname ?? "",
      seqValue: sname ? (fields[sname] ?? "") : "",
    })
  }
  return { fields, slots }
}

export async function main(
  customers: { ion_cust_id: string; new_seq: number; name: string }[] = [],
  mode: "dryrun" | "live" | "debug" | "routes" | "admin_form" | "create_routes" = "dryrun",
  target_route_id = "",
  route_names: string[] = [],
) {
  const raw = await wmill.getVariable(CACHE)
  if (!raw) throw new Error("no cached ION session")
  const s = JSON.parse(raw)
  const results: any[] = []

  if (mode === "admin_form") {
    // READ-ONLY: render the admin add-routes screen, dump its form structure
    const url = `${s.ionOrigin}/admin/addRoutes.cfm?source=home${target_route_id ? `&RouteID=${target_route_id}` : ""}&rand=0.1&_cf_containerId=cf_layoutarearoutecenter&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${s.cfClientId ?? ""}&_cf_rc=1`
    const page = await ionGet(s, url)
    const body = page.body
    const forms: any[] = []
    for (const m of body.matchAll(/<form\b[^>]*>/gi)) {
      const tag = m[0]
      const g = (k: string) => (new RegExp(`\\b${k}\\s*=\\s*["']([^"']*)["']`, "i").exec(tag) || [])[1] ?? null
      forms.push({ action: g("action"), method: g("method"), id: g("id"), name: g("name") })
    }
    const fields: any[] = []
    for (const m of body.matchAll(/<(input|select|textarea)\b[^>]*>/gi)) {
      const tag = m[0]
      const g = (k: string) => (new RegExp(`\\b${k}\\s*=\\s*["']([^"']*)["']`, "i").exec(tag) || [])[1] ?? null
      const f: any = { el: m[1], name: g("name"), id: g("id"), type: g("type"), value: g("value") }
      if (m[1].toLowerCase() === "select") {
        const close = body.toLowerCase().indexOf("</select>", m.index!)
        const inner = body.slice(m.index!, close < 0 ? m.index! + 40000 : close)
        const sel = /<option\b[^>]*\bselected\b[^>]*\bvalue\s*=\s*["']([^"']*)["']/i.exec(inner)
          || /<option\b[^>]*\bvalue\s*=\s*["']([^"']*)["'][^>]*\bselected\b/i.exec(inner)
        f.selected = sel?.[1] ?? null
        f.options = [...inner.matchAll(/<option\b[^>]*\bvalue\s*=\s*["']([^"']*)["'][^>]*>([^<]*)</gi)]
          .slice(0, 400).map((o) => ({ value: o[1], label: o[2].trim() }))
      }
      fields.push(f)
    }
    const hints = new Set<string>()
    for (const m of body.matchAll(/["'`](\/[A-Za-z0-9_\/]+\.cfm[^"'`]{0,60})["'`]/g)) hints.add(m[1])
    return {
      status: page.status, bytes: body.length,
      looksLogin: /txtPassword/i.test(body.slice(0, 4000)),
      forms, fields, cfmRefs: [...hints].slice(0, 25),
      head: body.slice(0, 1500),
    }
  }

  return await customerModes(s, customers, mode, target_route_id, results)
}

async function customerModes(s: any, customers: any[], mode: string, target_route_id: string, results: any[]) {

  for (const c of customers) {
    const out: any = { ion_cust_id: c.ion_cust_id, name: c.name, new_seq: c.new_seq }
    try {
      await ionGet(s, `${s.ionOrigin}/customers/customerTabs.cfm?customerid=${c.ion_cust_id}`)
      const url = `${s.ionOrigin}/customers/addRoute.cfm?id=${c.ion_cust_id}`
      const page = await ionGet(s, url)
      if (page.status !== 200 || /txtPassword/i.test(page.body.slice(0, 4000))) {
        out.error = `addRoute GET status ${page.status} / login page`
        results.push(out); continue
      }
      const { fields, slots } = parseForm(page.body)
      out.slots = slots.filter((sl) => sl.routeId || sl.seqValue)
      if (mode === "routes") {
        // dump the full RouteID dropdown (route roster)
        const m = /<select\b[^>]*\bname\s*=\s*["']RouteID["'][^>]*>/i.exec(page.body)
        if (m) {
          const close = page.body.toLowerCase().indexOf("</select>", m.index)
          const inner = page.body.slice(m.index, close)
          out.routes = [...inner.matchAll(/<option\b[^>]*\bvalue\s*=\s*["']([^"']+)["'][^>]*>([^<]*)</gi)]
            .map((o) => ({ id: o[1], name: o[2].trim() }))
        }
        results.push(out); continue
      }
      if (mode === "debug") {
        out.allSlots = slots
        out.fieldNames = Object.entries(fields).map(([k, v]) => `${k}=${v}`).slice(0, 40)
        out.bytes = page.body.length
        out.hasForm = /addcustroute/i.test(page.body)
        const i = page.body.search(/addcustroute/i)
        out.formHead = page.body.slice(Math.max(0, i - 100), i + 1800)
      }

      if (mode === "live") {
        const hit = slots.find((sl) => sl.routeId === target_route_id)
        if (!hit) { out.error = `no slot with RouteID ${target_route_id}`; results.push(out); continue }
        out.matched = { rowLabel: hit.rowLabel, routeName: hit.routeName, seqField: hit.seqField, before: hit.seqValue }
        if (String(hit.seqValue).trim() === String(c.new_seq)) {
          out.skipped = "already at target sequence"
          results.push(out); continue
        }
        const post = { ...fields, [hit.seqField]: String(c.new_seq), submit: "Update Routes" }
        const resp = await ionPost(s, url, post)
        out.post_status = resp.status
        // verify by re-reading
        const check = await ionGet(s, url)
        const after = parseForm(check.body).slots.find((sl) => sl.routeId === target_route_id)
        out.after = after?.seqValue ?? null
        out.verified = String(after?.seqValue ?? "").trim() === String(c.new_seq)
      }
    } catch (e: any) {
      out.error = String(e?.message ?? e)
    }
    results.push(out)
  }
  return { mode, target_route_id, count: results.length, results }
}

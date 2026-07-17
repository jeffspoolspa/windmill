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
  mode: "dryrun" | "live" | "debug" | "routes" | "admin_form" | "create_routes" | "assign_full" = "dryrun",
  target_route_id = "",
  route_names: string[] = [],
  new_routes: { name: string; technician?: string }[] = [],
  accounts: { ion_cust_id: string; name?: string; days: { dow: number; route_id: string; seq: number }[] }[] = [],
) {
  const raw = await wmill.getVariable(CACHE)
  if (!raw) throw new Error("no cached ION session")
  const s0 = JSON.parse(raw)

  if (mode === "assign_full") {
    // Multi-day account: write each in-scope day into its day-numbered slot
    // (dow d -> RouteID{d+1}/sequence{d+1}), CLEAR the default slot, and PRESERVE
    // every other slot (out-of-scope days like Candice weekends). One POST/account.
    const out: any[] = []
    for (const a of accounts) {
      const rec: any = { ion_cust_id: a.ion_cust_id, name: a.name }
      try {
        await ionGet(s0, `${s0.ionOrigin}/customers/customerTabs.cfm?customerid=${a.ion_cust_id}`)
        const url = `${s0.ionOrigin}/customers/addRoute.cfm?id=${a.ion_cust_id}`
        const page = await ionGet(s0, url)
        const { fields } = parseForm(page.body)
        const post: Record<string, string> = { ...fields, RouteID: "", sequence: "", submit: "Update Routes" }
        const want: Record<number, string> = {}
        for (const d of a.days) {
          const n = d.dow + 1
          post[`RouteID${n}`] = d.route_id
          post[`sequence${n}`] = String(d.seq)
          want[n] = d.route_id
        }
        const resp = await ionPost(s0, url, post)
        rec.post_status = resp.status
        const after = parseForm((await ionGet(s0, url)).body)
        const amap: Record<string, string> = {}
        for (const sl of after.slots) if (sl.routeId) amap[sl.routeField] = sl.routeId
        rec.verified = Object.entries(want).every(([n, rid]) => amap[`RouteID${n}`] === rid)
        rec.after = after.slots.filter((sl) => sl.routeId).map((sl) => `${sl.routeField}=${sl.routeId}:${sl.seqValue}`)
      } catch (e: any) { rec.error = String(e?.message ?? e) }
      out.push(rec)
    }
    return { mode, count: out.length, results: out }
  }
  const s = JSON.parse(raw)
  const results: any[] = []

  if (mode === "create_routes") {
    const dec = (t: string) => t.replace(/&#x28;/gi, "(").replace(/&#x29;/gi, ")").replace(/&amp;/gi, "&").replace(/\s+/g, " ").trim()
    const postUrl = `${s.ionOrigin}/admin/addRoutes.cfm?source=home&rand=0.1&_cf_containerId=cf_layoutarearoutecenter&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${s.cfClientId ?? ""}&_cf_rc=1`
    const created: any[] = []
    for (const r of new_routes) {
      const resp = await ionPost(s, postUrl, {
        RouteName: r.name,
        Technician: r.technician ?? "",
        StartLocation: "1416",
        StopLocation: "1416",
        RouteDesc: "",
        RouteID: "",
        FromSource: "home",
        Submit: "Submit",
      })
      created.push({ name: r.name, post_status: resp.status, err: /error|not\s+authorized|denied/i.test(resp.body.slice(0, 2000)) ? resp.body.slice(0, 300) : null })
    }
    // verify: re-pull the route dropdown from a customer page and resolve each name
    await ionGet(s, `${s.ionOrigin}/customers/customerTabs.cfm?customerid=2465326`)
    const page = await ionGet(s, `${s.ionOrigin}/customers/addRoute.cfm?id=2465326`)
    const m = /<select\b[^>]*\bname\s*=\s*["']RouteID["'][^>]*>/i.exec(page.body)
    const roster: { id: string; name: string }[] = []
    if (m) {
      const close = page.body.toLowerCase().indexOf("</select>", m.index)
      for (const o of page.body.slice(m.index, close).matchAll(/<option\b[^>]*\bvalue\s*=\s*["']([^"']+)["'][^>]*>([^<]*)</gi))
        roster.push({ id: o[1], name: dec(o[2]) })
    }
    for (const c of created) {
      const hit = roster.filter((r) => r.name.toUpperCase() === c.name.toUpperCase())
      c.resolved_ids = hit.map((h) => h.id)
      c.verified = hit.length === 1
    }
    return { created, rosterCount: roster.length }
  }

  if (mode === "routes_list") {
    // READ-ONLY: the routes listing — see existing routes' tech/location columns + edit link shape
    const url = `${s.ionOrigin}/routes/routes.cfm`
    const page = await ionGet(s, url)
    const rows: string[] = []
    for (const m of page.body.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)) {
      const row = m[1]
      if (!/RH - |addRoutes\.cfm|RouteID/i.test(row)) continue
      rows.push(row.replace(/\s+/g, " ").trim().slice(0, 500))
      if (rows.length >= 12) break
    }
    return { status: page.status, bytes: page.body.length, sampleRows: rows, head: page.body.slice(0, 800) }
  }

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
        const rid = c.route_id ?? target_route_id   // per-customer route wins
        let hit = slots.find((sl) => sl.routeId === rid)
        if (!hit) {
          // not assigned yet -> place into the requested slot (default or day)
          const slotMap: Record<string, { routeField: string; seqField: string }> = {
            default: { routeField: "RouteID", seqField: "sequence" },
            sunday: { routeField: "RouteID1", seqField: "sequence1" },
            monday: { routeField: "RouteID2", seqField: "sequence2" },
            tuesday: { routeField: "RouteID3", seqField: "sequence3" },
            wednesday: { routeField: "RouteID4", seqField: "sequence4" },
            thursday: { routeField: "RouteID5", seqField: "sequence5" },
            friday: { routeField: "RouteID6", seqField: "sequence6" },
            saturday: { routeField: "RouteID7", seqField: "sequence7" },
          }
          const want = slotMap[(c.assign_slot ?? "default").toLowerCase()]
          if (!want) { out.error = `bad assign_slot ${c.assign_slot}`; results.push(out); continue }
          const occupied = (fields[want.routeField] ?? "").trim()
          if (occupied && occupied !== rid && !c.allow_reassign) {
            out.error = `slot ${want.routeField} occupied by RouteID ${occupied} — not clobbering`
            results.push(out); continue
          }
          if (occupied && occupied !== rid) out.reassigned_from = occupied
          hit = { rowLabel: c.assign_slot ?? "default", routeField: want.routeField, routeId: "", routeName: "(assigning)", seqField: want.seqField, seqValue: fields[want.seqField] ?? "" }
        }
        out.matched = { rowLabel: hit.rowLabel, routeName: hit.routeName, seqField: hit.seqField, before: hit.seqValue }
        if (String(hit.seqValue).trim() === String(c.new_seq)) {
          out.skipped = "already at target sequence"
          results.push(out); continue
        }
        const post = { ...fields, [hit.routeField]: rid, [hit.seqField]: String(c.new_seq), submit: "Update Routes" }
        const resp = await ionPost(s, url, post)
        out.post_status = resp.status
        // verify by re-reading
        const check = await ionGet(s, url)
        const after = parseForm(check.body).slots.find((sl) => sl.routeId === rid)
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

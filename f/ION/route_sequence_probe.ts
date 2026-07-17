// requirements:
// windmill-client
// f/ION/route_sequence_probe — READ-ONLY endpoint discovery. Reuses the cached
// ION session (f/ION/session_cache) over pure HTTP (no browser). Primes the
// customer, then GETs the cstdetails + customerInfo containers and extracts the
// form action + any route/sequence fields. Writes NOTHING. Temporary; delete after.
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

// pull <input|select|textarea> whose name/id mentions route or seq
function fields(html: string): any[] {
  const out: any[] = []
  const re = /<(input|select|textarea)\b[^>]*>/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(html))) {
    const tag = m[0]
    const nm = /\b(name|id)\s*=\s*["']([^"']*)["']/gi
    let names = ""
    let a: RegExpExecArray | null
    while ((a = nm.exec(tag))) names += " " + a[2]
    if (!/route|seq/i.test(names)) continue
    const val = /\bvalue\s*=\s*["']([^"']*)["']/i.exec(tag)
    const name = /\bname\s*=\s*["']([^"']*)["']/i.exec(tag)
    const id = /\bid\s*=\s*["']([^"']*)["']/i.exec(tag)
    const entry: any = { el: m[1], name: name?.[1] ?? null, id: id?.[1] ?? null, value: val?.[1] ?? null }
    if (m[1].toLowerCase() === "select") {
      // capture selected option within this select
      const sIdx = m.index
      const close = html.toLowerCase().indexOf("</select>", sIdx)
      const inner = html.slice(sIdx, close < 0 ? sIdx + 2000 : close)
      const opt = /<option\b[^>]*\bselected[^>]*>([^<]*)<|<option\b[^>]*value\s*=\s*["']([^"']*)["'][^>]*\bselected/i.exec(inner)
      const selVal = /<option\b[^>]*\bselected\b[^>]*\bvalue\s*=\s*["']([^"']*)["']/i.exec(inner)
        || /<option\b[^>]*\bvalue\s*=\s*["']([^"']*)["'][^>]*\bselected\b/i.exec(inner)
      entry.selectedOption = selVal?.[1] ?? (opt ? (opt[1] || opt[2]) : null)
      const opts = [...inner.matchAll(/<option\b[^>]*\bvalue\s*=\s*["']([^"']*)["'][^>]*>([^<]*)</gi)].slice(0, 12)
      entry.optionSample = opts.map((o) => ({ value: o[1], label: o[2].trim() }))
    }
    out.push(entry)
  }
  return out
}

function forms(html: string): any[] {
  const out: any[] = []
  const re = /<form\b[^>]*>/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(html))) {
    const tag = m[0]
    out.push({
      action: (/\baction\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1] ?? null,
      method: (/\bmethod\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1] ?? "GET",
      id: (/\bid\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1] ?? null,
      name: (/\bname\s*=\s*["']([^"']*)["']/i.exec(tag) || [])[1] ?? null,
    })
  }
  return out
}

// ALL form fields (name/id/type/value + selected option) — full edit form dump
function allFields(html: string): any[] {
  const out: any[] = []
  const re = /<(input|select|textarea)\b[^>]*>/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(html))) {
    const tag = m[0]
    const g = (k: string) => (new RegExp(`\\b${k}\\s*=\\s*["']([^"']*)["']`, "i").exec(tag) || [])[1] ?? null
    const entry: any = { el: m[1], name: g("name"), id: g("id"), type: g("type"), value: g("value") }
    if (m[1].toLowerCase() === "select") {
      const close = html.toLowerCase().indexOf("</select>", m.index)
      const inner = html.slice(m.index, close < 0 ? m.index + 3000 : close)
      const sel = /<option\b[^>]*\bselected\b[^>]*\bvalue\s*=\s*["']([^"']*)["']/i.exec(inner)
        || /<option\b[^>]*\bvalue\s*=\s*["']([^"']*)["'][^>]*\bselected\b/i.exec(inner)
      entry.selected = sel?.[1] ?? null
      entry.optionCount = (inner.match(/<option\b/gi) || []).length
    }
    out.push(entry)
  }
  return out
}

// find function/endpoints referenced near a save (save*, update*, .cfm in JS)
function saveHints(html: string): string[] {
  const hits = new Set<string>()
  for (const m of html.matchAll(/\b([A-Za-z_]*[Ss]ave[A-Za-z_]*|[A-Za-z_]*[Uu]pdate[A-Za-z_]*)\s*\(/g)) hits.add(m[1] + "()")
  for (const m of html.matchAll(/["'`](\/[A-Za-z0-9_\/]+\.cfm)["'`]/g)) hits.add(m[1])
  return [...hits].slice(0, 30)
}

export async function main(customerid = "1124422") {
  const raw = await wmill.getVariable(CACHE)
  if (!raw) throw new Error("no cached ION session")
  const s = JSON.parse(raw)

  // 1) prime the session's current customer
  const prime = await ionGet(s, `${s.ionOrigin}/customers/customerTabs.cfm?customerid=${customerid}`)

  const cid = s.cfClientId ?? ""
  const q = `rand=0.1&_cf_nodebug=true&_cf_nocache=true&_cf_clientid=${cid}&_cf_rc=1`

  // 2) the details (edit) container — where the sequence save form lives
  const details = await ionGet(s, `${s.ionOrigin}/customers/details.cfm?${q}&_cf_containerId=cstdetails`)
  // 3) the info container Carter's first curl hit (rendered route/sequence)
  const info = await ionGet(s, `${s.ionOrigin}/customers/customerTabs.cfm?customerid=${customerid}&${q}&_cf_containerId=customerInfo`)

  const snip = (html: string, kw: string, n = 3, back = 30, fwd = 220) => {
    const low = html.toLowerCase(); const out: string[] = []
    let i = low.indexOf(kw), c = 0
    while (i >= 0 && c < n) { out.push(html.slice(Math.max(0, i - back), i + fwd).replace(/\s+/g, " ").trim()); i = low.indexOf(kw, i + 1); c++ }
    return out
  }

  // 4) the ACTUAL edit form (opened by the "Route Info" button)
  const addRoute = await ionGet(s, `${s.ionOrigin}/customers/addRoute.cfm?id=${customerid}`)

  return {
    customerid,
    prime: { status: prime.status, bytes: prime.body.length, looksLogin: /txtPassword/i.test(prime.body.slice(0, 4000)) },
    routeSeqDisplay: snip(info.body, "route seq", 2, 15, 120),
    routeNameDisplay: snip(info.body, "route name", 1, 15, 120),
    addRoute: {
      status: addRoute.status, bytes: addRoute.body.length,
      looksLogin: /txtPassword/i.test(addRoute.body.slice(0, 4000)),
      forms: forms(addRoute.body),
      fields: allFields(addRoute.body),
      saveHints: saveHints(addRoute.body),
      seqSnippets: snip(addRoute.body, "seq", 4, 40, 160),
      // first 1500 chars of the form region for eyeballing
      head: addRoute.body.slice(0, 1200),
    },
  }
}

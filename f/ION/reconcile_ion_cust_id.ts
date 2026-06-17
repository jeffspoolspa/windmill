//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
//postgres@3.4.5
import { chromium } from "playwright@1.40.0"
import postgres from "postgres@3.4.5"
import * as wmill from "windmill-client"

/**
 * ADR-006 ion_cust_id reconciler. Finds Customers missing ion_cust_id (default scope: customers
 * with an active maintenance task -- the "serviced gaps"), searches ION's customer list by name,
 * and writes ion_cust_id for HIGH-confidence matches only (normalized name + phone agree, unique).
 * Medium / ambiguous / same-phone ION duplicates / deleted customers go to the returned review
 * list and are NOT written. Runnable on a schedule and manually (pass customer_ids to target
 * specific rows; dry_run to preview without writing). Tag: chromium. ION exposes no QBO id, hence
 * fuzzy-match-once -- see docs/adrs/006.
 */
export async function main(
  dry_run = true,
  limit = 25,
  customer_ids: number[] = [],
  include_deleted = false,
) {
  const sb: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({
    host: sb.host, port: sb.port, database: sb.dbname, username: sb.user, password: sb.password,
    ssl: "require", max: 2, idle_timeout: 10, connect_timeout: 15,
  })

  // ── target customers ──────────────────────────────────────────────────────
  let targets: { id: number; qbo: string; name: string; phone: string | null }[]
  if (customer_ids.length) {
    targets = (await sql`
      select c.id, c.qbo_customer_id as qbo, c.display_name as name, c.phone
      from public."Customers" c
      where c.id = any(${customer_ids}) and c.ion_cust_id is null`) as any
  } else {
    targets = (await sql`
      select c.id, c.qbo_customer_id as qbo, c.display_name as name, c.phone
      from public.v_customer_data_quality v
      join public."Customers" c on c.id = v.id
      where v.missing_ion_active
        ${include_deleted ? sql`` : sql`and c.display_name not ilike '%(deleted)%'`}
      order by c.id desc
      limit ${limit}`) as any
  }

  const out: any = { dry_run, scope: customer_ids.length ? "explicit_ids" : "serviced_gaps", targeted: targets.length, written: 0, review: 0, results: [] }
  if (!targets.length) { await sql.end(); return out }

  // ── matcher (validated: 98% name-exact on 683 pairs; phone confirms) ───────
  const decodeText = (h: string) =>
    h.replace(/<[^>]*>/g, " ")
      .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)))
      .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
      .replace(/&amp;/g, "&").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim()
  const cleanName = (s: string) => (s || "").replace(/\([^)]*\)/g, " ").replace(/[*&#]/g, " ").toLowerCase()
  const norm = (s: string) => cleanName(s).replace(/[^a-z0-9]/g, "")
  const normSorted = (s: string) => cleanName(s).replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter(Boolean).sort().join("")
  const phone10 = (s: string | null) => { const d = (s || "").replace(/\D/g, ""); return d.length >= 10 ? d.slice(-10) : "" }
  const surnameTerm = (name: string) => {
    let s = name.replace(/\([^)]*\)/g, " ").trim()
    if (s.includes(",")) return s.split(",")[0].trim()
    const t = s.split(/\s+/).filter(Boolean); return t.length ? t[t.length - 1] : s
  }
  const fullTerm = (name: string) => name.replace(/\([^)]*\)/g, " ").replace(/\s+/g, " ").trim()

  interface Cand { ion: string; name: string; home: string; mobile: string }
  const parseCandidates = (body: string): Cand[] => {
    const o: Cand[] = []
    const re = /customerTabs\.cfm\?customerid=(\d+)',\s*'customerInfo'\)[^>]*>([\s\S]*?)<\/a>/gi
    let m: RegExpExecArray | null
    while ((m = re.exec(body)) !== null) {
      const after = body.slice(m.index + m[0].length, m.index + m[0].length + 700)
      const tds = [...after.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)].map((x) => decodeText(x[1]))
      o.push({ ion: m[1], name: decodeText(m[2]), home: tds[1] || "", mobile: tds[2] || "" })
    }
    return o
  }
  const RANK: Record<string, number> = { high: 3, medium: 2, review: 1, none: 0 }
  function matchOver(cands: Cand[], t: typeof targets[number]) {
    const tNE = norm(t.name), tNS = normSorted(t.name), tP = phone10(t.phone)
    const scored = cands.map((c) => ({
      c, nameExact: norm(c.name) === tNE, nameSorted: normSorted(c.name) === tNS,
      phoneMatch: tP !== "" && (phone10(c.home) === tP || phone10(c.mobile) === tP),
    }))
    const nameHits = scored.filter((s) => s.nameExact || s.nameSorted)
    let chosen: typeof scored[number] | null = null, confidence = "none", reason = ""
    if (nameHits.length === 1) { chosen = nameHits[0]; confidence = chosen.phoneMatch ? "high" : "medium"; reason = chosen.phoneMatch ? "name+phone" : "name_unique_no_phone" }
    else if (nameHits.length > 1) {
      const ph = nameHits.filter((s) => s.phoneMatch)
      if (ph.length === 1) { chosen = ph[0]; confidence = "high"; reason = "name+phone_disambiguated" }
      else { confidence = "review"; reason = ph.length >= 2 ? "duplicate_same_phone" : "ambiguous_name" }
    } else {
      const ph = scored.filter((s) => s.phoneMatch)
      if (ph.length === 1) { chosen = ph[0]; confidence = "medium"; reason = "phone_only" }
      else reason = "no_match"
    }
    return { chosen, confidence, reason, phoneHits: scored.filter((s) => s.phoneMatch).length }
  }

  // ── ION login ──────────────────────────────────────────────────────────────
  const username = await wmill.getVariable("f/ION/USERNAME")
  const password = await wmill.getVariable("f/ION/PASSWORD")
  const loginUrl = await wmill.getVariable("f/ION/LOGIN_URL")
  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox", "--single-process", "--no-zygote", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  })
  const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36" })
  const page = await context.newPage()

  try {
    await page.goto(loginUrl)
    await page.locator("#txtUserName").fill(username as string)
    await page.locator("#txtPassword").fill(password as string)
    await page.locator('button:has-text("Log In")').click()
    await page.waitForLoadState("networkidle", { timeout: 30000 })
    await page.locator('button[data-bs-target="#navbarToggleContent"]').click({ timeout: 5000 })
    await page.waitForTimeout(1000)
    await page.locator("text=ION POOL CARE").click({ timeout: 5000 })
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    const origin = new URL(page.url()).origin

    const search = (term: string) =>
      page.evaluate(async (u: string) => {
        try { const r = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } }); return await r.text() }
        catch (e: any) { return "ERR:" + String(e) }
      }, `${origin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=${encodeURIComponent(term)}&reset=1`)

    for (const t of targets) {
      const term1 = surnameTerm(t.name)
      let r = matchOver(parseCandidates(await search(term1)), t)
      let usedTerm = term1
      // Only narrow when pass1 failed due to breadth/noise -- NOT when it found a same-phone dup.
      if (r.confidence !== "high" && r.reason !== "duplicate_same_phone") {
        const term2 = fullTerm(t.name)
        if (term2.toLowerCase() !== term1.toLowerCase()) {
          const r2 = matchOver(parseCandidates(await search(term2)), t)
          if (RANK[r2.confidence] > RANK[r.confidence]) { r = r2; usedTerm = term2 }
          else if (RANK[r2.confidence] === RANK[r.confidence] && r.chosen && r2.chosen && r.chosen.c.ion !== r2.chosen.c.ion) {
            r = { ...r, chosen: null, confidence: "review", reason: "conflicting_passes" }
          }
        }
      }

      const chosenIon = r.chosen?.c.ion ?? null
      let action = "review"
      let error: string | null = null
      if (r.confidence === "high" && chosenIon) {
        if (dry_run) action = "would_write"
        else {
          try {
            const res = await sql`
              update public."Customers"
              set ion_cust_id = ${chosenIon}, ion_match_method = 'api_fuzzy',
                  ion_match_confidence = 'high', ion_matched_at = now()
              where id = ${t.id} and ion_cust_id is null`
            action = res.count > 0 ? "written" : "skipped_already_set"
            if (res.count > 0) out.written++
          } catch (e: any) { action = "write_error"; error = String(e?.message ?? e) }
        }
      }
      if (action === "review") out.review++
      out.results.push({
        id: t.id, qbo: t.qbo, name: t.name, used_term: usedTerm,
        chosen_ion: chosenIon, chosen_name: r.chosen?.c.name ?? null,
        confidence: r.confidence, reason: r.reason, action, error,
      })
    }
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
    await sql.end()
  }
  return out
}

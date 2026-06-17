//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"

const TARGETS: { kind: string; id: number; qbo: string; name: string; phone: string | null; known_ion: string | null }[] = [
  { kind: "control", id: 772962, qbo: "9848", name: "PRICE, CLAY", phone: "(912) 445-2127", known_ion: "2568084" },
  { kind: "control", id: 737504, qbo: "9845", name: "LEWIS, RALPH", phone: "(404) 441-2125", known_ion: "2567674" },
  { kind: "control", id: 728619, qbo: "9841", name: "HERRIN, ZACH", phone: "(912) 282-6805", known_ion: "2567423" },
  { kind: "control", id: 719722, qbo: "9839", name: "ZUKOWSKI, DAVID", phone: "(508) 725-1733", known_ion: "2567254" },
  { kind: "control", id: 710835, qbo: "9837", name: "BELL, NICK", phone: "(912) 230-1755", known_ion: "2567250" },
  { kind: "control", id: 675392, qbo: "9833", name: "THOMPSON, SPENCER", phone: "(949) 300-0485", known_ion: "2566745" },
  { kind: "control", id: 639930, qbo: "9823", name: "MECH, WILLIAM", phone: "(216) 952-3063", known_ion: "2565017" },
  { kind: "control", id: 559062, qbo: "9812", name: "SPARKS, BARBARA", phone: "(419) 508-8666", known_ion: "2563804" },
  { kind: "control", id: 548508, qbo: "9800", name: "NICHOLS, ANGELA", phone: "(912) 467-9657", known_ion: "2563048" },
  { kind: "control", id: 547777, qbo: "9806", name: "MASSEY, JEFFERY", phone: "2292914324", known_ion: "2563659" },
  { kind: "control", id: 547593, qbo: "9810", name: "LUCAS, BRIANNA", phone: "(912) 312-4818", known_ion: "2563757" },
  { kind: "control", id: 544129, qbo: "9797", name: "CHESSER, KAREN", phone: "6156931766", known_ion: "2562987" },
  { kind: "gap", id: 593467, qbo: "9819", name: "Richard Ingalls", phone: "(770) 310-2819", known_ion: null },
  { kind: "gap", id: 2511, qbo: "4709", name: "FLEXER, B.K.", phone: "264-3522", known_ion: null },
  { kind: "gap", id: 474, qbo: "6954", name: "BEANE, BOB (deleted)", phone: "678-381-6326", known_ion: null },
  { kind: "gap", id: 8556, qbo: "212", name: "WILLS, BRIAN", phone: "404-403-9777 - BRIAN", known_ion: null },
  { kind: "gap", id: 7459, qbo: "1062", name: "STEMPF, PETER", phone: "912-230-6237", known_ion: null },
]

function decodeText(h: string): string {
  return h
    .replace(/<[^>]*>/g, " ")
    .replace(/&#x([0-9a-f]+);/gi, (_, n) => String.fromCharCode(parseInt(n, 16)))
    .replace(/&#(\d+);/g, (_, n) => String.fromCharCode(parseInt(n, 10)))
    .replace(/&amp;/g, "&").replace(/&nbsp;/g, " ").replace(/\s+/g, " ").trim()
}
const cleanName = (s: string) => (s || "").replace(/\([^)]*\)/g, " ").replace(/[*&#]/g, " ").toLowerCase()
const norm = (s: string) => cleanName(s).replace(/[^a-z0-9]/g, "")
const normSorted = (s: string) => cleanName(s).replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter(Boolean).sort().join("")
function phone10(s: string | null): string {
  const d = (s || "").replace(/\D/g, "")
  return d.length >= 10 ? d.slice(-10) : ""
}
function surnameTerm(name: string): string {
  let s = name.replace(/\([^)]*\)/g, " ").trim()
  if (s.includes(",")) return s.split(",")[0].trim()
  const toks = s.split(/\s+/).filter(Boolean)
  return toks.length ? toks[toks.length - 1] : s
}
const fullTerm = (name: string) => name.replace(/\([^)]*\)/g, " ").replace(/\s+/g, " ").trim()

interface Cand { ion: string; name: string; home: string; mobile: string }
function parseCandidates(body: string): Cand[] {
  const out: Cand[] = []
  const re = /customerTabs\.cfm\?customerid=(\d+)',\s*'customerInfo'\)[^>]*>([\s\S]*?)<\/a>/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(body)) !== null) {
    const after = body.slice(m.index + m[0].length, m.index + m[0].length + 700)
    const tds = [...after.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)].map((x) => decodeText(x[1]))
    out.push({ ion: m[1], name: decodeText(m[2]), home: tds[1] || "", mobile: tds[2] || "" })
  }
  return out
}

const RANK: Record<string, number> = { high: 3, medium: 2, review: 1, none: 0 }
function matchOver(cands: Cand[], t: typeof TARGETS[number]) {
  const tNameExact = norm(t.name), tNameSorted = normSorted(t.name), tPhone = phone10(t.phone)
  const scored = cands.map((c) => ({
    c,
    nameExact: norm(c.name) === tNameExact,
    nameSorted: normSorted(c.name) === tNameSorted,
    phoneMatch: tPhone !== "" && (phone10(c.home) === tPhone || phone10(c.mobile) === tPhone),
  }))
  const nameHits = scored.filter((s) => s.nameExact || s.nameSorted)
  let chosen: typeof scored[number] | null = null
  let confidence = "none"
  if (nameHits.length === 1) { chosen = nameHits[0]; confidence = chosen.phoneMatch ? "high" : "medium" }
  else if (nameHits.length > 1) {
    const ph = nameHits.filter((s) => s.phoneMatch)
    if (ph.length === 1) { chosen = ph[0]; confidence = "high" } else { confidence = "review" }
  } else {
    const ph = scored.filter((s) => s.phoneMatch)
    if (ph.length === 1) { chosen = ph[0]; confidence = "medium" }
  }
  return { chosen, confidence, scored, count: cands.length }
}

export async function main() {
  const username = await wmill.getVariable("f/ION/USERNAME")
  const password = await wmill.getVariable("f/ION/PASSWORD")
  const loginUrl = await wmill.getVariable("f/ION/LOGIN_URL")

  const browser = await chromium.launch({
    executablePath: "/usr/bin/chromium",
    args: ["--no-sandbox", "--single-process", "--no-zygote", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
  })
  const context = await browser.newContext({ userAgent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", acceptDownloads: true })
  const page = await context.newPage()
  const out: any = { logged_in: false, results: [] }

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
    out.logged_in = true

    const search = (term: string) =>
      page.evaluate(async (u: string) => {
        try { const res = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } }); return await res.text() }
        catch (e: any) { return "ERR:" + String(e) }
      }, `${origin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=${encodeURIComponent(term)}&reset=1`)

    let reproduced = 0, controls = 0, matched = 0
    for (const t of TARGETS) {
      const term1 = surnameTerm(t.name)
      let r = matchOver(parseCandidates(await search(term1)), t)
      let usedTerm = term1, passes = 1
      if (r.confidence !== "high") {
        const term2 = fullTerm(t.name)
        if (term2.toLowerCase() !== term1.toLowerCase()) {
          const r2 = matchOver(parseCandidates(await search(term2)), t)
          passes = 2
          if (RANK[r2.confidence] > RANK[r.confidence]) { r = r2; usedTerm = term2 }
          else if (RANK[r2.confidence] === RANK[r.confidence] && r.chosen && r2.chosen && r.chosen.c.ion !== r2.chosen.c.ion) {
            r = { ...r, chosen: null, confidence: "review" }
          }
        }
      }
      const chosenIon = r.chosen?.c.ion ?? null
      const repro = t.known_ion ? chosenIon === t.known_ion : null
      if (t.kind === "control") { controls++; if (repro) reproduced++ }
      if (r.chosen) matched++
      out.results.push({
        kind: t.kind, name: t.name, qbo: t.qbo, known_ion: t.known_ion,
        used_term: usedTerm, passes, candidates: r.count,
        chosen_ion: chosenIon, chosen_name: r.chosen?.c.name ?? null, confidence: r.confidence,
        reproduced: repro,
      })
    }
    out.summary = { controls, reproduced, matched, total: TARGETS.length }
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

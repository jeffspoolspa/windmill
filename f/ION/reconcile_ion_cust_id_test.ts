//bun-extra-requirements:
//playwright@1.48.0
//chromium-bidi@0.8.0
import { chromium } from "playwright@1.40.0"
import * as wmill from "windmill-client"

// Embedded test targets (dry-run validation before wiring DB read/write).
// "control" rows have known_ion -> the reconciler must reproduce it.
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
    .replace(/&amp;/g, "&")
    .replace(/&nbsp;/g, " ")
    .replace(/\s+/g, " ")
    .trim()
}
function cleanName(s: string): string {
  return (s || "").replace(/\([^)]*\)/g, " ").replace(/[*&#]/g, " ").toLowerCase()
}
function norm(s: string): string {
  return cleanName(s).replace(/[^a-z0-9]/g, "")
}
function normSorted(s: string): string {
  return cleanName(s).replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter(Boolean).sort().join("")
}
function phone10(s: string | null): string {
  const d = (s || "").replace(/\D/g, "")
  return d.length >= 10 ? d.slice(-10) : ""
}
function searchTerm(name: string): string {
  let s = name.replace(/\([^)]*\)/g, " ").trim()
  if (s.includes(",")) return s.split(",")[0].trim()
  const toks = s.split(/\s+/).filter(Boolean)
  return toks.length ? toks[toks.length - 1] : s
}

interface Cand { ion: string; name: string; location: string; home: string; mobile: string }

function parseCandidates(body: string): Cand[] {
  const out: Cand[] = []
  const re = /customerTabs\.cfm\?customerid=(\d+)',\s*'customerInfo'\)[^>]*>([\s\S]*?)<\/a>/gi
  let m: RegExpExecArray | null
  while ((m = re.exec(body)) !== null) {
    const ion = m[1]
    const name = decodeText(m[2])
    const after = body.slice(m.index + m[0].length, m.index + m[0].length + 700)
    const tds = [...after.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)].map((x) => decodeText(x[1]))
    out.push({ ion, name, location: tds[0] || "", home: tds[1] || "", mobile: tds[2] || "" })
  }
  return out
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

    const fetchUrl = (url: string) =>
      page.evaluate(async (u: string) => {
        try {
          const res = await fetch(u, { credentials: "include", headers: { Accept: "text/html, */*" } })
          return await res.text()
        } catch (e: any) {
          return "ERR:" + String(e)
        }
      }, url)

    let ok = 0, reproduced = 0, controls = 0
    for (const t of TARGETS) {
      const term = searchTerm(t.name)
      const body = await fetchUrl(
        `${origin}/customers/customerlist.cfm?officeid=0&techid=0&routeid=0&search=${encodeURIComponent(term)}&reset=1`,
      )
      const cands = parseCandidates(body)
      const capped = body.includes("500 customers matching")
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
      if (nameHits.length === 1) {
        chosen = nameHits[0]
        confidence = chosen.phoneMatch ? "high" : "medium"
      } else if (nameHits.length > 1) {
        const ph = nameHits.filter((s) => s.phoneMatch)
        if (ph.length === 1) { chosen = ph[0]; confidence = "high" }
        else { confidence = "review"; }
      } else {
        const ph = scored.filter((s) => s.phoneMatch)
        if (ph.length === 1) { chosen = ph[0]; confidence = "medium" }
      }

      const chosenIon = chosen?.c.ion ?? null
      const repro = t.known_ion ? chosenIon === t.known_ion : null
      if (t.kind === "control") { controls++; if (repro) reproduced++ }
      if (chosen) ok++

      out.results.push({
        kind: t.kind, name: t.name, qbo: t.qbo, known_ion: t.known_ion,
        search_term: term, candidates: cands.length, capped,
        chosen_ion: chosenIon, chosen_name: chosen?.c.name ?? null, confidence,
        reproduced: repro,
        top: scored.slice(0, 4).map((s) => ({ ion: s.c.ion, name: s.c.name, home: s.c.home, mobile: s.c.mobile, nameExact: s.nameExact, nameSorted: s.nameSorted, phoneMatch: s.phoneMatch })),
      })
    }
    out.summary = { controls, reproduced, matched_any: ok, total: TARGETS.length }
  } catch (e: any) {
    out.error = String(e)
  } finally {
    await browser.close()
  }
  return out
}

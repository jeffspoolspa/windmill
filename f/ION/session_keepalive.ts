// session_keepalive — keeps the injected ION session alive server-side WITHOUT
// a browser. Reads f/ION/session_cache, GETs a lightweight ION page with the
// cookies (resets ION's inactivity clock), and bumps the cache's expiresAt so
// getOrRefreshSession keeps reusing it (never launches the broken worker
// chromium). Runs on the DEFAULT worker (pure HTTP — NOT the chromium tag).
//
// Pairs with an external login minter (GitHub Actions) that re-mints a fresh
// session every few hours: keepalive stretches each session; the minter
// replaces it before ION's absolute max-lifetime. If ION bounces us to login
// (session finally died), keepalive reports alive=false so the minter / an
// alert can react. Schedule: every ~10 min.
import * as wmill from "windmill-client"

const CACHE = "f/ION/session_cache"

function cookieHeader(s: any): string {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

export async function main(extend_minutes = 120) {
  const raw = await wmill.getVariable(CACHE)
  if (!raw) return { alive: false, reason: "no session in cache — needs a fresh login mint" }
  const s = JSON.parse(raw)

  const resp = await fetch(`${s.ionOrigin}/main.cfm`, {
    headers: {
      Cookie: cookieHeader(s),
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      Accept: "text/html, */*",
    },
    redirect: "manual",
  })
  const body = await resp.text()
  // logged-in ION app page is ~50KB+; a bounce is a 302 to login or a tiny login form
  const alive = resp.status === 200 && body.length > 20000 && !/txtPassword/i.test(body.slice(0, 4000))

  if (alive) {
    // ION accepted us -> the session is warm. Push expiresAt out so
    // getOrRefreshSession keeps reusing it (no browser refresh attempt).
    s.expiresAt = Date.now() + extend_minutes * 60 * 1000
    await wmill.setVariable(CACHE, JSON.stringify(s))
    return { alive: true, http: resp.status, bytes: body.length, extended_min: extend_minutes,
             age_min: Math.round((Date.now() - s.capturedAt) / 60000) }
  }
  return { alive: false, http: resp.status, bytes: body.length,
           reason: "ION bounced us — session dead; the login minter must re-mint" }
}

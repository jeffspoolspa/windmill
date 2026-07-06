// session_http_test — does an EXTERNALLY-MINTED session (injected into
// f/ION/session_cache from a Mac) work from the Windmill worker's IP? Reads
// the cached session (NO browser), fetches one lightweight ION page with the
// cookies, reports HTTP status + a content signature. 200 with real app
// markup => injection works, no IP binding => ingest can run browser-free.
// Redirect-to-login / 403 => the session was rejected (IP-bound or stale).
import * as wmill from "windmill-client"

function cookieHeader(s: any): string {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

export async function main() {
  const raw = await wmill.getVariable("f/ION/session_cache")
  const rec: any = {}
  if (!raw) { rec.error = "session_cache empty"; return rec }
  const s = JSON.parse(raw)
  rec.origin = s.ionOrigin
  rec.cookie_count = s.cookies?.length
  rec.fresh = Date.now() < s.expiresAt - 60000
  rec.age_min = Math.round((Date.now() - s.capturedAt) / 60000)

  const url = `${s.ionOrigin}/main.cfm`
  const resp = await fetch(url, {
    headers: {
      Cookie: cookieHeader(s),
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
      Accept: "text/html, */*",
    },
    redirect: "manual",
  })
  rec.http_status = resp.status
  rec.location = resp.headers.get("location") ?? null
  const body = await resp.text()
  rec.bytes = body.length
  // signatures: logged-in app vs bounced-to-login
  rec.looks_logged_in = /logout|ionpoolcare|ServiceLog|main\.cfm|dashboard/i.test(body)
  rec.looks_like_login = /txtUserName|txtPassword|fluidra|sign in|log in/i.test(body)
  rec.body_head = body.replace(/\s+/g, " ").slice(0, 200)
  return rec
}

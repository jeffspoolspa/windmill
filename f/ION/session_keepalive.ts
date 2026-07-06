// requirements:
// postgres
// session_keepalive — keeps the injected ION session alive server-side WITHOUT
// a browser, AND makes the pipeline self-healing. Runs on the DEFAULT worker
// (pure HTTP — NOT the chromium tag). Schedule: every ~10 min.
//
// Each tick:
//   - GET a lightweight ION page with the cached cookies.
//   - ALIVE  -> bump session_cache.expiresAt (getOrRefreshSession keeps reusing
//               it; never launches the broken worker chromium). Clear alarms.
//   - DEAD   -> the session finally expired. We CANNOT re-login here (no
//               browser). Instead:
//               1. Trigger the GitHub Actions minter immediately
//                  (workflow_dispatch) so a fresh session is minted in ~2 min,
//                  instead of waiting for its 4h schedule. (Needs f/ION/GITHUB_TOKEN.)
//               2. Raise a system_alerts row (deduped) so a human is looped in
//                  if minting keeps failing (ION down / creds changed / GH out).
//
// There is NO deadlock risk: the minter does a FRESH login independent of the
// current session, so it always recovers regardless of cache state.
import * as wmill from "windmill-client"
import postgres from "postgres"

const CACHE = "f/ION/session_cache"
const REPO = "jeffspoolspa/servicebilling"
const WORKFLOW = "ion-session.yml"

function cookieHeader(s: any): string {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies
    .filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`)
    .join("; ")
}

async function triggerMint(): Promise<string> {
  let ghToken: string | null = null
  try { ghToken = await wmill.getVariable("f/ION/GITHUB_TOKEN") } catch { /* not configured */ }
  if (!ghToken) return "no GITHUB_TOKEN var — skipped self-heal trigger"
  const r = await fetch(
    `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${ghToken}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main", inputs: { trigger_ingest: true } }),
    },
  )
  return r.status === 204 ? "GitHub minter triggered" : `GitHub dispatch HTTP ${r.status}`
}

async function alertDeadOnce(sql: any, detail: string) {
  // dedupe: only one alert per death episode (nothing pending/sent in last 3h)
  const recent = await sql`
    select 1 from public.system_alerts
    where source = 'ion_session_keepalive' and created_at > now() - interval '3 hours' limit 1`
  if (recent.length) return "alert already raised this episode"
  await sql`
    insert into public.system_alerts (source, severity, subject, body_text, body_html)
    values ('ion_session_keepalive', 'high',
      'ION session dead — automatic re-mint triggered',
      ${"The cached ION session was rejected. " + detail +
        " A fresh mint was requested; if ingests keep failing, check the ion-session GitHub Action, ION credentials, and ION availability."},
      ${"<p>The cached ION session was rejected.</p><p>" + detail +
        "</p><p>A fresh mint was requested; if ingests keep failing, check the ion-session GitHub Action, ION credentials, and ION availability.</p>"})`
  return "alert raised"
}

export async function main(extend_minutes = 120) {
  const raw = await wmill.getVariable(CACHE)
  const sb = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user,
                         password: sb.password, ssl: "require", max: 1 })
  try {
    if (!raw) {
      const trig = await triggerMint()
      const alert = await alertDeadOnce(sql, "session_cache is empty.")
      return { alive: false, reason: "no session in cache", self_heal: trig, alert }
    }
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
    const alive = resp.status === 200 && body.length > 20000 && !/txtPassword/i.test(body.slice(0, 4000))

    if (alive) {
      s.expiresAt = Date.now() + extend_minutes * 60 * 1000
      await wmill.setVariable(CACHE, JSON.stringify(s))
      return { alive: true, http: resp.status, bytes: body.length, extended_min: extend_minutes,
               age_min: Math.round((Date.now() - s.capturedAt) / 60000) }
    }
    // dead: self-heal + alert (both idempotent)
    const trig = await triggerMint()
    const alert = await alertDeadOnce(sql, `ION returned HTTP ${resp.status}.`)
    return { alive: false, http: resp.status, bytes: body.length, self_heal: trig, alert }
  } finally {
    await sql.end()
  }
}

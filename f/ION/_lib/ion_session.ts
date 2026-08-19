// The one shared ION login — BROWSERLESS. ION's login is a ColdFusion form:
// three fields, no CSRF token, no SSO hop (the Fluidra portal hop retired
// 2026-08; chromium in these scripts was a fossil of it). Recipe ported from
// the .NET adapter (src/Ion/IonSession.cs), verified against the live site:
//   - POST /security/login.cfm answers 302 when it ACCEPTS and 200 when it
//     REFUSES — ColdFusion re-renders the form on failure, a successful HTTP
//     response describing a failed login. Never auto-follow redirects.
//   - An EXPIRED session answers 200 with a body that is nothing but a
//     security/logout.cfm redirect stub — status codes lie here too.
//   - A THROTTLED login does not answer at all; it hangs. Bound every call
//     and never retry a refused login in a loop — that turns a throttle
//     into an outage.
// Fresh session per job: ION keeps its cursor server-side, so a session
// shared across concurrent jobs reads the wrong customer with no error.

export type IonCredentials = {
  username: string
  password: string
}

const ION_ORIGIN = "https://ionpoolcare.com"
const BROWSER_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
const DEFAULT_INACTIVITY_MS = 15 * 60 * 1000

export interface IonCookie {
  name: string
  value: string
}

export interface IonSession {
  cookies: IonCookie[]
  cfClientId: string | undefined
  ionOrigin: string
  capturedAt: number
  expiresAt: number
}

function absorbCookies(res: Response, jar: Map<string, string>): void {
  const set = (res.headers as any).getSetCookie?.() as string[] | undefined
  const all = set ?? (res.headers.get("set-cookie") ? [res.headers.get("set-cookie") as string] : [])
  for (const line of all) {
    const pair = line.split(";")[0]
    const eq = pair.indexOf("=")
    if (eq > 0) jar.set(pair.slice(0, eq).trim(), pair.slice(eq + 1).trim())
  }
}

function jarHeader(jar: Map<string, string>): string {
  return [...jar.entries()].map(([k, v]) => `${k}=${v}`).join("; ")
}

export async function loginToIon(ion: IonCredentials): Promise<IonSession> {
  const jar = new Map<string, string>()

  const login = await fetch(`${ION_ORIGIN}/security/login.cfm`, {
    method: "POST",
    body: new URLSearchParams({
      IPCLogin: ion.username,
      IPCPassword: ion.password,
      Submitted: "Log In",
    }),
    redirect: "manual",
    headers: { "User-Agent": BROWSER_USER_AGENT, Accept: "text/html, */*" },
    signal: AbortSignal.timeout(45000), // a throttled login hangs, not errors
  })
  absorbCookies(login, jar)
  if (login.status !== 302) {
    const body = await login.text()
    throw new Error(
      `ION login refused: HTTP ${login.status}` +
      (looksLikeLoginPage(body) ? " (form re-rendered — bad credentials?)" : ""))
  }

  // main.cfm renders the CF AJAX bootstrap; _cf_clientid is in its markup.
  const main = await fetch(`${ION_ORIGIN}/main.cfm`, {
    redirect: "manual",
    headers: { Cookie: jarHeader(jar), "User-Agent": BROWSER_USER_AGENT, Accept: "text/html, */*" },
    signal: AbortSignal.timeout(45000),
  })
  absorbCookies(main, jar)
  if (main.status >= 300 && main.status < 400) {
    throw new Error(`ION bounced /main.cfm to ${main.headers.get("location")} — session not established`)
  }
  const body = await main.text()
  if (looksLikeLoginPage(body)) {
    throw new Error("ION login did not stick — /main.cfm served the login form")
  }
  const cfClientId = body.match(/_cf_clientid=([A-F0-9]{32})/i)?.[1]

  const now = Date.now()
  return {
    cookies: [...jar.entries()].map(([name, value]) => ({ name, value })),
    cfClientId,
    ionOrigin: ION_ORIGIN,
    capturedAt: now,
    expiresAt: now + DEFAULT_INACTIVITY_MS,
  }
}

export function cookieHeader(session: IonSession): string {
  return session.cookies.map((c) => `${c.name}=${c.value}`).join("; ")
}

export async function ionFetch(
  session: IonSession,
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers)
  headers.set("Cookie", cookieHeader(session))
  if (!headers.has("User-Agent")) headers.set("User-Agent", BROWSER_USER_AGENT)
  if (!headers.has("Accept")) headers.set("Accept", "text/html, */*")
  const res = await fetch(url, {
    ...init,
    headers,
    redirect: "manual",
    signal: init?.signal ?? AbortSignal.timeout(120000),
  })
  if (res.status >= 300 && res.status < 400) {
    const loc = res.headers.get("location") ?? ""
    if (loc.toLowerCase().includes("login")) {
      throw new IonSessionExpiredError(url, loc)
    }
  }
  if (res.ok) session.expiresAt = Date.now() + DEFAULT_INACTIVITY_MS
  return res
}

export async function ionFetchText(
  session: IonSession,
  url: string,
  init?: RequestInit,
): Promise<string> {
  const res = await ionFetch(session, url, init)
  if (!res.ok) {
    const preview = (await res.text()).slice(0, 300)
    throw new Error(`ionFetch ${url} -> HTTP ${res.status}: ${preview}`)
  }
  const body = await res.text()
  // ION's expired-session tell: a 200 whose body is only a logout redirect
  // stub. Trusting the status code here parses the stub as the page.
  if (body.length < 2000 && /security\/logout\.cfm/i.test(body)) {
    throw new IonSessionExpiredError(url, "security/logout.cfm stub body")
  }
  return body
}

export class IonSessionExpiredError extends Error {
  constructor(
    public readonly url: string,
    public readonly redirectedTo: string,
  ) {
    super(`ION session expired: ${url} -> ${redirectedTo}`)
    this.name = "IonSessionExpiredError"
  }
}

// #IPCLogin is the username field on /security/login.cfm and appears nowhere
// else — the whole test. Do NOT sniff for "password": authenticated pages
// carry it in their change-password menu and false-positive every session.
export function looksLikeLoginPage(body: string): boolean {
  return /IPCLogin/i.test(body)
}

export function isSessionFresh(session: IonSession, marginMs = 60000): boolean {
  return Date.now() < session.expiresAt - marginMs
}

// Runnable smoke test: login + fetch main.cfm, report authenticated-ness.
export async function main(ion: IonCredentials) {
  const session = await loginToIon(ion)
  const res = await ionFetch(session, `${session.ionOrigin}/main.cfm`)
  const body = await res.text()
  return {
    ok: res.ok && !looksLikeLoginPage(body),
    cookieCount: session.cookies.length,
    cfClientIdCaptured: Boolean(session.cfClientId),
    ionOrigin: session.ionOrigin,
  }
}

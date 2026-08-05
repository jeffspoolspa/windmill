//bun-extra-requirements:
//playwright@1.40.0

import { chromium } from "playwright@1.40.0"

export type IonResource = {
  username: string
  password: string
  /**
   * @deprecated Ignored since 2026-08. ION retired the Fluidra portal hop, so
   * login is a single ColdFusion form on ionpoolcare.com itself. Callers still
   * pass `$var:f/ION/LOGIN_URL`, which still points at the dead Fluidra page —
   * ION_LOGIN_URL below wins so no caller has to be re-deployed in lockstep.
   */
  loginUrl?: string
}

// The whole login: one form, one POST. No portal, no nav click, no redirect.
const ION_LOGIN_URL = "https://ionpoolcare.com/security/login.cfm"

export interface IonCookie {
  name: string
  value: string
  domain: string
  path: string
  expires?: number
  httpOnly?: boolean
  secure?: boolean
  sameSite?: "Strict" | "Lax" | "None"
}

export interface IonSession {
  cookies: IonCookie[]
  cfClientId: string | undefined
  ionOrigin: string
  capturedAt: number
  expiresAt: number
}

const DEFAULT_INACTIVITY_MS = 15 * 60 * 1000

const CHROMIUM_LAUNCH_ARGS = [
  "--no-sandbox",
  "--single-process",
  "--no-zygote",
  "--disable-setuid-sandbox",
  "--disable-dev-shm-usage",
  "--disable-gpu",
]

const BROWSER_USER_AGENT =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

// Prefer playwright 1.40's OWN chromium build (installed by the worker
// group's init script to a pinned path) — the 2026-07-06 incident: the
// unpinned distro chromium jumped to 150, which SIGTRAPs on any render
// under nsjail, killing every ION login. chromium-1091 = the build
// matching the playwright@1.40.0 pin; bump BOTH together or never.
const BUNDLED_CHROMIUM =
  "/usr/lib/ms-playwright/chromium-1091/chrome-linux/chrome"

function chromiumExecutable(): string {
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    const fs = require("fs")
    if (fs.existsSync(BUNDLED_CHROMIUM)) return BUNDLED_CHROMIUM
  } catch { /* fall through */ }
  return "/usr/bin/chromium"
}

export async function loginToIon(ion: IonResource): Promise<IonSession> {
  const browser = await chromium.launch({
    executablePath: chromiumExecutable(),
    args: CHROMIUM_LAUNCH_ARGS,
    timeout: 60000, // else a stuck launch hangs the whole job unbounded
  })
  try {
    const context = await browser.newContext({ userAgent: BROWSER_USER_AGENT })
    const page = await context.newPage()
    let cfClientId: string | undefined
    page.on("request", (req: any) => {
      if (cfClientId) return
      const m = req.url().match(/_cf_clientid=([A-F0-9]{32})/i)
      if (m) cfClientId = m[1]
    })
    await page.goto(ION_LOGIN_URL, { timeout: 30000 }) // default goto has no cap -> bound it
    await page.locator("#IPCLogin").fill(ion.username)
    await page.locator("#IPCPassword").fill(ion.password)
    await page.locator("#Submitted").click() // <input type=submit>, not a <button>
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    // main.cfm is what fires the ux_*.cfm AJAX carrying _cf_clientid, so go there
    // explicitly rather than trusting wherever the login POST happens to land.
    const ionOrigin = new URL(page.url()).origin
    await page.goto(`${ionOrigin}/main.cfm`, { timeout: 30000 })
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    // Single-stage login means a bad password ALSO leaves us on ionpoolcare.com,
    // so the origin alone no longer proves anything: ION bounces an unauthed
    // /main.cfm back to the login form. Assert the form is gone, or we'd cache
    // an anonymous session and every downstream fetch would 302 to login.
    if (!ionOrigin.includes("ionpoolcare.com")) {
      throw new Error(`ION login did not land on ionpoolcare.com: ${page.url()}`)
    }
    if ((await page.locator("#IPCLogin").count()) > 0) {
      throw new Error(`ION login rejected — still on the login form: ${page.url()}`)
    }
    const rawCookies = await context.cookies()
    const cookies: IonCookie[] = rawCookies.map((c: any) => ({
      name: c.name,
      value: c.value,
      domain: c.domain,
      path: c.path,
      expires: c.expires,
      httpOnly: c.httpOnly,
      secure: c.secure,
      sameSite: c.sameSite,
    }))
    const now = Date.now()
    return {
      cookies,
      cfClientId,
      ionOrigin,
      capturedAt: now,
      expiresAt: now + DEFAULT_INACTIVITY_MS,
    }
  } finally {
    await browser.close()
  }
}

export function cookieHeader(session: IonSession): string {
  return session.cookies
    .filter((c) => isCookieRelevantTo(c, session.ionOrigin))
    .map((c) => `${c.name}=${c.value}`)
    .join("; ")
}

function isCookieRelevantTo(cookie: IonCookie, origin: string): boolean {
  const host = new URL(origin).hostname
  const cookieDomain = cookie.domain.replace(/^\./, "")
  return host === cookieDomain || host.endsWith("." + cookieDomain)
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
  const res = await fetch(url, { ...init, headers, redirect: "manual" })
  if (res.status >= 300 && res.status < 400) {
    const loc = res.headers.get("location") ?? ""
    if (loc.toLowerCase().includes("login")) { // ION bounces unauthed -> /security/login.cfm
      throw new IonSessionExpiredError(url, loc)
    }
  }
  if (res.ok) {
    session.expiresAt = Date.now() + DEFAULT_INACTIVITY_MS
  }
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
  return res.text()
}

export class IonSessionExpiredError extends Error {
  constructor(
    public readonly url: string,
    public readonly redirectedTo: string,
  ) {
    super(`ION session expired: ${url} redirected to ${redirectedTo}`)
    this.name = "IonSessionExpiredError"
  }
}

// The one place that knows what ION's login page looks like. #IPCLogin is the
// username field on /security/login.cfm and appears nowhere else, so it is the
// whole test -- do NOT also sniff for "password", which authenticated pages
// carry in their change-password menu and which false-positives every session
// as dead. (Pre-2026-08 this looked for txtUserName, the retired Fluidra form.)
export function looksLikeLoginPage(body: string): boolean {
  return /IPCLogin/i.test(body)
}

export function isSessionFresh(session: IonSession, marginMs = 60000): boolean {
  return Date.now() < session.expiresAt - marginMs
}

export async function main(ion: IonResource) {
  console.log("Logging in via the ION login form...")
  const session = await loginToIon(ion)
  console.log(
    `  captured ${session.cookies.length} cookie(s); cfClientId: ${
      session.cfClientId ?? "(not captured)"
    }`,
  )
  for (const c of session.cookies) {
    console.log(
      `    ${c.domain}${c.path} ${c.name}=${c.value.slice(0, 12)}...`,
    )
  }
  const smokeUrl = `${session.ionOrigin}/main.cfm`
  console.log(`Smoke test: ionFetch ${smokeUrl}`)
  const res = await ionFetch(session, smokeUrl)
  const body = await res.text()
  const lower = body.toLowerCase()
  const looksAuthenticated =
    !looksLikeLoginPage(body) &&
    (lower.includes("menuitem0") ||
      lower.includes("ion pool care") ||
      lower.includes("coldfusionnavigate"))
  console.log(
    `  status=${res.status} body=${body.length} bytes -- looksAuthenticated=${looksAuthenticated}`,
  )
  return {
    ok: res.ok && looksAuthenticated,
    cookieCount: session.cookies.length,
    cfClientIdCaptured: Boolean(session.cfClientId),
    ionOrigin: session.ionOrigin,
    capturedAt: new Date(session.capturedAt).toISOString(),
    expiresAt: new Date(session.expiresAt).toISOString(),
    smokeTest: {
      url: smokeUrl,
      status: res.status,
      bodyLength: body.length,
      looksAuthenticated,
      bodyPreview: body.slice(0, 400),
    },
  }
}

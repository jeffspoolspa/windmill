//bun-extra-requirements:
//playwright@1.40.0

// The one shared ION login. Import THIS, not _lib/session (legacy name — its
// path substring-collides with _lib/session_cache in Windmill's bundler and
// corrupts any file importing both). Every batch job logs in fresh: ION keeps
// its cursor server-side, so a session shared across concurrent jobs reads
// the wrong customer with no error to show for it.

import { chromium } from "playwright@1.40.0"

export type IonCredentials = {
  username: string
  password: string
}

// The whole login: one form, one POST, on ionpoolcare.com itself.
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

// Prefer playwright 1.40's OWN chromium build (pinned by the worker group's
// init script) — the 2026-07-06 incident: unpinned distro chromium jumped to
// 150, which SIGTRAPs on render under nsjail, killing every ION login.
// chromium-1091 matches playwright@1.40.0; bump BOTH together or never.
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

export async function loginToIon(ion: IonCredentials): Promise<IonSession> {
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
    await page.goto(ION_LOGIN_URL, { timeout: 30000 })
    await page.locator("#IPCLogin").fill(ion.username)
    await page.locator("#IPCPassword").fill(ion.password)
    await page.locator("#Submitted").click() // <input type=submit>, not a <button>
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    // main.cfm fires the ux_*.cfm AJAX carrying _cf_clientid — go there
    // explicitly rather than trusting where the login POST lands.
    const ionOrigin = new URL(page.url()).origin
    await page.goto(`${ionOrigin}/main.cfm`, { timeout: 30000 })
    await page.waitForLoadState("networkidle", { timeout: 45000 })
    // Single-form login means a bad password ALSO lands on ionpoolcare.com,
    // so assert the login form is GONE — else we'd hand back an anonymous
    // session and every downstream fetch would 302 to login.
    if (!ionOrigin.includes("ionpoolcare.com")) {
      throw new Error(`ION login did not land on ionpoolcare.com: ${page.url()}`)
    }
    if ((await page.locator("#IPCLogin").count()) > 0) {
      throw new Error(`ION login rejected — still on the login form: ${page.url()}`)
    }
    const rawCookies = await context.cookies()
    const cookies: IonCookie[] = rawCookies.map((c: any) => ({
      name: c.name, value: c.value, domain: c.domain, path: c.path,
      expires: c.expires, httpOnly: c.httpOnly, secure: c.secure, sameSite: c.sameSite,
    }))
    const now = Date.now()
    return { cookies, cfClientId, ionOrigin, capturedAt: now, expiresAt: now + DEFAULT_INACTIVITY_MS }
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
  const ok = res.ok && !looksLikeLoginPage(body)
  return {
    ok,
    cookieCount: session.cookies.length,
    cfClientIdCaptured: Boolean(session.cfClientId),
    ionOrigin: session.ionOrigin,
  }
}

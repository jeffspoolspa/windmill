//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
// The ONE remaining ION Windmill dependency (ADR 012): mint/refresh session
// keys. Login needs chromium (two-stage Fluidra); everything AFTER login is
// plain HTTP the app-side Ion class does itself with these keys.
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
export async function main(force_refresh = false) {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s = await getOrRefreshSession(ion, { forceRefresh: force_refresh })
  return { ionOrigin: s.ionOrigin, cookieHeader: (s.cookies as {name:string;value:string}[]).map(c => `${c.name}=${c.value}`).join("; ") }
}

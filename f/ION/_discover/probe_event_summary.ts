//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession, ionFetchText } from "/f/ION/_lib/session_cache"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await getOrRefreshSession(ion)
  const html = await ionFetchText(s, `${s.ionOrigin}/reports/Schedule.cfm`)
  // the Update button's real mechanism lives in the page JS — capture every
  // url-ish string and every function wired to buttons/selects
  const urls = [...html.matchAll(/["']([^"']*\.cfm[^"']*)["']/g)].map((m) => m[1])
  const onclicks = [...html.matchAll(/onclick="([^"]{0,160})"/gi)].map((m) => m[1])
  const cfajax = [...html.matchAll(/ColdFusion\.\w+\([^)]{0,200}\)/g)].map((m) => m[0])
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]{0,1200}?)<\/script>/gi)]
    .map((m) => m[1].trim()).filter((t) => t && /rptStart|EventSummary|navigate|submit/i.test(t))
  return {
    urls: [...new Set(urls)].slice(0, 25),
    onclicks: onclicks.slice(0, 10),
    cfajax: cfajax.slice(0, 10),
    scripts: scripts.slice(0, 4),
  }
}

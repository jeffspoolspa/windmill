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
  const o = s.ionOrigin
  const url = `${o}/reports/serviceEvents.cfm?` + new URLSearchParams({
    office: "", tech: "", serviceType: "", Start: "2026-08-08", end: "2026-09-05", set: "1",
  }).toString()
  const body = await ionFetchText(s, url)
  const dates = [...new Set([...body.matchAll(/\d{2}\/\d{2}\/\d{4}/g)].map((m) => m[0]))].sort()
  return {
    length: body.length,
    distinctDates: dates.length,
    firstDate: dates[0] ?? null, lastDate: dates[dates.length - 1] ?? null,
    head: body.slice(0, 250).replace(/\s+/g, " "),
    hasCfClientId: Boolean((s as any).cfClientId),
  }
}

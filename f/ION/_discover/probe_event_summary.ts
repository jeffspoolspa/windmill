//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// TEMP PROBE (delete after): does the Event Summary report tolerate a plain
// session fetch (like CompletedLogDetail) or demand the browser dance (like
// transactions)? Also: discover the reports index links + the extract's
// column header row, so the ScheduleSweep parser is built against real
// bytes, not guesses. Read-only.

import "playwright@1.40.0"
import * as wmill from "windmill-client"
// ONE session module only — importing session AND session_cache in one file
// trips the bun prefix-collision bundle failure (documented; do not "fix" by
// re-adding the cache import here).
import { loginToIon, ionFetchText } from "/f/ION/_lib/session"

export async function main() {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const s = await loginToIon(ion)
  const o = s.ionOrigin
  const out: Record<string, unknown> = {}

  // 1. reports index: what report pages exist?
  for (const idx of ["/reports/", "/reports/index.cfm", "/reports/reports.cfm"]) {
    try {
      const html = await ionFetchText(s, `${o}${idx}`)
      const links = [...html.matchAll(/href="([^"]+\.cfm[^"]*)"[^>]*>([^<]{0,60})/gi)]
        .map((m) => `${m[1]} :: ${m[2].trim()}`)
        .filter((l) => /event|task|schedul|summar/i.test(l))
      if (links.length) { out[`index ${idx}`] = links.slice(0, 20); break }
      out[`index ${idx}`] = `no matching links (len ${html.length})`
    } catch (e) { out[`index ${idx}`] = String(e).slice(0, 120) }
  }

  // 2. candidate pickers + extracts, plain fetch
  const start = new Date().toISOString().slice(0, 10)
  const end = new Date(Date.now() + 28 * 86_400_000).toISOString().slice(0, 10)
  const pickers = ["eventSummary.cfm", "eventsRpt.cfm", "eventRpt.cfm", "scheduleRpt.cfm", "taskRpt.cfm", "activeTasks.cfm"]
  for (const p of pickers) {
    try {
      const url = `${o}/reports/${p}?` + new URLSearchParams({
        office: "", tech: "", Start: start, end, set: "1",
        _cf_containerId: "rptDetail", _cf_nodebug: "true", _cf_nocache: "true",
        _cf_clientid: s.cfClientId ?? "", _cf_rc: "1",
      }).toString()
      const html = await ionFetchText(s, url)
      out[`picker ${p}`] = `${html.length}b :: ${html.slice(0, 120).replace(/\s+/g, " ")}`
    } catch (e) { out[`picker ${p}`] = String(e).slice(0, 80) }
  }
  const extracts = ["EventSummary.cfm", "EventDetail.cfm", "ScheduledEvents.cfm", "ActiveTasks.cfm", "TaskList.cfm"]
  for (const x of extracts) {
    try {
      const body = await ionFetchText(s, `${o}/reports/_xls/${x}`)
      out[`xls ${x}`] = `${body.length}b :: ${body.slice(0, 300).replace(/\s+/g, " ")}`
    } catch (e) { out[`xls ${x}`] = String(e).slice(0, 80) }
  }
  return out
}

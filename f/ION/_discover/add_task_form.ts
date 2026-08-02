//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0

// DISCOVERY (read-only): how does ION create a recurring task?
//
// We have the EDIT path (addTask.cfm?EventID=<id> -> POSTs back to itself), but
// no CREATE path. Fetching addTask.cfm with an empty EventID returns a form
// carrying the day selects and NO inputs, so the create form is reached some
// other way -- almost certainly from the customer's task tab, which is also
// where ION puts the customer context the form needs.
//
// This script only READS: it pulls the customer tab and the task list, harvests
// every addTask/newTask style link it can see, then fetches each candidate and
// reports what fields the resulting form actually has. Nothing is POSTed.
// The point is to learn the URL shape + the required field set before any
// creating code exists (skill: discover write endpoints by watching ION).

import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { parse } from "node-html-parser"
import { getOrRefreshSession, ionFetchText } from "/f/ION/_lib/session_cache"

const norm = (s: string) => (s || "").replace(/\s+/g, " ").trim()

/** Every field a form would POST, plus which are selects and their options. */
function formShape(html: string, wantAction: string) {
  const root = parse(html)
  const forms = root.querySelectorAll("form")
  const picked =
    forms.find((f: any) => (f.getAttribute("action") || "").includes(wantAction)) ?? forms[0]
  if (!picked) return null
  const inputs: { name: string; type: string; value: string }[] = []
  for (const i of picked.querySelectorAll("input")) {
    const name = i.getAttribute("name")
    if (name) {
      inputs.push({
        name,
        type: (i.getAttribute("type") || "text").toLowerCase(),
        value: i.getAttribute("value") ?? "",
      })
    }
  }
  const selects: { name: string; optionCount: number; first: string[] }[] = []
  for (const s of picked.querySelectorAll("select")) {
    const name = s.getAttribute("name")
    if (!name) continue
    const opts = s.querySelectorAll("option")
    selects.push({
      name,
      optionCount: opts.length,
      first: opts.slice(0, 4).map((o: any) => `${o.getAttribute("value") ?? ""}=${norm(o.text)}`),
    })
  }
  const textareas = picked
    .querySelectorAll("textarea")
    .map((t: any) => t.getAttribute("name"))
    .filter(Boolean)
  return {
    action: picked.getAttribute("action") ?? "",
    method: (picked.getAttribute("method") || "get").toLowerCase(),
    inputCount: inputs.length,
    requiredish: inputs.filter((i) => i.type !== "hidden").map((i) => i.name),
    hidden: inputs.filter((i) => i.type === "hidden").map((i) => i.name),
    selects,
    textareas,
  }
}

export async function main(ionCustId: string | number, extraPaths: string[] = []) {
  const ion = {
    loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"),
    password: await wmill.getVariable("f/ION/PASSWORD"),
  }
  const session = await getOrRefreshSession(ion)
  const out: Record<string, unknown> = { ionCustId: String(ionCustId) }

  // Prime the customer, exactly as the read path does.
  const tabs = await ionFetchText(
    session,
    `${session.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`,
  )
  out.customerTabsBytes = tabs.length

  // Harvest every link/handler that smells like "add a task".
  const hrefs = new Set<string>()
  for (const m of tabs.matchAll(/(?:href|src)\s*=\s*["']([^"']*(?:addTask|newTask|addEvent)[^"']*)["']/gi)) {
    hrefs.add(m[1])
  }
  for (const m of tabs.matchAll(/(['"])((?:\/)?[^'"]*(?:addTask|newTask|addEvent)[^'"]*)\1/gi)) {
    if (m[2].includes(".cfm")) hrefs.add(m[2])
  }
  // ION drives navigation through JS, so also catch ColdFusionNavigate targets.
  for (const m of tabs.matchAll(/ColdFusionNavigate\(\s*['"]([^'"]+)['"]/gi)) hrefs.add(m[1])
  out.candidateLinks = [...hrefs].slice(0, 40)

  // Try each candidate plus anything the caller wants probed.
  const tries = [
    ...new Set([
      ...[...hrefs].filter((h) => h.includes("addTask")),
      `/tasks/addTask.cfm?customerid=${ionCustId}&isIFrame=1`,
      `/tasks/addTask.cfm?CustomerID=${ionCustId}&isIFrame=1`,
      `/tasks/addTask.cfm?EventID=0&CustomerID=${ionCustId}&isIFrame=1`,
      ...extraPaths,
    ]),
  ].slice(0, 12)

  const probes: Record<string, unknown> = {}
  for (const path of tries) {
    const url = path.startsWith("http")
      ? path
      : `${session.ionOrigin}${path.startsWith("/") ? "" : "/"}${path}`
    try {
      const html = await ionFetchText(session, url)
      probes[path] = { bytes: html.length, form: formShape(html, "addTask") }
    } catch (err) {
      probes[path] = { error: err instanceof Error ? err.message : String(err) }
    }
  }
  out.probes = probes
  return out
}

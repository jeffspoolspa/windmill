//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { parse } from "node-html-parser"

// PERMANENT AD-HOC ION RUNNER. Override main() body; run via runScriptByPath -> getJob.
// CURRENT: dump the full transactionRpt.cfm <form> -- every input/select/button with all attrs, plus
// the _CF_checkrpt() body and any submit trigger -- to reproduce the exact browser POST.
function cookieHeader(s: any) {
  const host = new URL(s.ionOrigin).hostname
  return s.cookies.filter((c: any) => { const d = c.domain.replace(/^\./, ""); return host === d || host.endsWith("." + d) })
    .map((c: any) => `${c.name}=${c.value}`).join("; ")
}

export async function main() {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"), username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const s: any = await getOrRefreshSession(ion)
  const o = s.ionOrigin
  const H = { Cookie: cookieHeader(s), "User-Agent": "Mozilla/5.0", Accept: "text/html, */*" }
  const body = await (await fetch(`${o}/reports/transactionRpt.cfm`, { headers: H, redirect: "manual" })).text()
  const root = parse(body)
  const form = root.querySelector("form#rpt") || root.querySelector("form")
  const controls = form ? form.querySelectorAll("input, select, button, a").map((el: any) => ({
    tag: el.tagName, name: el.getAttribute("name") || null, id: el.getAttribute("id") || null,
    type: el.getAttribute("type") || null, value: el.getAttribute("value") || null,
    onclick: (el.getAttribute("onclick") || "").slice(0, 120) || null, href: (el.getAttribute("href") || "").slice(0, 80) || null,
    text: (el.text || "").trim().slice(0, 40) || null,
  })).filter((c: any) => c.name || c.onclick || c.type === "submit" || /submit|view|report|xls|search/i.test(c.text || "")) : []
  const checkFn = (body.match(/function\s+_CF_checkrpt[\s\S]{0,600}?\}/) || [])[0] || null
  const submitCtx = (body.match(/[^;{}]*\.submit\(\)[^;]*/g) || []).slice(0, 6)
  return { controls, _CF_checkrpt: checkFn, submit_calls: submitCtx }
}

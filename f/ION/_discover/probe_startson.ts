//bun-extra-requirements:
//node-html-parser@6.1.13
//playwright@1.40.0
// READ-ONLY: what does the edit form's save actually send? Dump the StartsOn
// input, the submit machinery (onsubmit/onclick JS), and any input our
// serializer would drop (unchecked boxes, disabled, outside-form).
import "playwright@1.40.0"
import * as wmill from "windmill-client"
import { getOrRefreshSession, ionFetchText } from "/f/ION/_lib/session_cache"
export async function main(ionTaskId: string) {
  const ion = { loginUrl: await wmill.getVariable("f/ION/LOGIN_URL"),
    username: await wmill.getVariable("f/ION/USERNAME"), password: await wmill.getVariable("f/ION/PASSWORD") }
  const session = await getOrRefreshSession(ion)
  const html = await ionFetchText(session, `${session.ionOrigin}/tasks/addTask.cfm?EventID=${ionTaskId}&isIFrame=1`)
  const grab = (re: RegExp) => [...html.matchAll(re)].map(m => m[0].slice(0, 300))
  return {
    bytes: html.length,
    startsOnInput: grab(/<input[^>]*StartsOn[^>]*>/gi),
    submitMachinery: grab(/<(?:button|input|a)[^>]*(?:submit|save)[^>]*>/gi).slice(0, 6),
    onsubmit: grab(/onsubmit\s*=\s*"[^"]{0,250}/gi),
    saveJs: grab(/function\s+(?:save|submit)\w*\s*\([^)]*\)\s*\{[\s\S]{0,400}/gi).slice(0, 3),
    dateJs: grab(/StartsOn[\s\S]{0,200}/gi).slice(0, 8),
  }
}

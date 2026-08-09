/** READ-ONLY: what does the task list's Delete link actually call? */
import "playwright@1.40.0"
import { getOrRefreshSession, ionFetch, ionFetchText } from "/f/ION/_lib/session_cache"

type Resource = { ion: object }

export async function main(ion: Resource["ion"], ionCustId = "2581392") {
  const s = await getOrRefreshSession(ion)
  await ionFetchText(s, `${s.ionOrigin}/customers/customerTabs.cfm?customerid=${ionCustId}`)
  const res = await ionFetch(s, `${s.ionOrigin}/tasks/taskList.cfm`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", Referer: `${s.ionOrigin}/main.cfm` },
    body: "",
  })
  const html = await res.text()
  const hits: string[] = []
  const rx = /delete/gi
  let m: RegExpExecArray | null
  const seen = new Set<number>()
  while ((m = rx.exec(html)) && hits.length < 10) {
    const start = Math.max(0, m.index - 200)
    if ([...seen].some((x) => Math.abs(x - start) < 100)) continue
    seen.add(start)
    hits.push(html.slice(start, m.index + 260).replace(/\s+/g, " "))
  }
  return { length: html.length, hits }
}

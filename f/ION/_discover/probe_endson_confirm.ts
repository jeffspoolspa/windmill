/**
 * f/ION/_discover/probe_endson_confirm — READ ONLY: what does the addTask
 * form's JavaScript do when EndsOn is set? The boundary test showed the
 * EndsOn POST not persisting while the create did — hypothesis: the UI's
 * save path carries a confirm/extra field for end-dating (ION pops
 * "will delete scheduled visits"). Find it in the page source.
 */
import "playwright@1.40.0"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml } from "/f/ION/_lib/task_detail"

type Resource = { ion: object }

export async function main(ion: Resource["ion"], ionTaskId = "6040821", ionCustId = "2581392") {
  const s = await getOrRefreshSession(ion)
  const html = await fetchTaskFormHtml(s, ionTaskId, ionCustId)
  const hits: string[] = []
  const rx = /(confirm|deletevisit|delete_visit|removevisit|EndsOn|processTask|submitTask|LinkUsed)/gi
  let m: RegExpExecArray | null
  const seen = new Set<number>()
  while ((m = rx.exec(html)) && hits.length < 25) {
    const start = Math.max(0, m.index - 140)
    if ([...seen].some((x) => Math.abs(x - start) < 80)) continue
    seen.add(start)
    hits.push(html.slice(start, m.index + 220).replace(/\s+/g, " "))
  }
  return { length: html.length, hits }
}

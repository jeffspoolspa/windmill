/** READ-ONLY: the ServiceRepeat select's full option list (id -> label). */
import "playwright@1.40.0"
import { getOrRefreshSession } from "/f/ION/_lib/session_cache"
import { fetchTaskFormHtml } from "/f/ION/_lib/task_detail"

type Resource = { ion: object }

export async function main(ion: Resource["ion"], ionTaskId = "6040824", ionCustId = "2581392") {
  const s = await getOrRefreshSession(ion)
  const html = await fetchTaskFormHtml(s, ionTaskId, ionCustId)
  const block = html.match(/<select[^>]*name="ServiceRepeat"[\s\S]*?<\/select>/i)?.[0] ?? ""
  const options: Record<string, string> = {}
  const rx = /<option[^>]*value="([^"]*)"[^>]*>([^<]*)</gi
  let m: RegExpExecArray | null
  while ((m = rx.exec(block))) options[m[1]] = m[2].trim()
  return { options }
}

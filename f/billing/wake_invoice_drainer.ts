// f/billing/wake_invoice_drainer — the WAKE RELAY for the maintenance
// invoice machine. The machine's drain loop lives in the Next.js app
// (lib/billing); this script's only job is to knock on its door with the
// machine token. Drain-until-empty + the 10-min stale-claim release are the
// correctness guarantee; this wake is best-effort latency (ADR 008).
// Woken by: billing.wake_queue_worker via trg_wake_invoice_machine on
// billing.invoice_queue (statement-level, debounced, allowlisted).
import * as wmill from "windmill-client@^1"

export async function main() {
  const url = await wmill.getVariable("f/billing/INVOICE_DRAIN_URL")
  const token = await wmill.getVariable("f/billing/INVOICE_DRAIN_TOKEN")
  const res = await fetch(url, { method: "POST", headers: { "x-drain-token": token } })
  const body = await res.json().catch(() => null)
  if (!res.ok) throw new Error(`drain endpoint ${res.status}: ${JSON.stringify(body).slice(0, 300)}`)
  return body
}

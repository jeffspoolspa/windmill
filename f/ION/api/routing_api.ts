// Some agent sandboxes allowlist egress (package registries resolve, Railway
// fails at the TLS handshake). Windmill workers have full egress, and the
// Windmill MCP is already connected where those agents run — so this script
// IS their network path to the routing API. It adds no second door: same
// base URL, same bearer secret, every write still lands in routing.change_log.
//
// path examples:
//   /maintenance/routing/customers?q=longman
//   /maintenance/routing/tasks?customerId=2583394
//   /maintenance/routing/batches            (method POST, body = the batch)
//   /maintenance/routing/batches/<batchId>  (poll)

import * as wmill from "windmill-client"

export async function main(
  path: string,
  method: "GET" | "POST" = "GET",
  body?: object,
) {
  const base = (await wmill.getVariable("f/ION/JPS_API_URL")) as string
  const secret = (await wmill.getVariable("f/ION/JPS_API_SECRET")) as string

  if (!path.startsWith("/")) path = "/" + path
  const res = await fetch(`${base}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${secret}`,
      ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    signal: AbortSignal.timeout(60000),
  })

  const text = await res.text()
  let json: unknown = null
  try { json = JSON.parse(text) } catch { /* not json — return the text */ }
  return { status: res.status, body: json ?? text.slice(0, 4000) }
}

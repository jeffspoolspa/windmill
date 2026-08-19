// Windmill's one door INTO the .NET API: syncs end by notifying the app that
// fresh data landed (e.g. POST /maintenance/routing/customers/link-run).
// Business logic stays in the app; this lib only knows how to knock.
//
// Variables to create once:
//   f/ION/JPS_API_URL    -> https://jpsinternal-production.up.railway.app
//   f/ION/JPS_API_SECRET -> the shared API secret

import * as wmill from "windmill-client"

export async function notifyApi(
  path: string,
  init?: { method?: string; body?: unknown },
): Promise<{ status: number; body: string }> {
  const base = (await wmill.getVariable("f/ION/JPS_API_URL")) as string
  const secret = (await wmill.getVariable("f/ION/JPS_API_SECRET")) as string
  const res = await fetch(`${base}${path}`, {
    method: init?.method ?? "POST",
    headers: {
      Authorization: `Bearer ${secret}`,
      ...(init?.body !== undefined ? { "Content-Type": "application/json" } : {}),
    },
    ...(init?.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
  })
  return { status: res.status, body: (await res.text()).slice(0, 2000) }
}

// import-only lib; main is a reachability smoke test against /health
export async function main() {
  const base = (await wmill.getVariable("f/ION/JPS_API_URL")) as string
  const res = await fetch(`${base}/health`)
  return { ok: res.ok, status: res.status }
}

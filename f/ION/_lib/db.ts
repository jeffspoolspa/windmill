//bun-extra-requirements:
//postgres@3.4.5

// One place for the pg connection defaults. Callers: `const sql = await
// supabaseSql(); try { ... } finally { await sql.end() }`.
//
// ponytail: resource path defaults to the user-scoped u/carter/supabase the
// whole workspace uses today; when a folder-scoped f/ION/supabase resource
// exists, flip the default here and every consumer moves at once.

import postgres from "postgres@3.4.5"
import * as wmill from "windmill-client"

export async function supabaseSql(resourcePath = "u/carter/supabase") {
  const sb: any = await wmill.getResource(resourcePath)
  return postgres({
    host: sb.host,
    port: sb.port,
    database: sb.dbname,
    username: sb.user,
    password: sb.password,
    ssl: "require",
    max: 2,
    idle_timeout: 10,
    connect_timeout: 15,
  })
}

// import-only lib; main is a connectivity smoke test
export async function main() {
  const sql = await supabaseSql()
  try {
    const [row] = await sql`select 1 as ok`
    return { ok: row.ok === 1 }
  } finally {
    await sql.end()
  }
}

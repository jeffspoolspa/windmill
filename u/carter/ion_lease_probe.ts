//bun-extra-requirements:
//postgres@3.4.4
// Proves the Windmill side can take, hold and release the ION lease.
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"
export async function main() {
  const sb: any = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user,
                         password: sb.password, ssl: "require", max: 1 })
  try {
    const holder = "wm:probe"
    const [a] = await sql`SELECT * FROM ion.acquire_session_lease(${holder}, 'probe', 60)`
    const [blocked] = await sql`SELECT * FROM ion.acquire_session_lease('wm:other', 'probe2', 60)`
    const [renewed] = await sql`SELECT ion.renew_session_lease(${holder}, 60) AS ok`
    const [released] = await sql`SELECT ion.release_session_lease(${holder}) AS ok`
    const [after] = await sql`SELECT holder FROM ion.session_lease WHERE id='ion'`
    return { acquired: a.acquired, other_blocked: blocked.acquired === false,
             renewed: renewed.ok, released: released.ok, holder_after: after.holder }
  } finally { await sql.end() }
}

import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"
import { parse } from "node-html-parser"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  // set session filters like the sync does, then pull the data
  await ionFetchText(session,
    `${session.ionOrigin}/reports/customers.cfm?office=0&zone=0&tech=0&Start=&End=&typeID=0&set=1`)
  const body = await ionFetchText(session, `${session.ionOrigin}/reports/_xls/AllCustomers.cfm`)

  const tables = parse(body).querySelectorAll("table")
  let table = tables[0] ?? null
  for (const t of tables) {
    if (t.querySelectorAll("tr").length > (table?.querySelectorAll("tr").length ?? 0)) table = t
  }
  const grid = table!.querySelectorAll("tr").map(tr =>
    tr.querySelectorAll("td, th").map(td => td.text.trim()))

  const hist: Record<number, number> = {}
  for (const r of grid) hist[r.length] = (hist[r.length] ?? 0) + 1

  return {
    bodyLen: body.length,
    tableCount: tables.length,
    rowCount: grid.length,
    lengthHistogram: hist,
    firstSix: grid.slice(0, 6).map(r => ({ len: r.length, cells: r.slice(0, 8).map(c => c.slice(0, 25)) })),
    row4_tail: grid[4] ? grid[4].slice(-6) : null,
  }
}

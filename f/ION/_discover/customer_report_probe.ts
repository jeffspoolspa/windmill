import * as wmill from "windmill-client"
import { loginToIon, ionFetchText } from "/f/ION/_lib/ion_session"

export async function main() {
  const session = await loginToIon({
    username: (await wmill.getVariable("f/ION/USERNAME")) as string,
    password: (await wmill.getVariable("f/ION/PASSWORD")) as string,
  })
  const body = await ionFetchText(session,
    `${session.ionOrigin}/reports/customers.cfm?office=0&zone=0&tech=0&Start=&End=&typeID=0&set=1`)
  const links = [...body.matchAll(/<a([^>]*)>([\s\S]{0,120}?)<\/a>/gi)].map(m => ({
    href: (m[1].match(/href\s*=\s*["']([^"']+)["']/i)?.[1] ?? "").slice(0, 220),
    onclick: (m[1].match(/onclick\s*=\s*["']([^"']+)["']/i)?.[1] ?? "").slice(0, 220),
    text: m[2].replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim().slice(0, 60),
  }))
  return { count: links.length, links: links.slice(0, 25) }
}

//bun-extra-requirements:
//playwright@1.40.0

import "playwright@1.40.0"
import { loginToIon, type IonResource } from "/f/ION/_lib/session"

export async function main(ion: IonResource) {
  const session = await loginToIon(ion)
  return {
    cookies: session.cookies,
    cfClientId: session.cfClientId,
    ionOrigin: session.ionOrigin,
    capturedAt: session.capturedAt,
    capturedAtIso: new Date(session.capturedAt).toISOString(),
  }
}

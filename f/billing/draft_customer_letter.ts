//bun-extra-requirements:
//postgres@3.4.4

// CUSTOMER LETTER DRAFTER for the review workbench. Same evidence as
// analyze_maint_bill (invoice lines, visits, per-item vs usual vs peers,
// history) but the output is a CUSTOMER-FACING letter explaining an unusual
// bill — printed to PDF and sent with the invoice. The reviewer seeds it with
// their own context (the modal), reads the draft, and iterates: each call
// passes the prior thread back, so the model refines rather than restarts.
//
// Returns { letter (markdown), usage } and upserts the latest draft to
// billing.customer_letters keyed (customer_id, billing_month).
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"

const MODEL = "claude-sonnet-5"

const SYSTEM_PROMPT = `You draft letters to customers of Jeff's Pool & Spa Service, a family pool-maintenance company in coastal Georgia. A letter accompanies a monthly invoice that is higher or more unusual than the customer will expect, and explains it BEFORE they have to ask.

Voice: warm, plain, professional. A neighborly expert, not a corporation. First person plural ("we treated", "our technician"). No corporate filler, no apologizing for doing the work, no jargon — say "chlorine tablets", not SKU names.

Content rules:
- Open by naming the month and thanking them briefly. One sentence.
- Explain WHAT drove the bill using the service-log evidence: which visits, what was found (readings), what was added and why. Connect cause to dose ("after the storms in early July your chlorine was low, so...").
- Ground every number in the data provided. NEVER invent visits, amounts, or causes. If the reviewer's notes give the reason, trust them over your inference.
- If a discount or adjustment is mentioned in the reviewer's notes, state it plainly and warmly.
- Close with an invitation to call, and sign "The team at Jeff's Pool & Spa Service".
- UNDER 250 words. Markdown, no headings — just paragraphs.

Respond with ONLY the letter text.`

export async function main(
  customer_id: number,
  billing_month: string, // 'YYYY-MM-01'
  reviewer_context: string, // the modal's text — the human's framing, trusted
  thread: { role: "user" | "assistant"; text: string }[] = [], // prior iterations
) {
  const sb = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user,
                         password: sb.password, ssl: "require", max: 1 })
  try {
    const [cust, items, visits, findings] = await Promise.all([
      sql`SELECT display_name, company FROM public."Customers" WHERE id = ${customer_id}`,
      sql`
        SELECT bi.kind, bi.item_name, sum(bi.qty) AS qty, sum(bi.amount_cents)::bigint AS cents
        FROM billing.billable_items bi
        JOIN billing.billing_months bm ON bm.id = bi.billing_month_id
        WHERE bm.customer_id = ${customer_id} AND bm.month = ${billing_month}
        GROUP BY 1, 2 ORDER BY 1, 4 DESC`,
      sql`SELECT * FROM public.maint_billing_review_visits(${customer_id}, ${billing_month})`,
      sql`
        SELECT f.phase, f.rule, f.severity, f.message
        FROM billing.findings f
        JOIN billing.billing_months bm ON bm.id = f.billing_month_id
        WHERE bm.customer_id = ${customer_id} AND bm.month = ${billing_month}
          AND f.resolved_at IS NULL
        ORDER BY f.severity, f.rule`,
    ])
    if (!cust.length) throw new Error(`customer ${customer_id} not found`)

    const itemBlock = items.map((i: any) =>
      `${i.kind}: ${i.item_name ?? "service"} x${i.qty} = $${(Number(i.cents) / 100).toFixed(2)}`).join("\n")
    const visitBlock = visits.map((v: any) => {
      const reads = Object.entries(v.readings ?? {}).map(([k, x]) => `${k} ${x}`).join(", ")
      const chems = (v.chems ?? []).map((c: any) => `${c.qty} ${c.item}`).join(", ")
      return `${v.visit_date} — ${v.tech ?? "?"}: readings ${reads || "—"}; added ${chems || "nothing"}; notes: ${v.notes ?? "—"}`
    }).join("\n")
    const findingBlock = findings.map((f: any) => `[${f.phase}/${f.severity}] ${f.message}`).join("\n")

    const contextText = `CUSTOMER: ${cust[0].display_name}${cust[0].company ? ` (${cust[0].company})` : ""}
MONTH: ${billing_month.slice(0, 7)}

== THE BILL (our billable items, rolled up) ==
${itemBlock || "(none)"}

== THE MONTH'S VISITS (service logs) ==
${visitBlock || "(no visits)"}

== WHY THIS BILL WAS FLAGGED (internal — do not quote verbatim) ==
${findingBlock || "(no open findings)"}

== THE REVIEWER'S NOTES (trusted framing — follow these) ==
${reviewer_context || "(none given)"}`

    const apiKey = await wmill.getVariable("f/service_billing/ANTHROPIC_API_KEY")
    const messages: any[] = [
      { role: "user", content: [{ type: "text", text: contextText, cache_control: { type: "ephemeral" } }] },
      ...thread.map((t) => ({ role: t.role, content: t.text })),
    ]
    if (!thread.length) messages[0].content.push({ type: "text", text: "Draft the letter." })

    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: { "x-api-key": apiKey, "anthropic-version": "2023-06-01", "content-type": "application/json" },
      body: JSON.stringify({
        model: MODEL, max_tokens: 1200, thinking: { type: "disabled" },
        system: [{ type: "text", text: SYSTEM_PROMPT, cache_control: { type: "ephemeral" } }],
        messages,
      }),
    })
    if (!resp.ok) throw new Error(`anthropic ${resp.status}: ${(await resp.text()).slice(0, 300)}`)
    const data = await resp.json()
    const letter = (data.content ?? []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("").trim()
    if (!letter) throw new Error(`empty letter. stop=${data.stop_reason}`)

    await sql`
      INSERT INTO billing.customer_letters (customer_id, billing_month, letter, reviewer_context, model, usage)
      VALUES (${customer_id}, ${billing_month}, ${letter}, ${reviewer_context}, ${MODEL}, ${data.usage ?? null})
      ON CONFLICT (customer_id, billing_month)
      DO UPDATE SET letter = EXCLUDED.letter, reviewer_context = EXCLUDED.reviewer_context,
                    model = EXCLUDED.model, usage = EXCLUDED.usage, updated_at = now()`

    return { letter, usage: data.usage }
  } finally {
    await sql.end()
  }
}

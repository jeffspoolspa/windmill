//bun-extra-requirements:
//postgres@3.4.4

// AI BILL ANALYSIS for the review workbench. Gathers the customer-month's
// full evidence (invoice lines, visits w/ readings+chems+notes, hold reasons),
// billing history (visits + recorded chems + linked invoice totals per month,
// back to the April data floor), peer-group stats, and up to 12 visit-photo
// THUMBNAILS (public S3, small), then asks Claude for {driver, normal,
// recommend}. Result upserted to billing_audit.maint_bill_analyses (the
// workbench shows the latest on load) AND returned as the job result.
//
// PROMPT CACHING: the system prompt carries cache_control ephemeral — one
// cache write, then every analysis in the next 5 min (batch reviewing) reads
// it at 10% price. Per-customer context is inherently unique, so that's the
// cacheable prefix.
import * as wmill from "windmill-client"
import postgres from "postgres@3.4.4"

const MODEL = "claude-sonnet-5"
const MAX_PHOTOS = 12

const SYSTEM_PROMPT = `You are the billing reviewer's assistant at Jeff's Pool & Spa Service, a pool maintenance company. You are shown one customer's monthly maintenance bill that was HELD for human review, with the full service-log evidence behind it.

Your job: explain the bill so the reviewer can decide in seconds.

Focus ONLY on the consumable sales and what is driving the bill's size. Do NOT compare or reconcile invoice amounts against expected/recorded totals — the pipeline already verified the amounts before this bill reached review.

Analyze:
1. DRIVER — which consumable sales (items, quantities, dollars) and which visits produced them. If readings explain the doses (low FC -> shock, low salt reading -> salt bags), connect them.
2. NORMAL? — is this consumption normal for THIS customer given their history, and reasonable versus peers? Distinguish a one-off event (storm, green pool, equipment issue) from a rising trend. COMMERCIAL accounts get BULK chemical deliveries — one bulk line can swing a month; if it is near the account's usual delivery size, that IS the explanation and is fine to bill.
3. RECOMMEND — one concrete action: approve as billed; adjust a specific line (say which, by how much, why); or investigate something specific first. If photos contradict or confirm the notes, say so.

Ground every claim in the data you were given. Never invent visits, amounts, or history. If the evidence is thin, say what is missing rather than guessing.

Respond with ONLY a JSON object, no code fences:
{"driver": "...", "normal": "...", "recommend": "..."}
Each value: EXACTLY ONE short sentence, UNDER 25 WORDS, plain language, at most two dollar amounts — the single most decision-relevant fact only. No compound sentences chaining clauses with commas or dashes. The reviewer reads this in five seconds.`

function cents(n: number | null | undefined): string {
  return n == null ? "—" : `$${(Number(n) / 100).toFixed(2)}`
}

export async function main(
  customer_id: number,
  qbo_customer_id: string,
  billing_month: string, // 'YYYY-MM-01'
) {
  const sb = await wmill.getResource("u/carter/supabase")
  const sql = postgres({ host: sb.host, port: sb.port, database: sb.dbname, username: sb.user,
                         password: sb.password, ssl: "require", max: 1 })
  try {
    // ---- gather ----
    const [periods, visits, history, peer, flagItems] = await Promise.all([
      sql`
        SELECT tbp.id, tbp.expected_total_cents, tbp.ion_amt_cents, tbp.needs_review_reason,
               tbp.notes, tbp.qbo_invoice_id,
               i.doc_number, i.subtotal, i.total_amt, i.balance, i.email_status, i.line_items
        FROM billing_audit.task_billing_periods tbp
        LEFT JOIN billing.invoices i ON i.qbo_invoice_id = tbp.qbo_invoice_id
        WHERE tbp.qbo_customer_id = ${qbo_customer_id}
          AND tbp.billing_month = ${billing_month}`,
      sql`SELECT * FROM public.maint_billing_review_visits(${customer_id}, ${billing_month})`,
      sql`
        WITH m AS (
          SELECT date_trunc('month', v.visit_date)::date AS month, v.id
          FROM maintenance.visits v
          JOIN maintenance.tasks t ON t.id = v.task_id
          WHERE t.customer_id = ${customer_id}
            AND v.visit_date >= (${billing_month}::date - interval '12 months')
            AND v.visit_date < (${billing_month}::date + interval '1 month')
        ),
        vis AS (SELECT month, count(*) AS visits FROM m GROUP BY 1),
        chem AS (
          SELECT m.month, cu.item_name, sum(cu.quantity) AS qty,
                 sum(round(cu.quantity * coalesce(cc.unit_price_cents, 0)))::bigint AS cents
          FROM m
          JOIN maintenance.consumables_usage cu ON cu.visit_id = m.id
          LEFT JOIN maintenance.consumables cc ON cc.ion_item_id = cu.ion_item_id
          WHERE cu.item_name IS NOT NULL
          GROUP BY 1, 2
        )
        SELECT vis.month, vis.visits,
               coalesce(sum(chem.cents), 0)::bigint AS chem_cents,
               jsonb_object_agg(chem.item_name, chem.qty)
                 FILTER (WHERE chem.item_name IS NOT NULL) AS chem_qty
        FROM vis LEFT JOIN chem ON chem.month = vis.month
        GROUP BY 1, 2 ORDER BY 1`,
      sql`
        SELECT peer_group
        FROM billing_audit.customer_peer_group WHERE customer_id = ${customer_id}`,
      sql`
        SELECT item_name, month_qty, month_usd, usual_qty, usual_usd, peer_avg_usd
        FROM public.maint_billing_flag_items(${customer_id}, ${billing_month})`,
    ])
    if (!periods.length) throw new Error("no billing periods for this customer-month")

    const photos = await sql`
      SELECT vp.thumb_url, v.visit_date
      FROM maintenance.visit_photos vp
      JOIN maintenance.visits v ON v.ion_log_id = vp.ion_log_id
      JOIN maintenance.tasks t ON t.id = v.task_id
      WHERE t.customer_id = ${customer_id}
        AND date_trunc('month', v.visit_date)::date = ${billing_month}
      ORDER BY v.visit_date, vp.guid
      LIMIT ${MAX_PHOTOS}`

    // ---- build context text ----
    const invoiceBlock = periods.map((p: any) => {
      const lines = (p.line_items ?? [])
        .filter((li: any) => li.line_type === "item" || !li.line_type)
        .map((li: any) => `  - ${li.item_name ?? li.description ?? "?"}: qty ${li.qty ?? "—"} = $${li.amount ?? 0}`)
        .join("\n")
      return `Invoice #${p.doc_number ?? p.qbo_invoice_id ?? "unlinked"} — subtotal $${p.subtotal ?? "?"} (pre-tax), total $${p.total_amt ?? "?"} (incl. sales tax), balance $${p.balance ?? "?"}, ${p.email_status === "EmailSent" ? "sent" : "not sent"}
Hold reason: ${p.needs_review_reason ?? "—"}
Lines:\n${lines || "  (none cached)"}`
    }).join("\n\n")

    const visitBlock = visits.map((v: any) => {
      const reads = Object.entries(v.readings ?? {}).map(([k, x]) => `${k} ${x}`).join(", ")
      const chems = (v.chems ?? []).map((c: any) => `${c.qty} ${c.item} (${cents(c.cents)})`).join(", ")
      return `${v.visit_date} — ${v.tech ?? "?"}${v.minutes ? `, ${v.minutes} min` : ""}
  readings: ${reads || "—"}
  chems sold: ${chems || "none"}
  notes: ${v.notes ?? "—"}
  photos: ${(v.photos ?? []).length}`
    }).join("\n")

    const historyBlock = history.map((h: any) => {
      const qty = Object.entries(h.chem_qty ?? {}).map(([k, q]) => `${q} ${k}`).join(", ")
      return `${String(h.month).slice(0, 7)}: ${h.visits} visits, chems ${cents(h.chem_cents)}${qty ? ` (${qty})` : ""}`
    }).join("\n")

    const peerBlock = peer.length ? `Peer group: ${peer[0].peer_group}` : "Peer group: unknown"
    const itemsBlock = flagItems.map((it: any) =>
      `${it.item_name}: this month ${it.month_qty ?? 0} ($${it.month_usd ?? 0}) vs usual ${it.usual_qty ?? "—"} ($${it.usual_usd ?? "—"}), peer avg $${it.peer_avg_usd ?? "—"}`,
    ).join("\n")

    const contextText = `CUSTOMER-MONTH UNDER REVIEW: ${billing_month.slice(0, 7)}

== THE BILL ==
${invoiceBlock}

== THIS MONTH'S VISITS (our ingested service logs) ==
${visitBlock || "(no visits recorded)"}

== PER-ITEM: THIS MONTH vs THIS CUSTOMER'S USUAL vs PEERS ==
${itemsBlock || "(no consumable usage recorded)"}

== MONTHLY HISTORY (visits + recorded chem $, since data start) ==
${historyBlock || "(no history)"}

== PEER CONTEXT ==
${peerBlock}

${photos.length ? `The ${photos.length} attached images are this month's service-log photos (thumbnails), in visit-date order.` : "No photos this month."}`

    // ---- Claude call (system prompt cached) ----
    const apiKey = await wmill.getVariable("f/service_billing/ANTHROPIC_API_KEY")
    // thumbnails are ~8KB public S3 — fetch here and send base64 (image URL
    // blocks count against Anthropic's 10/min URL-fetch limit; base64 doesn't)
    const content: any[] = [{ type: "text", text: contextText }]
    for (const p of photos) {
      try {
        const r = await fetch(p.thumb_url)
        if (!r.ok) continue
        const b64 = Buffer.from(await r.arrayBuffer()).toString("base64")
        content.push({ type: "image", source: { type: "base64", media_type: "image/jpeg", data: b64 } })
      } catch { /* skip unfetchable thumb */ }
    }

    // cache breakpoint at the end of the full context (system alone is under
    // the 1024-token cache minimum): a re-run on the same customer within
    // 5 min — the analyze -> eyeball -> re-run loop — reads ~3k tokens at 10%
    if (content.length) content[content.length - 1].cache_control = { type: "ephemeral" }

    const resp = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "x-api-key": apiKey,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
      },
      body: JSON.stringify({
        model: MODEL,
        max_tokens: 1500,
        thinking: { type: "disabled" },
        system: [{ type: "text", text: SYSTEM_PROMPT, cache_control: { type: "ephemeral" } }],
        messages: [{ role: "user", content }],
      }),
    })
    if (!resp.ok) throw new Error(`anthropic ${resp.status}: ${(await resp.text()).slice(0, 300)}`)
    const data = await resp.json()
    const raw = (data.content ?? []).filter((b: any) => b.type === "text").map((b: any) => b.text).join("")
    const jsonText = raw.replace(/^```(json)?/m, "").replace(/```\s*$/m, "").trim()
    let result: any
    try {
      result = JSON.parse(jsonText)
    } catch {
      // tolerate prose-wrapped JSON
      const m = jsonText.match(/\{[\s\S]*\}/)
      if (!m) throw new Error(`model did not return JSON. stop=${data.stop_reason} resp=${JSON.stringify(data).slice(0, 600)}`)
      result = JSON.parse(m[0])
    }

    await sql`
      INSERT INTO billing_audit.maint_bill_analyses (customer_id, billing_month, result, model, usage)
      VALUES (${customer_id}, ${billing_month}, ${result}, ${MODEL}, ${data.usage ?? null})
      ON CONFLICT (customer_id, billing_month)
      DO UPDATE SET result = EXCLUDED.result, model = EXCLUDED.model,
                    usage = EXCLUDED.usage, created_at = now()`

    return { result, usage: data.usage, photos_sent: photos.length, visits: visits.length }
  } finally {
    await sql.end()
  }
}

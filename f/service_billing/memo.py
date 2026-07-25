# requirements:
# requests

# f/service_billing/memo — compose the customer-facing invoice memo.
#
# One concern: (work order, invoice) -> memo text + confidence. Deterministic
# rule first, LLM fallback, customer-specific guards (the O'Brien three-pool
# tag). No DB, no QBO — pure policy over inputs, so the whole module is
# checkable without a network (main() is the self-check; the LLM call is the
# only I/O and is injected around).

import calendar
import json
import random
import time
from datetime import date as _date

import requests
import wmill

OPENAI_KEY_VAR = "f/service_billing/OPENAI_API_KEY"
MEMO_CONFIDENCE_THRESHOLD = 0.85
MODEL = "gpt-4o-mini"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

MEMO_PROMPT = """You write a short customer-friendly memo for a pool service invoice.

Input: a JSON object with these fields:
- customer: customer name as it appears in QBO (may be "LAST, FIRST" or "First Last")
- type: work order type (e.g. "GENERAL SERVICE", "DELIVERY", "MAINTENANCE")
- description: what the technician was sent to do
- corrective: what the technician actually did - usually most reliable
- tech_instructions: notes from the office about the job - often clarifies ambiguity

Output: a JSON object with:
- memo: the memo text (NO WO number prefix - just the service description)
- confidence: 0.0 to 1.0 - how confident you are you understand what was done
- reasoning: 1 sentence

Style rules:
- Title Case, 2-7 words. NEVER more than 7 words.
- Equipment + Action format: "Autofill Valve Replacement", "Pool Pump Diagnosis"
- Use "&" to join two related items: "Salt Cell Cleaning & Filter Replacement"
- Use " - " for a qualifier: "Water Chemistry Service - Shock Treatment"
- Add context when meaningful: "Pre-Purchase Pool Inspection", "VSP Pump Error Diagnosis"
- Action words: Diagnosis, Replacement, Repair, Install, Delivery, Cleaning, Removal, Check, Clearing, Service
- No trailing punctuation
- Lean on `corrective` over `description`; use `tech_instructions` to disambiguate

**SPECIAL CUSTOMER RULE — ROBERT O'BRIEN (3-pool property)**

If the `customer` field contains BOTH "ROBERT" AND ("O'BRIEN" or "OBRIEN") —
case-insensitive, any order ("ROBERT O'BRIEN", "O'BRIEN, ROBERT",
"obrien robert" all qualify) — this rule applies:

1. The memo body describes the SERVICE only. Do NOT include the pool name
   in the body.
2. The memo MUST END with EXACTLY ONE of these tags (uppercase, in parens):
       (LAP POOL)
       (VOLLEYBALL)
       (SPA)
3. The tag is REQUIRED. The tag does NOT count toward the 7-word memo limit.
4. Pick the tag by scanning description, corrective, and tech_instructions
   for these keywords (case-insensitive):
       "lap pool"                               → (LAP POOL)
       "volleyball" / "vball" / "v-ball"        → (VOLLEYBALL)
       "spa"                                    → (SPA)
5. If you cannot find ANY pool keyword in the inputs, return confidence
   below 0.6 — DO NOT guess.

✅ CORRECT format (note: action first, tag at the end, ALL CAPS in parens):
   "Heat Exchanger Diagnosis (VOLLEYBALL)"
   "Spa Heater Repair (SPA)"
   "Booster Pump Replacement (LAP POOL)"
   "Salt Cell Cleaning & Filter Replacement (LAP POOL)"

❌ WRONG format (do NOT produce any of these):
   "Volleyball Pool Heat Exchanger Diagnosis"    ← pool name in body, no tag
   "Heat Exchanger Diagnosis (Volleyball Pool)"  ← wrong tag wording/case
   "Heat Exchanger Diagnosis VOLLEYBALL"         ← missing parens
   "Heat Exchanger Diagnosis"                    ← tag missing entirely
   "Volleyball Heat Exchanger Diagnosis (VOLLEYBALL)"  ← redundant pool reference

If you cannot figure out what was done, return confidence below 0.6.

Return ONLY valid JSON matching the schema."""

MEMO_EXAMPLES = [
    {"input": {"customer": "Smith, Jo", "type": "POOL INSPECTION", "description": "Pool inspection", "corrective": "Pool Inspection", "tech_instructions": ""},
     "output": {"memo": "Pool Inspection", "confidence": 0.97, "reasoning": "Straight pool inspection."}},
    {"input": {"customer": "Doe, John", "type": "GENERAL SERVICE", "description": "Valve was clogged with leaves and a wiffle ball.", "corrective": "Unclogged valve with leaves and wiffle ball.", "tech_instructions": ""},
     "output": {"memo": "Clogged Valve Clearing", "confidence": 0.96, "reasoning": "Valve was clogged and cleared."}},
    {"input": {"customer": "Williams, Bob", "type": "DIAGNOSIS", "description": "Electric heater making buzzing noise, then clicks off every ~3 min.", "corrective": "Found bad capacitor. Replaced. Unit started right up.", "tech_instructions": ""},
     "output": {"memo": "Electric Heater Diagnosis", "confidence": 0.95, "reasoning": "Electric heater diagnosed and repaired."}},
    {"input": {"customer": "Jones, Mary", "type": "GENERAL SERVICE", "description": "Remove Pool Cover", "corrective": "Removed cover.", "tech_instructions": ""},
     "output": {"memo": "Pool Cover Removal", "confidence": 0.98, "reasoning": "Pool cover removed."}},
    {"input": {"customer": "Brown, Alice", "type": "MAINTENANCE", "description": "Clean salt cell and replace filter.", "corrective": "Cleaned salt cell. Installed the filter no problem.", "tech_instructions": ""},
     "output": {"memo": "Salt Cell Cleaning & Filter Replacement", "confidence": 0.92, "reasoning": "Both services done."}},
    {"input": {"customer": "Davis, Chuck", "type": "DELIVERY", "description": "Deliver a 50lb bucket of chlorine tabs", "corrective": "Delivered", "tech_instructions": ""},
     "output": {"memo": "Chlorine Tab Delivery", "confidence": 0.98, "reasoning": "Standard chemical delivery."}},
    {"input": {"customer": "Wilson, Tom", "type": "GENERAL SERVICE", "description": "Spa Pump running loud. Motor + seal plate needed.", "corrective": "Installed new plate and motor.", "tech_instructions": ""},
     "output": {"memo": "Spa Pump Motor & Seal Plate Replacement", "confidence": 0.96, "reasoning": "Spa pump motor + seal plate replacement."}},
    {"input": {"customer": "Anderson, Pat", "type": "POOL INSPECTION", "description": "Pool Inspection. Due diligence 3/25 or 3/26. Potential buyer access.", "corrective": ".", "tech_instructions": ""},
     "output": {"memo": "Pre-Purchase Pool Inspection", "confidence": 0.93, "reasoning": "Pool inspection for potential buyer."}},
    {"input": {"customer": "Miller, Sam", "type": "DIAGNOSIS", "description": "Heater not firing", "corrective": "Replaced thermistor.", "tech_instructions": "Customer reports gas heater showing IF code intermittently"},
     "output": {"memo": "Gas Heater Diagnosis & Thermistor Replacement", "confidence": 0.93, "reasoning": "Tech instructions clarified gas heater + IF code; thermistor replaced."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Replace lid assembly on commercial chlorinator on the volleyball pool.", "corrective": "Installed new lid assembly. Tested and functional.", "tech_instructions": ""},
     "output": {"memo": "Commercial Chlorinator Lid Assembly Replacement (VOLLEYBALL)", "confidence": 0.95, "reasoning": "Volleyball pool chlorinator lid replaced."}},
    {"input": {"customer": "ROBERT O'BRIEN", "type": "DIAGNOSIS", "description": "Heater not firing", "corrective": "Replaced thermistor", "tech_instructions": "Spa heater issue - check IF code"},
     "output": {"memo": "Spa Heater Diagnosis & Thermistor Replacement (SPA)", "confidence": 0.93, "reasoning": "Tech instructions specified spa heater."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Replaced O-ring", "corrective": "O-ring replaced", "tech_instructions": ""},
     "output": {"memo": "O-Ring Replacement", "confidence": 0.45, "reasoning": "O'Brien WO but no pool name in any field - cannot determine which pool."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "DIAGNOSIS", "description": "Travis received call the Vball pool drained on Saturday. Need to diagnose. Customer filling, equipment off.", "corrective": "Diagnosed. Volley ball heat exchanger cracked draining pool. Shut off bypass to faulty heat pump.", "tech_instructions": ""},
     "output": {"memo": "Heat Exchanger Diagnosis (VOLLEYBALL)", "confidence": 0.95, "reasoning": "Vball/volleyball mentioned in both description and corrective — heat exchanger diagnosis on the volleyball pool."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Lap pool booster pump making grinding noise", "corrective": "Replaced booster pump motor and seal", "tech_instructions": ""},
     "output": {"memo": "Booster Pump Motor & Seal Replacement (LAP POOL)", "confidence": 0.96, "reasoning": "Lap pool explicitly named; booster pump motor + seal replacement."}},
    {"input": {"customer": "O'BRIEN, ROBERT", "type": "GENERAL SERVICE", "description": "Salt cell needs cleaning on volleyball", "corrective": "Cleaned salt cell, replaced o-rings", "tech_instructions": ""},
     "output": {"memo": "Salt Cell Cleaning & O-Ring Replacement (VOLLEYBALL)", "confidence": 0.94, "reasoning": "Volleyball pool salt cell cleaning + o-ring replacement."}},
]


def deterministic_memo(wo, invoice):
    desc = (wo.get("work_description") or "").lower()
    corr = (wo.get("corrective_action") or "").lower()
    instr = (wo.get("technician_instructions") or "").lower()
    haystack = f"{desc} {corr} {instr}"

    if "not on consumables" in haystack:
        date_val = (invoice or {}).get("txn_date") or wo.get("completed")
        if date_val:
            try:
                if isinstance(date_val, str):
                    d = _date.fromisoformat(date_val[:10])
                else:
                    d = date_val
                month_name = calendar.month_name[d.month]
                return {
                    "memo": f"{month_name} Supplies",
                    "confidence": 1.0,
                    "reasoning": "Monthly maintenance supplies (description marked 'not on consumables').",
                }
            except (ValueError, AttributeError):
                pass

    return None


_OBRIEN_POOL_TAGS = ("(LAP POOL)", "(VOLLEYBALL)", "(SPA)")


def _is_obrien_customer(name):
    if not name:
        return False
    n = name.lower().replace(",", " ")
    return ("robert" in n) and ("obrien" in n or "o'brien" in n)


def _has_obrien_pool_tag(memo):
    if not memo:
        return False
    upper = memo.upper()
    return any(tag in upper for tag in _OBRIEN_POOL_TAGS)


def generate_memo(wo, invoice, api_key=None, max_retries=3):
    api_key = api_key or wmill.get_variable(OPENAI_KEY_VAR)
    customer_name = (invoice or {}).get("customer_name") or wo.get("customer") or ""
    user_payload = {
        "customer": customer_name,
        "type": wo.get("type"),
        "description": wo.get("work_description") or "",
        "corrective": wo.get("corrective_action") or "",
        "tech_instructions": wo.get("technician_instructions") or "",
    }
    user_msg = json.dumps(user_payload)

    examples_text = "\n\nExamples:"
    for ex in MEMO_EXAMPLES:
        examples_text += f"\nInput: {json.dumps(ex['input'])}\nOutput: {json.dumps(ex['output'])}\n"

    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": MEMO_PROMPT + examples_text},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "memo_response",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "memo": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["memo", "confidence", "reasoning"],
                    "additionalProperties": False,
                },
            },
        },
        "max_tokens": 256,
        "temperature": 0.2,
    }

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(
                OPENAI_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=body, timeout=30,
            )
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = f"OpenAI network error: {e}"
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 30))
                continue
            break

        if resp.ok:
            try:
                content = resp.json()["choices"][0]["message"]["content"]
                usage = resp.json().get("usage") or {}
                cached = usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
                total_in = usage.get("prompt_tokens", 0)
                print(f"  openai usage: prompt={total_in} (cached={cached}), out={usage.get('completion_tokens', 0)}")
                return json.loads(content)
            except (KeyError, IndexError, json.JSONDecodeError) as e:
                return {"error": f"Failed to parse OpenAI response: {e}"}

        last_err = f"OpenAI API {resp.status_code}: {resp.text[:200]}"
        if resp.status_code == 429 and attempt < max_retries:
            retry_after = resp.headers.get("retry-after")
            if retry_after and retry_after.isdigit():
                base = min(int(retry_after), 30)
            else:
                base = min(2 ** attempt, 30)
            time.sleep(base + random.random() * base * 0.5)
            continue
        break

    return {"error": last_err}


def resolve_memo(wo, invoice, api_key=None):
    """The whole memo policy: locked memos are preserved; deterministic rule
    first, LLM after; O'Brien guard demotes untagged memos below threshold.
    Returns {"text", "locked", "ok", "source"} — text is None when nothing
    usable was produced (ok False -> enrichment fails -> needs_review)."""
    if bool(invoice.get("memo_locked")) and bool(invoice.get("memo")):
        return {"text": invoice["memo"], "locked": True, "ok": True,
                "source": "locked",
                "statement": invoice.get("statement_memo") or invoice["memo"]}

    memo_result = deterministic_memo(wo, invoice)
    source = "deterministic"
    if memo_result is None:
        memo_result = generate_memo(wo, invoice, api_key)
        source = "llm"

    if memo_result.get("memo") and "error" not in memo_result:
        customer = invoice.get("customer_name") or wo.get("customer") or ""
        if _is_obrien_customer(customer) and not _has_obrien_pool_tag(memo_result["memo"]):
            memo_result = {**memo_result,
                           "confidence": min(memo_result.get("confidence", 0), 0.4),
                           "reasoning": "O'Brien customer but memo lacks pool tag"}

    if "error" in memo_result:
        print(f"  memo failed: {memo_result['error'][:120]}")
        return {"text": None, "locked": False, "ok": False, "source": source}
    if memo_result.get("confidence", 0) < MEMO_CONFIDENCE_THRESHOLD:
        print(f"  memo low confidence: {memo_result.get('confidence', 0):.0%}")
        return {"text": memo_result.get("memo"), "locked": False, "ok": False,
                "source": source}
    return {"text": memo_result["memo"], "locked": True, "ok": True, "source": source}


# ── self-check: no network (generate_memo swapped) ──────────────────────────

def main():
    checks = []
    def ok(name, cond):
        checks.append((name, bool(cond)))

    d = deterministic_memo({"work_description": "not on consumables"},
                           {"txn_date": "2026-07-15"})
    ok("consumables rule -> month supplies", d and d["memo"] == "July Supplies"
       and d["confidence"] == 1.0)
    ok("no rule -> None (LLM's turn)",
       deterministic_memo({"work_description": "fix pump"}, {}) is None)
    ok("obrien detection both orders",
       _is_obrien_customer("O'BRIEN, ROBERT") and _is_obrien_customer("robert obrien")
       and not _is_obrien_customer("Smith, Bob"))
    ok("tag detection", _has_obrien_pool_tag("Heater Fix (SPA)")
       and not _has_obrien_pool_tag("Heater Fix"))

    r = resolve_memo({}, {"memo_locked": True, "memo": "Keep Me"}, api_key=None)
    ok("locked memo preserved", r["ok"] and r["locked"] and r["text"] == "Keep Me")

    g = globals()
    real = g["generate_memo"]
    try:
        g["generate_memo"] = lambda wo, inv, key: {"memo": "Pump Repair", "confidence": 0.95}
        r = resolve_memo({"work_description": "x"}, {"customer_name": "O'BRIEN, ROBERT"}, None)
        ok("obrien untagged memo demoted below threshold", not r["ok"])
        r = resolve_memo({"work_description": "x"}, {"customer_name": "Smith, Jo"}, None)
        ok("confident memo locks", r["ok"] and r["locked"] and r["text"] == "Pump Repair")
        g["generate_memo"] = lambda wo, inv, key: {"memo": "Guess", "confidence": 0.5}
        r = resolve_memo({"work_description": "x"}, {"customer_name": "Smith, Jo"}, None)
        ok("low confidence -> text kept, ok False (human reviews)",
           not r["ok"] and r["text"] == "Guess" and not r["locked"])
        g["generate_memo"] = lambda wo, inv, key: {"error": "OpenAI down"}
        r = resolve_memo({"work_description": "x"}, {"customer_name": "Smith, Jo"}, None)
        ok("LLM error -> no text, ok False", not r["ok"] and r["text"] is None)
    finally:
        g["generate_memo"] = real

    failed = [n for n, p in checks if not p]
    return {"ok": not failed, "passed": len(checks) - len(failed),
            "total": len(checks), "failed": failed}


if __name__ == "__main__":
    print(main())

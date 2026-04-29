# Classify work orders using Claude AI.
#
# Two-layer classification:
#   1. qbo_class: MECHANICAL (Service, Maintenance, Delivery, Renovation)
#      - MNT- prefix tech → Maintenance
#      - DELIVERY type or "deliver" keyword → Delivery
#      - renovation/replaster/retile keywords → Renovation
#      - else → Service
#   2. service_category: AI-classified for Service AND Maintenance classes
#      - Service: install | diagnosis | repair | misc (always populated)
#      - Maintenance: install | diagnosis | repair | routine (routine → null in DB)
#      - Delivery / Renovation: always null (no AI call)
#
# Mechanical fields (handled elsewhere, not here):
#   - Payment method: *bill* override → invoice, card on file → on_file, else → invoice
#   - Office: derived from the technician's branch
#   - Credit matching + subtotal check + final status: fn_validate_and_advance
#
# Schedule: runs after pull_qbo_invoices (every 4h) or on demand.

import wmill
import psycopg2
import psycopg2.extras
import json
import requests

SUPABASE_RESOURCE = "u/carter/supabase"
CONFIDENCE_THRESHOLD = 0.9
MODEL = "claude-sonnet-4-20250514"


def get_db_conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def determine_qbo_class(assigned_to: str, wo_type: str, description: str) -> str:
    """Mechanically derive qbo_class from technician + WO type + description.

    Returns one of: Service, Maintenance, Delivery, Renovation.
    """
    assigned = (assigned_to or "").upper()
    desc = (description or "").lower()
    wo = (wo_type or "").upper()

    # MNT- prefix technicians are always Maintenance class
    if assigned.startswith("MNT-"):
        return "Maintenance"
    # DELIVERY type, or short SVC- job explicitly labeled "deliver"
    if wo == "DELIVERY" or (assigned.startswith("SVC-") and "deliver" in desc and len(desc) < 80):
        return "Delivery"
    # Renovation keywords
    if "renovation" in desc or "replaster" in desc or "retile" in desc:
        return "Renovation"
    return "Service"


SERVICE_PROMPT = """You classify a pool-service work order performed by a SERVICE technician.

Return a JSON object:
- service_category: one of ["install", "diagnosis", "repair", "misc"]
- confidence: 0.0 to 1.0
- reasoning: brief explanation (1 sentence)

Categories:
- "install" = installing new or replacement equipment. Pump, motor, heater, salt cell, light, control board, filter, liner, basket. The WO describes putting equipment in.
- "diagnosis" = finding a problem. Leak detection, inspection, "check the X", troubleshooting. The WO describes assessment, not the fix.
- "repair" = fixing existing equipment. Patching, re-plumbing, re-sealing, gasket replacement, fixing a connection. The WO describes repairing something that stays in place.
- "misc" = anything else service-related. Pool school, green pool recovery, one-off odd jobs, quality control.

Read BOTH description and corrective action — the description says why the tech came out, the corrective action says what they actually did. The corrective action is usually more accurate for classification.

Return ONLY valid JSON, no markdown."""

SERVICE_EXAMPLES = [
    {
        "input": {"type": "GENERAL SERVICE", "description": "Customer said fountain pump is tripping the breaker.", "corrective": "Pump install on fountain.", "total": 1553.99},
        "output": {"service_category": "install", "confidence": 0.95, "reasoning": "Pump was replaced — an install even though triggered by a problem."}
    },
    {
        "input": {"type": "GENERAL SERVICE", "description": "Leak detection.", "corrective": "Found leak at skimmer throat. Needs re-plumb on next visit.", "total": 275.00},
        "output": {"service_category": "diagnosis", "confidence": 0.92, "reasoning": "Work was locating the leak — the repair is a follow-up WO."}
    },
    {
        "input": {"type": "GENERAL SERVICE", "description": "Replace pump lid gasket — leaking air.", "corrective": "Replaced gasket, primed pump, checked operation.", "total": 89.99},
        "output": {"service_category": "repair", "confidence": 0.95, "reasoning": "Gasket replacement is a repair of an existing pump."}
    },
    {
        "input": {"type": "GENERAL SERVICE", "description": "Pool school — teach new owner basic maintenance.", "corrective": "Completed pool school.", "total": 200.00},
        "output": {"service_category": "misc", "confidence": 0.98, "reasoning": "Pool school is an education visit."}
    },
    {
        "input": {"type": "GO BACK", "description": "Customer says pump making loud noise after recent replacement.", "corrective": "Fixed pump suction side with new manifold and three way.", "total": 99.99},
        "output": {"service_category": "repair", "confidence": 0.90, "reasoning": "Follow-up repair of manifold after prior install."}
    },
    {
        "input": {"type": "GENERAL SERVICE", "description": "Green pool recovery. Vinyl pool, 3-5 visits.", "corrective": "Started green pool treatment.", "total": 500.00},
        "output": {"service_category": "misc", "confidence": 0.90, "reasoning": "Green pool recovery is a one-off service project, not install/diagnosis/repair."}
    },
    {
        "input": {"type": "GENERAL SERVICE", "description": "Customer wants us to send a technician to turn the heater on.", "corrective": "Aaron left heater in standalone operation.", "total": 135.00},
        "output": {"service_category": "misc", "confidence": 0.82, "reasoning": "Operating the heater is not install/diagnosis/repair — a one-off service task."}
    },
]

MAINTENANCE_PROMPT = """You classify a pool-maintenance work order performed by a MAINTENANCE technician (MNT- prefix).

Maintenance techs mostly do routine visits, but occasionally do install or repair work during their visits. Read the description + corrective action to decide which.

Return a JSON object:
- service_category: one of ["install", "diagnosis", "repair", "routine"]
- confidence: 0.0 to 1.0
- reasoning: brief explanation (1 sentence)

Categories:
- "install" = installing new or replacement equipment during the visit. New pump, motor, heater, salt cell, light, control board, filter cartridge, basket.
- "diagnosis" = troubleshooting a specific problem during the visit (leak detection, finding a fault).
- "repair" = fixing existing equipment during the visit (leak repair, re-plumbing, gasket replacement).
- "routine" = standard maintenance. Follow-ups, salt cell CLEANS, filter CLEANS, chemical checks, standard service visits, water testing, brushing/vacuuming.

Important: routine is the DEFAULT for maintenance WOs. Only return install/diagnosis/repair if the description clearly describes that specific equipment work.
- A salt cell CLEAN is routine. Installing a NEW salt cell is install.
- A filter CLEAN is routine. Replacing a filter CARTRIDGE is install.
- Balancing chemicals is routine. Finding and fixing a leak is repair.

Return ONLY valid JSON, no markdown."""

MAINTENANCE_EXAMPLES = [
    {
        "input": {"type": "MAINTENANCE", "description": "Weekly maintenance visit.", "corrective": "Vacuumed pool, balanced chemicals, brushed walls.", "total": 125.00},
        "output": {"service_category": "routine", "confidence": 0.98, "reasoning": "Standard weekly maintenance."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Salt cell clean.", "corrective": "Cleaned salt cell, tested output.", "total": 85.00},
        "output": {"service_category": "routine", "confidence": 0.98, "reasoning": "Salt cell cleaning is routine maintenance, not replacement."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Filter cartridge clean.", "corrective": "Pulled cartridges, hosed them, reinstalled.", "total": 95.00},
        "output": {"service_category": "routine", "confidence": 0.97, "reasoning": "Filter cleaning is routine."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Follow up on last week's green pool treatment.", "corrective": "Pool clear, chemistry balanced.", "total": 75.00},
        "output": {"service_category": "routine", "confidence": 0.93, "reasoning": "Follow-up visit after prior treatment."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Dive pool and install pool light. Check filter media.", "corrective": "Found 3 leaks we repaired. Also got the light in correctly.", "total": 381.99},
        "output": {"service_category": "repair", "confidence": 0.72, "reasoning": "Primary work was leak repair during the visit, plus light install. Confidence lowered because both repair and install happened."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Replace pump during maintenance visit.", "corrective": "New 1hp pump installed.", "total": 650.00},
        "output": {"service_category": "install", "confidence": 0.96, "reasoning": "Pump installation done during a maintenance visit."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Pool losing water, check for leaks.", "corrective": "Located leak at return line, scheduling repair for next visit.", "total": 150.00},
        "output": {"service_category": "diagnosis", "confidence": 0.90, "reasoning": "Leak detection only — repair is a follow-up."}
    },
    {
        "input": {"type": "MAINTENANCE", "description": "Approved to replace pump lid gasket. Part will be delivered and installed on your normal maintenance.", "corrective": "Part replaced, pump checks ok.", "total": 48.99},
        "output": {"service_category": "repair", "confidence": 0.93, "reasoning": "Gasket replacement during maintenance visit is a repair."}
    },
]


def classify_service_category(wo: dict, api_key: str, qbo_class: str) -> dict:
    """Call Claude to classify service_category for Service or Maintenance class.

    Returns dict with service_category, confidence, reasoning, or error key.
    """
    if qbo_class == "Service":
        system_prompt = SERVICE_PROMPT
        examples = SERVICE_EXAMPLES
    elif qbo_class == "Maintenance":
        system_prompt = MAINTENANCE_PROMPT
        examples = MAINTENANCE_EXAMPLES
    else:
        return {"error": f"AI not supported for class {qbo_class}"}

    user_msg = json.dumps({
        "type": wo.get("type"),
        "description": wo.get("work_description") or "",
        "corrective": wo.get("corrective_action") or "",
        "total": float(wo.get("total_due") or 0),
    })

    examples_text = ""
    for ex in examples:
        examples_text += f"\nInput: {json.dumps(ex['input'])}\nOutput: {json.dumps(ex['output'])}\n"

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 256,
            "system": system_prompt + "\n\nExamples:" + examples_text,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=30,
    )

    if not resp.ok:
        return {"error": f"Claude API {resp.status_code}: {resp.text[:200]}"}

    text = resp.json()["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse: {text[:200]}"}


def main(limit: int = None, confidence_threshold: float = 0.9):
    """Classify awaiting_invoice WOs with cached invoices.

    qbo_class is mechanical. service_category is AI-classified for Service + Maintenance
    classes; Delivery and Renovation always have service_category = null.

    Args:
        limit: Max WOs to classify (for testing).
        confidence_threshold: Below this → needs_review.
    """
    print(f"=== classify_work_orders_ai started (threshold={confidence_threshold}) ===")

    api_key = wmill.get_variable("f/service_billing/ANTHROPIC_API_KEY")
    conn = get_db_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    sql = """
        SELECT w.wo_number, w.type, w.template, w.customer, w.total_due,
               w.work_description, w.corrective_action, w.assigned_to,
               w.invoice_number, w.office_name, w.sub_total,
               i.qbo_customer_id
        FROM public.work_orders w
        JOIN billing.invoices i ON i.doc_number = w.invoice_number
        WHERE w.billing_status = 'awaiting_invoice'
        ORDER BY w.completed DESC NULLS LAST
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    wos = [dict(r) for r in cur.fetchall()]
    cur.close()

    print(f"Found {len(wos)} WOs to classify")
    if not wos:
        conn.close()
        return {"status": "nothing_to_classify", "classified": 0}

    stats = {"auto_applied": 0, "needs_review": 0, "errors": 0, "by_class": {}}
    results = []

    for i, wo in enumerate(wos):
        wo_number = wo["wo_number"]

        # Resolve technician → employee (office comes via employee.branch)
        if wo.get("assigned_to"):
            cur2 = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur2.execute("""
                SELECT e.id
                FROM public.employees e
                WHERE %s = ANY(e.ion_username)
                LIMIT 1
            """, (wo["assigned_to"],))
            emp = cur2.fetchone()
            cur2.close()
            if emp:
                cur_u = conn.cursor()
                cur_u.execute(
                    "UPDATE public.work_orders SET employee_id = %s WHERE wo_number = %s",
                    (emp["id"], wo_number)
                )
                conn.commit()
                cur_u.close()

        # 1. Mechanical qbo_class
        qbo_class = determine_qbo_class(wo.get("assigned_to"), wo.get("type"), wo.get("work_description"))
        stats["by_class"][qbo_class] = stats["by_class"].get(qbo_class, 0) + 1

        # 2. service_category: AI for Service + Maintenance; null otherwise
        if qbo_class in ("Service", "Maintenance"):
            ai_result = classify_service_category(wo, api_key, qbo_class)

            if "error" in ai_result:
                print(f"  [{i+1}] {wo_number}: ERROR — {ai_result['error']}")
                stats["errors"] += 1
                results.append({"wo_number": wo_number, "qbo_class": qbo_class, "error": ai_result["error"]})
                continue

            confidence = ai_result.get("confidence", 0)
            ai_category = ai_result.get("service_category")
            reasoning = ai_result.get("reasoning", "")

            # Maintenance "routine" → null in DB (keep category null; qbo_class still Maintenance)
            if qbo_class == "Maintenance" and ai_category == "routine":
                service_category_db = None
            else:
                service_category_db = ai_category
        else:
            # Delivery / Renovation — service_category stays null, no AI call
            confidence = 1.0
            ai_category = None
            service_category_db = None
            reasoning = f"{qbo_class} class — service_category not applicable"

        cur3 = conn.cursor()

        if confidence >= confidence_threshold:
            # Auto-apply classification
            cur3.execute("""
                UPDATE public.work_orders
                SET qbo_class = %s,
                    service_category = %s,
                    classification_confidence = %s,
                    classification_model = %s,
                    last_classified_at = now()
                WHERE wo_number = %s
            """, (qbo_class, service_category_db, confidence, MODEL, wo_number))
            conn.commit()
            cur3.close()

            # Run validation (subtotal, credits, payment method, final status)
            cur4 = conn.cursor()
            cur4.execute("SELECT billing.fn_validate_and_advance(%s)", (wo_number,))
            validation = cur4.fetchone()[0]
            conn.commit()
            cur4.close()

            final_status = validation.get("status", "ready_to_process") if isinstance(validation, dict) else "ready_to_process"
            stats["auto_applied"] += 1
            display_cat = ai_category or "—"
            print(f"  [{i+1}] {wo_number}: {qbo_class}/{display_cat} ({confidence:.0%}) → {final_status}")
        else:
            # Low confidence → needs_review
            cur3.execute("""
                UPDATE public.work_orders
                SET qbo_class = %s,
                    service_category = %s,
                    classification_confidence = %s,
                    classification_model = %s,
                    billing_status = 'needs_review',
                    needs_review_reason = %s,
                    billing_status_set_at = now(),
                    last_classified_at = now()
                WHERE wo_number = %s
            """, (
                qbo_class, service_category_db, confidence, MODEL,
                f"Low confidence ({confidence:.0%}): {reasoning}",
                wo_number
            ))
            conn.commit()
            cur3.close()
            stats["needs_review"] += 1
            print(f"  [{i+1}] {wo_number}: {qbo_class}/{ai_category} ({confidence:.0%}) → needs_review: {reasoning}")

        results.append({
            "wo_number": wo_number,
            "qbo_class": qbo_class,
            "service_category": ai_category,
            "confidence": confidence,
            "reasoning": reasoning,
        })

        if (i + 1) % 20 == 0:
            print(f"  ... {i+1}/{len(wos)} classified")

    conn.close()
    print(f"=== done: {stats} ===")
    return {"status": "success", "total": len(wos), "stats": stats, "sample": results[:10]}

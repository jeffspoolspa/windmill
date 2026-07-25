# requirements:

"""
f/billing/_lib/calc — pure billing policy.

NO conn. NO I/O imports. Every function here is (inputs) -> output, so the
whole module is checkable without a database, a network, or a mock. If a rule
in here is ever consumed by a view or a gate, it moves to SQL instead
(docs: audit/parts/03-derivations-sql.md).

Import as:  from f.billing._lib.calc import derive_qbo_class, match_credits
"""


def derive_qbo_class(assigned_to, wo_type, description):
    """QBO class from the work order's own fields."""
    assigned = (assigned_to or "").upper()
    desc = (description or "").lower()
    wo = (wo_type or "").upper()
    if assigned.startswith("MNT-"):
        return "Maintenance"
    if wo == "DELIVERY" or (assigned.startswith("SVC-") and "deliver" in desc and len(desc) < 80):
        return "Delivery"
    if "renovation" in desc or "replaster" in desc or "retile" in desc:
        return "Renovation"
    return "Service"


def credit_match_reason(credit, wo_number, invoice_balance):
    """Why this credit belongs to this invoice — or None.

    Two rules, both deterministic:
      - the WO number appears in the credit's ref number or memo
      - the credit covers the invoice's balance exactly
    Anything else returns None: the credit is simply not applied, stays open,
    and surfaces as undecided in billing.invoice_ready until a human decides.
    """
    wo = (wo_number or "").lower()
    if wo:
        if wo in (credit.get("ref_num") or "").lower():
            return "wo_number_in_ref_num"
        if wo in (credit.get("memo") or "").lower():
            return "wo_number_in_memo"
    unapplied = float(credit.get("unapplied_amt") or 0)
    if unapplied > 0 and abs(unapplied - float(invoice_balance or 0)) < 0.01:
        return "full_cover"
    return None


def compose_memo(wo_number, memo_text, is_locked_source):
    """A locked memo is preserved verbatim (it was prefixed when first
    written); a new memo gets the WO prefix."""
    if is_locked_source:
        return memo_text
    return f"WO#{wo_number}: {memo_text}" if memo_text else None


def enrichment_updates(memo, qbo_class, class_id, wo_completed, current_txn_date):
    """The QBO sparse-PATCH body for an enrichment write. Pure shaping — the
    adapter performs it. TxnDate is only included when it actually differs."""
    updates = {"PrivateNote": memo, "CustomerMemo": {"value": memo}}
    if class_id:
        updates["ClassRef"] = {"value": class_id, "name": qbo_class}
    if wo_completed is not None:
        iso = (wo_completed.isoformat() if hasattr(wo_completed, "isoformat")
               else str(wo_completed))[:10]
        if current_txn_date != iso:
            updates["TxnDate"] = iso
    return updates


# ── self-check: pure, no DB, no network ─────────────────────────────────────

def main():
    checks = []
    def ok(n, c): checks.append((n, bool(c)))

    ok("MNT- tech -> Maintenance", derive_qbo_class("MNT-JOE", "GENERAL", "x") == "Maintenance")
    ok("DELIVERY type -> Delivery", derive_qbo_class("SVC-A", "DELIVERY", "x") == "Delivery")
    ok("renovation keyword", derive_qbo_class("SVC-A", "GENERAL", "replaster job") == "Renovation")
    ok("default Service", derive_qbo_class("SVC-A", "GENERAL", "fix pump") == "Service")

    reason = credit_match_reason
    ok("ref number match", reason({"ref_num": "5094004", "unapplied_amt": 150},
                                  "5094004", 500) == "wo_number_in_ref_num")
    ok("memo match", reason({"memo": "deposit wo 5094004", "unapplied_amt": 850},
                            "5094004", 500) == "wo_number_in_memo")
    ok("covers the balance exactly", reason({"unapplied_amt": 500}, "5094004", 500)
       == "full_cover")
    ok("half the balance is NOT a match (stays undecided)",
       reason({"unapplied_amt": 250}, "5094004", 500) is None)
    ok("unrelated credit is not a match",
       reason({"unapplied_amt": 77}, "5094004", 500) is None)
    ok("zero unapplied never matches on amount",
       reason({"unapplied_amt": 0}, "5094004", 0) is None)
    ok("no wo number falls back to amount only",
       reason({"memo": "x", "unapplied_amt": 500}, None, 500) == "full_cover")

    ok("new memo gets prefix", compose_memo("42", "Pump Repair", False) == "WO#42: Pump Repair")
    ok("locked memo verbatim", compose_memo("42", "WO#42: Old", True) == "WO#42: Old")
    ok("no text -> None", compose_memo("42", None, False) is None)

    import datetime as _dt
    u = enrichment_updates("m", "Service", "7", _dt.date(2026, 7, 20), "2026-07-01")
    ok("txn date included when different", u.get("TxnDate") == "2026-07-20")
    ok("class ref included", u["ClassRef"]["name"] == "Service")
    u2 = enrichment_updates("m", "Service", None, _dt.date(2026, 7, 20), "2026-07-20")
    ok("txn date omitted when same", "TxnDate" not in u2)
    ok("no class id -> no ClassRef", "ClassRef" not in u2)

    failed = [n for n, p in checks if not p]
    return {"ok": not failed, "passed": len(checks) - len(failed),
            "total": len(checks), "failed": failed}


if __name__ == "__main__":
    print(main())

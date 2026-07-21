import requests
import wmill
from collections import defaultdict

BASE = "https://quickbooks.api.intuit.com/v3/company"
MINOR = 75


def qbo_query(realm_id: str, access_token: str, query: str) -> dict:
    r = requests.get(
        f"{BASE}/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": query, "minorversion": MINOR},
    )
    if not r.ok:
        raise Exception(f"QBO query failed ({r.status_code}): {r.text[:500]}\nQuery: {query}")
    return r.json().get("QueryResponse", {})


def main(
    resource_path: str = "u/carter/quickbooks_api",
    start_date: str = "",
    end_date: str = "",
    max_detail_rows: int = 300,
):
    resource = wmill.get_resource(resource_path)

    # 1. Refresh tokens (rotating - MUST save new refresh token)
    tr = requests.post(
        "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer",
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "refresh_token", "refresh_token": resource["refresh_token"]},
        auth=(resource["client_id"], resource["client_secret"]),
    )
    if not tr.ok:
        raise Exception(f"Token refresh failed: {tr.status_code} - {tr.text}")
    tokens = tr.json()
    access_token = tokens["access_token"]

    # CRITICAL: persist the new refresh token immediately
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)

    realm = resource["realm_id"]

    # 2. Identify credit-card payment methods
    pm_resp = qbo_query(realm, access_token, "SELECT * FROM PaymentMethod MAXRESULTS 1000")
    methods = pm_resp.get("PaymentMethod", [])
    cc_methods = {
        m["Id"]: m.get("Name", "")
        for m in methods
        if m.get("Type", "").upper() == "CREDIT_CARD"
    }

    # 3. Paginate all Payments
    where = []
    if start_date:
        where.append(f"TxnDate >= '{start_date}'")
    if end_date:
        where.append(f"TxnDate <= '{end_date}'")
    where_clause = (" WHERE " + " AND ".join(where)) if where else ""

    payments = []
    start_pos = 1
    while True:
        q = f"SELECT * FROM Payment{where_clause} STARTPOSITION {start_pos} MAXRESULTS 1000"
        resp = qbo_query(realm, access_token, q)
        batch = resp.get("Payment", [])
        payments.extend(batch)
        if len(batch) < 1000:
            break
        start_pos += 1000

    # 4. Classify
    flagged = []
    cc_charged_count = 0
    non_cc_count = 0
    monthly = defaultdict(lambda: {"count": 0, "dollars": 0.0})

    for p in payments:
        pm_ref = (p.get("PaymentMethodRef") or {}).get("value")
        if pm_ref not in cc_methods:
            non_cc_count += 1
            continue

        cc_block = p.get("CreditCardPayment") or {}
        cc_resp = cc_block.get("CreditChargeResponse") or {}
        cc_trans_id = cc_resp.get("CCTransId")
        cc_status = cc_resp.get("Status")
        process_flag = p.get("ProcessPayment", False)

        actually_charged = bool(cc_trans_id) or process_flag is True
        if actually_charged:
            cc_charged_count += 1
            continue

        txn_date = p.get("TxnDate") or ""
        month = txn_date[:7] if txn_date else "unknown"
        monthly[month]["count"] += 1
        monthly[month]["dollars"] += float(p.get("TotalAmt") or 0)

        flagged.append({
            "payment_id": p.get("Id"),
            "txn_date": txn_date,
            "customer": (p.get("CustomerRef") or {}).get("name"),
            "amount": p.get("TotalAmt"),
            "payment_method": cc_methods.get(pm_ref),
            "ref_num": p.get("PaymentRefNum"),
            "unapplied": p.get("UnappliedAmt"),
            "deposit_to": (p.get("DepositToAccountRef") or {}).get("name"),
            "has_cc_block": bool(cc_block),
            "has_cctransid": bool(cc_trans_id),
            "cc_status": cc_status,
            "process_payment": process_flag,
        })

    flagged.sort(key=lambda r: r["txn_date"] or "", reverse=True)
    monthly_rollup = {
        k: {"count": v["count"], "dollars": round(v["dollars"], 2)}
        for k, v in sorted(monthly.items(), reverse=True)
    }

    return {
        "summary": {
            "total_payments_scanned": len(payments),
            "cc_method_ids": cc_methods,
            "cc_marked_and_actually_charged": cc_charged_count,
            "cc_marked_but_NOT_charged": len(flagged),
            "non_cc_payments": non_cc_count,
            "total_flagged_dollars": round(sum(float(r["amount"] or 0) for r in flagged), 2),
        },
        "monthly_rollup": monthly_rollup,
        "detail_truncated": len(flagged) > max_detail_rows,
        "flagged": flagged[:max_detail_rows],
    }

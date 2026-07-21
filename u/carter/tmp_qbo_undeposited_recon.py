import requests
import wmill
from collections import defaultdict

BASE = "https://quickbooks.api.intuit.com/v3/company"
MINOR = 75


def qbo_query(realm_id, access_token, query):
    r = requests.get(
        f"{BASE}/{realm_id}/query",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        params={"query": query, "minorversion": MINOR},
    )
    if not r.ok:
        raise Exception(f"QBO query failed ({r.status_code}): {r.text[:400]}\nQuery: {query}")
    return r.json().get("QueryResponse", {})


def paginate(realm, token, entity):
    out = []
    start = 1
    while True:
        resp = qbo_query(realm, token, f"SELECT * FROM {entity} STARTPOSITION {start} MAXRESULTS 1000")
        batch = resp.get(entity, [])
        out.extend(batch)
        if len(batch) < 1000:
            break
        start += 1000
    return out


def linked_types(txn):
    types = [lt.get("TxnType") for lt in (txn.get("LinkedTxn") or [])]
    for line in txn.get("Line", []):
        types += [lt.get("TxnType") for lt in line.get("LinkedTxn", [])]
    return set(types)


def main(resource_path: str = "u/carter/quickbooks_api", max_detail_rows: int = 120):
    resource = wmill.get_resource(resource_path)

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
    resource["refresh_token"] = tokens["refresh_token"]
    wmill.set_resource(resource_path, resource)
    realm = resource["realm_id"]

    # 1. Find the Undeposited Funds account and its live balance
    accounts = qbo_query(realm, access_token, "SELECT * FROM Account MAXRESULTS 1000").get("Account", [])
    uf = [a for a in accounts if a.get("AccountSubType") == "UndepositedFunds"]
    if not uf:
        raise Exception("No UndepositedFunds account found")
    uf_id = uf[0]["Id"]
    uf_balance = float(uf[0].get("CurrentBalance") or 0)

    def goes_to_uf(txn):
        ref = (txn.get("DepositToAccountRef") or {}).get("value")
        return ref is None or ref == uf_id

    undeposited = []
    totals = defaultdict(float)
    counts = defaultdict(int)
    yearly = defaultdict(lambda: {"count": 0, "dollars": 0.0})

    # 2. Payments: to UF, no Deposit link
    for p in paginate(realm, access_token, "Payment"):
        if not goes_to_uf(p):
            continue
        if "Deposit" in linked_types(p):
            continue
        amt = float(p.get("TotalAmt") or 0)
        totals["Payment"] += amt
        counts["Payment"] += 1
        if amt != 0:
            yr = (p.get("TxnDate") or "unknown")[:4]
            yearly[yr]["count"] += 1
            yearly[yr]["dollars"] += amt
            undeposited.append({
                "type": "Payment",
                "id": p.get("Id"),
                "txn_date": p.get("TxnDate"),
                "customer": (p.get("CustomerRef") or {}).get("name"),
                "amount": amt,
                "pm": (p.get("PaymentMethodRef") or {}).get("name"),
            })

    # 3. SalesReceipts: to UF, no Deposit link
    for s in paginate(realm, access_token, "SalesReceipt"):
        if not goes_to_uf(s):
            continue
        if "Deposit" in linked_types(s):
            continue
        amt = float(s.get("TotalAmt") or 0)
        totals["SalesReceipt"] += amt
        counts["SalesReceipt"] += 1
        if amt != 0:
            yr = (s.get("TxnDate") or "unknown")[:4]
            yearly[yr]["count"] += 1
            yearly[yr]["dollars"] += amt
            undeposited.append({
                "type": "SalesReceipt",
                "id": s.get("Id"),
                "txn_date": s.get("TxnDate"),
                "customer": (s.get("CustomerRef") or {}).get("name"),
                "amount": amt,
                "pm": (s.get("PaymentMethodRef") or {}).get("name"),
            })

    # 4. RefundReceipts drawn FROM UF reduce the balance
    for rr in paginate(realm, access_token, "RefundReceipt"):
        ref = (rr.get("DepositToAccountRef") or {}).get("value")
        if ref != uf_id:
            continue
        amt = float(rr.get("TotalAmt") or 0)
        totals["RefundReceipt"] -= amt
        counts["RefundReceipt"] += 1
        if amt != 0:
            undeposited.append({
                "type": "RefundReceipt (negative)",
                "id": rr.get("Id"),
                "txn_date": rr.get("TxnDate"),
                "customer": (rr.get("CustomerRef") or {}).get("name"),
                "amount": -amt,
                "pm": (rr.get("PaymentMethodRef") or {}).get("name"),
            })

    computed = round(sum(totals.values()), 2)
    undeposited.sort(key=lambda r: r["txn_date"] or "", reverse=True)

    return {
        "uf_account": {"id": uf_id, "name": uf[0].get("Name"), "current_balance": uf_balance},
        "computed_undeposited_total": computed,
        "delta_vs_uf_balance": round(uf_balance - computed, 2),
        "ties_out": abs(uf_balance - computed) < 0.01,
        "by_type": {
            k: {"count": counts[k], "dollars": round(v, 2)} for k, v in totals.items()
        },
        "by_year_nonzero": {
            k: {"count": v["count"], "dollars": round(v["dollars"], 2)}
            for k, v in sorted(yearly.items(), reverse=True)
        },
        "detail_truncated": len(undeposited) > max_detail_rows,
        "undeposited_items_total_count": len(undeposited),
        "items": undeposited[:max_detail_rows],
    }

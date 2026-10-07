import json
import urllib.parse
import urllib.request

import wmill

QBO = "https://quickbooks.api.intuit.com/v3/company"
ITEM_NAME = "Slide Attendant Labor"


def _token():
    r = wmill.run_script_by_path("f/qbo/get_access_token", args={})
    return r["access_token"], r["realm_id"]


def _req(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise Exception(f"QBO {method} {e.code}: {e.read().decode(errors='replace')[:2000]}")


def _query(token, realm, q):
    url = f"{QBO}/{realm}/query?" + urllib.parse.urlencode({"query": q, "minorversion": "73"})
    return _req("GET", url, token).get("QueryResponse", {})


def main(
    customer_id: str = "8377",
    txn_date: str = "2026-09-30",
    period_label: str = "September 2026",
    lines: list = None,
    dry_run: bool = True,
):
    """
    lines: [{"desc": str, "qty": float, "rate": float}, ...]
    """
    if not lines:
        raise Exception("lines is required")

    token, realm = _token()

    # 1. Item lookup by exact name (abort if missing; never create items here)
    items = _query(token, realm, f"SELECT Id, Name, Type FROM Item WHERE Name = '{ITEM_NAME}'").get("Item", [])
    if len(items) != 1:
        raise Exception(f"Expected exactly one item named {ITEM_NAME!r}, found {len(items)}: {items}")
    item = items[0]

    # 2. Prior slide invoices for this customer: mirror class, detect duplicate period
    prior = _query(
        token, realm,
        f"SELECT * FROM Invoice WHERE CustomerRef = '{customer_id}' ORDERBY TxnDate DESC MAXRESULTS 50",
    ).get("Invoice", [])
    slide_prior = []
    for inv in prior:
        descs = []
        hit = False
        for ln in inv.get("Line", []):
            d = ln.get("SalesItemLineDetail", {})
            if d.get("ItemRef", {}).get("value") == item["Id"]:
                hit = True
            if ln.get("Description"):
                descs.append(ln["Description"])
        if hit:
            slide_prior.append({
                "Id": inv["Id"], "DocNumber": inv.get("DocNumber"), "TxnDate": inv.get("TxnDate"),
                "TotalAmt": inv.get("TotalAmt"), "ClassRef": inv.get("ClassRef"),
                "BillEmail": inv.get("BillEmail"), "SalesTermRef": inv.get("SalesTermRef"),
                "CustomerMemo": inv.get("CustomerMemo"), "descs": descs,
            })

    dup = [p for p in slide_prior if any(period_label in d for d in p["descs"])]
    if dup:
        raise Exception(f"An invoice for {period_label} already exists for customer {customer_id}: {dup}")

    template = slide_prior[0] if slide_prior else None

    # 3. Build payload
    qbo_lines = []
    for i, ln in enumerate(lines, start=1):
        qty = round(float(ln["qty"]), 2)
        rate = round(float(ln["rate"]), 2)
        amt = round(qty * rate + 1e-9, 2)
        qbo_lines.append({
            "LineNum": i,
            "DetailType": "SalesItemLineDetail",
            "Description": ln["desc"],
            "Amount": amt,
            "SalesItemLineDetail": {
                "ItemRef": {"value": item["Id"], "name": item["Name"]},
                "Qty": qty,
                "UnitPrice": rate,
                **({"ClassRef": template["ClassRef"]} if template and template.get("ClassRef") else {}),
            },
        })
    payload = {
        "CustomerRef": {"value": customer_id},
        "TxnDate": txn_date,
        "Line": qbo_lines,
    }
    if template:
        if template.get("ClassRef"):
            payload["ClassRef"] = template["ClassRef"]
        if template.get("BillEmail"):
            payload["BillEmail"] = template["BillEmail"]
        if template.get("SalesTermRef"):
            payload["SalesTermRef"] = template["SalesTermRef"]

    computed_total = round(sum(l["Amount"] for l in qbo_lines), 2)
    summary = {
        "dry_run": dry_run,
        "item": item,
        "template_invoice": template,
        "prior_slide_invoices": [(p["DocNumber"], p["TxnDate"], p["TotalAmt"]) for p in slide_prior],
        "payload": payload,
        "computed_total": computed_total,
    }
    if dry_run:
        return summary

    # 4. Write
    created = _req("POST", f"{QBO}/{realm}/invoice?minorversion=73", token, payload)["Invoice"]
    summary["created"] = {
        "Id": created["Id"], "DocNumber": created.get("DocNumber"),
        "TxnDate": created.get("TxnDate"), "DueDate": created.get("DueDate"),
        "TotalAmt": created.get("TotalAmt"), "Balance": created.get("Balance"),
        "EmailStatus": created.get("EmailStatus"),
    }
    return summary

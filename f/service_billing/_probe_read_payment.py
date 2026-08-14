# f/service_billing/_probe_read_payment
#
# TEMP read-only diagnostic: GET a QBO Payment and return its raw payload.
#
# Module: docs/modules/service/billing.md
# Status: [draft]
# Concurrency key: qbo_api
#
# Triggered by:
#   - manual only
#
# Tables touched: (none)
#
# External APIs:
#   - QBO: GET /payment/{id}
#
# Why this exists:
#   record_cc_payment's create response came back with no CreditCardPayment
#   block, so cc_trans_id_ok was false. That is ambiguous: QBO may simply not
#   echo the block on create, or it may have dropped it. Reading the stored
#   Payment back is the only way to tell. Delete once the answer is known.

import requests
import wmill

TOKEN_PROVIDER = "f/qbo/get_access_token"


def main(qbo_payment_id: str, entity: str = "payment") -> dict:
    token = wmill.run_script_by_path(TOKEN_PROVIDER, args={})
    access_token, realm_id = token["access_token"], token["realm_id"]
    resp = requests.get(
        f"https://quickbooks.api.intuit.com/v3/company/{realm_id}/{entity}/{qbo_payment_id}",
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
        timeout=60,
    )
    if not resp.ok:
        raise Exception(f"Read failed: {resp.status_code} - {resp.text}")
    body = resp.json()
    return body.get(entity.capitalize()) or body

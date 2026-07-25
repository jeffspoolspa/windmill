# requirements:
# psycopg2-binary
# requests
# wmill

"""
f/billing/_lib/clients — our API to every external system.

Scripts never speak HTTP to QBO, never hold a token, never shape a QBO
payload. They construct this once and call methods.

What the client owns (so no caller has to):
  - the token lifecycle (the rotating refresh token — 27 copies of this
    existed across the repo before this class)
  - the ADR-008 rate governor: every call claims from the token bucket
  - transport, retries, SyncToken CAS
  - read = echo (a read converges the cache when the client holds a conn)
  - write = echo + emit (a write echoes its response and emits its fact)

This first cut is a FACADE over the proven f/billing/_lib/qbo primitives:
the contracts (idempotency, echo, emit) already live there and are money-
tested, so the class changes the call shape without changing behavior. The
primitives collapse into the methods later, one at a time.

One home for all adapter classes (QboClient now; IonClient,
AirtableClient, LlmClient to follow). Deliberately NOT named
`qbo_client`: a module whose path is a prefix-extension of another
(`_lib/qbo` + `_lib/qbo_client`) triggers the Windmill resolver
collision that mangles module names at RUN time — the documented
`session` / `session_cache` incident.

Import as:  from f.billing._lib.clients import QboClient
"""

from f.billing._lib import qbo as _qbo


class QboClient:
    """One instance per job. Pass `conn` and every read converges the cache
    and every write echoes + emits."""

    def __init__(self, conn=None, *, actor="auto"):
        self.conn = conn
        self.actor = actor
        if conn is not None:
            _qbo.set_rate_limiter(conn)          # ADR 008 §4: every call claims
        self.access_token, self.realm_id = _qbo.refresh_qbo_token()

    # ── invoice ────────────────────────────────────────────────────────────

    def get_invoice(self, invoice_id):
        """Full invoice read. Converges the cache (read = echo).
        Returns the QBO Invoice dict or None."""
        inv, _err = _qbo.fetch_qbo_invoice(invoice_id, self.access_token,
                                           self.realm_id, conn=self.conn)
        return inv

    def invoice_details(self, invoice_id):
        """{balance, email_status} or None. A None here means the leader could
        not be read — money callers MUST halt, never fall back to cache."""
        return _qbo.get_qbo_invoice_details(invoice_id, self.realm_id,
                                            self.access_token, conn=self.conn)

    def update_invoice(self, invoice_id, updates, *, intent_ref):
        """Sparse PATCH with SyncToken CAS. Echoes the response into the cache
        and emits invoice_edited with a before/after diff. Returns
        {success, invoice} | {success: False, error}."""
        return _qbo.update_invoice_sparse(invoice_id, updates, self.access_token,
                                          self.realm_id, conn=self.conn,
                                          intent_ref=intent_ref, actor=self.actor)

    def send_invoice(self, invoice_id, customer_id):
        """Email the invoice. Idempotent — QBO skips if already EmailSent."""
        return _qbo.send_invoice_email(invoice_id, customer_id,
                                       self.access_token, self.realm_id)

    def bump_due_date(self, invoice_id):
        return _qbo.bump_invoice_due_date_to_today(invoice_id, self.access_token,
                                                   self.realm_id)

    # ── payment / credit ───────────────────────────────────────────────────

    def create_payment(self, customer_id, amount, charge, lines, *, ref, memo):
        return _qbo.record_qbo_payment(customer_id, amount, charge, ref, memo,
                                       self.access_token, self.realm_id, lines)

    def send_receipt(self, payment_id, email):
        return _qbo.send_receipt(payment_id, email, self.access_token, self.realm_id)

    def apply_credit(self, credit_id, credit_type, invoice_id, customer_id, amount):
        return _qbo.apply_credit(credit_id, credit_type, invoice_id,
                                 {"value": customer_id}, amount,
                                 self.access_token, self.realm_id)

    # ── customer / catalog ─────────────────────────────────────────────────

    def customer_email(self, customer_id):
        return _qbo.fetch_qbo_customer_email(customer_id, self.access_token,
                                             self.realm_id)

    def classes(self):
        """name-lower -> ClassRef id. Cached per process."""
        return _qbo.fetch_qbo_classes(self.access_token, self.realm_id)

    def class_id(self, class_name):
        return self.classes().get((class_name or "").lower())


# ── self-check: no network — the module's _qbo is swapped ───────────────────

def main():
    checks = []
    def ok(n, c): checks.append((n, bool(c)))

    calls = []

    class _FakeQbo:
        def set_rate_limiter(self, conn): calls.append("rate_limiter")
        def refresh_qbo_token(self): calls.append("token"); return ("AT", "R1")
        def fetch_qbo_invoice(self, i, at, r, conn=None):
            calls.append(f"get_invoice:{i}:conn={conn is not None}"); return ({"Id": i}, None)
        def get_qbo_invoice_details(self, i, r, at, conn=None):
            calls.append("details"); return {"balance": 10.0, "email_status": None}
        def update_invoice_sparse(self, i, u, at, r, conn=None, intent_ref=None, actor=None):
            calls.append(f"patch:{i}:ref={intent_ref}:actor={actor}"); return {"success": True, "invoice": {"Id": i}}
        def fetch_qbo_classes(self, at, r): calls.append("classes"); return {"service": "7"}
        def send_invoice_email(self, i, c, at, r): calls.append("send"); return {"success": True}
        def fetch_qbo_customer_email(self, c, at, r): return "a@b.com"
        def bump_invoice_due_date_to_today(self, i, at, r): return {"success": True}
        def record_qbo_payment(self, *a): calls.append("record"); return {"success": True}
        def send_receipt(self, *a): return {"ok": True}
        def apply_credit(self, *a): calls.append("apply_credit"); return {"success": True}

    g = globals(); real = g["_qbo"]
    try:
        g["_qbo"] = _FakeQbo()
        sentinel = object()
        c = QboClient(conn=sentinel, actor="auto")
        ok("constructor arms the rate limiter", "rate_limiter" in calls)
        ok("constructor resolves the token ONCE", calls.count("token") == 1)

        c.get_invoice("68")
        ok("read passes conn (read = echo)", "get_invoice:68:conn=True" in calls)

        c.update_invoice("68", {"PrivateNote": "x"}, intent_ref="pre_process")
        ok("write carries intent_ref + actor", "patch:68:ref=pre_process:actor=auto" in calls)

        ok("class_id resolves via catalog", c.class_id("Service") == "7")
        ok("unknown class -> None", c.class_id("Nope") is None)
        ok("details returns leader view", c.invoice_details("68")["balance"] == 10.0)

        c2 = QboClient(conn=None)
        ok("conn-less client still constructs", c2.realm_id == "R1")
    finally:
        g["_qbo"] = real

    failed = [n for n, p in checks if not p]
    return {"ok": not failed, "passed": len(checks) - len(failed),
            "total": len(checks), "failed": failed}


if __name__ == "__main__":
    print(main())

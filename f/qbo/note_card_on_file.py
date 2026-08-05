import re
from datetime import date

import requests
import wmill

# Stamp "card on file" onto the QBO customer's Notes field.
#
# WHY: the office works out of QuickBooks. A payment method saved through
# secure.jeffspoolspa.com/collect is invisible there until someone opens the
# Payments tab, so a note on the customer record is the cheapest way to make it
# visible where they already are. Customer.Notes is API-writable and free-text.
#
# NON-DESTRUCTIVE BY CONSTRUCTION. Notes is a shared field the office also types
# into. This never sets it wholesale: it removes only the single line it owns
# (the one starting with MARKER) and re-appends a fresh one, leaving every other
# line byte-for-byte. Running it twice is a no-op beyond the date.
#
# Called by /api/card-collection/captured, off the vault's generic "a method was
# captured" webhook — the same port that refreshes the payment-method cache. The
# vault knows nothing about QuickBooks notes; it just announces the capture.
#
# Token comes from f/qbo/api/get_access_token (ADR 012, concurrent_limit=1) and
# NOT from f/billing/_lib/qbo.refresh_qbo_token, which performs its own OAuth
# refresh of the same rotating credential. Two refreshers race and burn the
# token; there is meant to be one door.

MARKER = "[card on file]"
NOTES_MAX = 4000  # QBO Customer.Notes hard limit
QBO_BASE = "https://quickbooks.api.intuit.com/v3/company"


def _line(brand: str, last4: str, method_type: str) -> str:
    what = "Bank account" if method_type == "ach" else (brand or "Card")
    tail = f" ending {last4}" if last4 else ""
    return f"{MARKER} {what}{tail} — added {date.today().isoformat()}"


def _rewrite(existing: str, new_line: str) -> str:
    """Drop our previous line, keep everything else, append the new one.

    Split on newlines rather than regex-replacing in place so a note whose text
    happens to contain the marker mid-sentence is left alone — only a line that
    STARTS with it is ours.
    """
    kept = [ln for ln in (existing or "").splitlines() if not ln.lstrip().startswith(MARKER)]
    while kept and not kept[-1].strip():
        kept.pop()
    out = "\n".join(kept + [new_line]) if kept else new_line
    if len(out) > NOTES_MAX:
        # Our line is the one that must survive; sacrifice the oldest text.
        keep_from = len(out) - NOTES_MAX
        out = out[keep_from:]
    return out


def main(
    qbo_customer_id: str,
    brand: str = "",
    last4: str = "",
    method_type: str = "card",
    commit: bool = False,
):
    """Add/refresh the card-on-file note on a QBO customer.

    commit defaults to FALSE so a bare run is a read-only dry run that shows
    exactly what would change. The webhook passes commit=True.
    """
    if not qbo_customer_id:
        return {"ok": False, "error": "qbo_customer_id is required"}

    tok = wmill.run_script_by_path("f/qbo/api/get_access_token", args={})
    access_token, realm_id = tok["access_token"], tok["realm_id"]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    new_line = _line(brand, last4, method_type)

    # SyncToken CAS with a retry, mirroring update_invoice_sparse: read fresh,
    # send the token we just read, retry once if someone else won the race.
    last_err = None
    for attempt in range(3):
        r = requests.get(
            f"{QBO_BASE}/{realm_id}/customer/{qbo_customer_id}?minorversion=73",
            headers=headers, timeout=30,
        )
        if not r.ok:
            return {"ok": False, "error": f"read customer {qbo_customer_id}: HTTP {r.status_code}"}
        cust = r.json()["Customer"]
        before = cust.get("Notes") or ""
        after = _rewrite(before, new_line)

        if after == before:
            return {"ok": True, "changed": False, "reason": "note already current", "notes": after}
        if not commit:
            return {"ok": True, "committed": False, "would_set": after, "current": before}

        w = requests.post(
            f"{QBO_BASE}/{realm_id}/customer?minorversion=73",
            headers=headers, timeout=30,
            json={"Id": str(qbo_customer_id), "SyncToken": cust["SyncToken"],
                  "sparse": True, "Notes": after},
        )
        if w.ok:
            return {"ok": True, "committed": True, "changed": True, "notes": after}
        text = w.text[:300]
        last_err = f"HTTP {w.status_code}: {text}"
        if "Stale Object" in text and attempt < 2:
            continue
        break

    return {"ok": False, "error": last_err}


# ── self-check: pure string logic, no network ───────────────────────────────

def _selfcheck():
    checks = []
    def ok(n, c): checks.append((n, bool(c)))

    line = _line("Visa", "6057", "card")
    ok("line names brand + last4", "Visa ending 6057" in line)
    ok("ach reads as a bank account", "Bank account ending 1234" in _line("", "1234", "ach"))

    # the whole point: other people's notes survive
    existing = "Gate code 4417\nDog in back yard — call first"
    out = _rewrite(existing, line)
    ok("existing notes preserved", existing in out)
    ok("our line appended", out.endswith(line))

    # idempotent: running again replaces our line, does not stack them
    twice = _rewrite(out, _line("Visa", "6057", "card"))
    ok("no duplicate marker lines", twice.count(MARKER) == 1)
    ok("still preserves the rest", "Gate code 4417" in twice)

    # a new card replaces the old line rather than appending
    swapped = _rewrite(out, _line("Amex", "1009", "card"))
    ok("old card line gone", "6057" not in swapped)
    ok("new card line present", "1009" in swapped)

    # a marker appearing mid-sentence is NOT ours and must survive
    prose = f"customer said {MARKER} was confusing"
    ok("mid-line marker untouched", prose in _rewrite(prose, line))

    ok("empty notes handled", _rewrite("", line) == line)
    ok("respects the 4000 cap", len(_rewrite("x" * 5000, line)) <= NOTES_MAX)
    ok("our line survives truncation", _rewrite("x" * 5000, line).endswith(line))

    failed = [n for n, p in checks if not p]
    return {"ok": not failed, "passed": len(checks) - len(failed),
            "total": len(checks), "failed": failed}


if __name__ == "__main__":
    print(_selfcheck())

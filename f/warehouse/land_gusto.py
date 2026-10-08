# extra_requirements:
# psycopg[binary]
# wmill
# Land Gusto facts into the warehouse, raw as they arrived: gusto.payweek (payrolls API)
# and gusto.punch_day (time-tracking CSV). No fixes (time zones, holidays, call-outs,
# excusals): those are Core. Windmill path: f/warehouse/land_gusto.py; only main() touches Windmill.
import csv
import io
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
import requests

API = "https://api.gusto.com"
ET = ZoneInfo("America/New_York")
HOURLY = {"Regular Hours": "reg_min", "Overtime": "ot_min",
          "Double overtime": "dot_min", "Double Overtime": "dot_min"}
SUFFIX_RE = re.compile(r"[\s,]+(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)


def upsert(cur, table, key, rows, batch=500):
    """One INSERT ... ON CONFLICT per batch; rows are dicts with the same keys."""
    if not rows:
        return
    cols = list(rows[0])
    sets = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key)
    sql = (f"insert into {table} ({', '.join(cols)}) select {', '.join(cols)} "
           f"from jsonb_populate_recordset(null::{table}, %s::jsonb) "
           f"on conflict ({', '.join(key)}) do update set {sets}")
    for i in range(0, len(rows), batch):
        cur.execute(sql, [json.dumps(rows[i:i + batch], default=str)])


def gget(url, h, params=None, tries=5):
    for _ in range(tries):
        r = requests.get(url, headers=h, params=params, timeout=60)
        if r.status_code != 429:
            return r
        time.sleep(int(r.headers.get("Retry-After", "15")))
    return r


def land_payweek(db_url, token, company_id, days_back=45, start=None, end=None):
    """Processed payrolls in the window -> gusto.payweek. The 45-day default heals late-processed payrolls."""
    h = {"Authorization": f"Bearer {token}", "X-Gusto-API-Version": "2025-06-15",
         "Accept": "application/json"}
    today = datetime.now(ET).date()
    start = start or str(today - timedelta(days=days_back))
    end = end or str(today)
    r = gget(f"{API}/v1/companies/{company_id}/payrolls", h,
             {"start_date": start, "end_date": end, "processing_statuses": "processed"})
    r.raise_for_status()
    payrolls = r.json()

    with psycopg.connect(db_url) as c, c.cursor() as cur:
        # ponytail: identity resolved at land so the PK holds as today; Core re-resolves if this moves
        cur.execute("select gusto_uuid::text, id from public.employees where gusto_uuid is not null")
        emp = dict(cur.fetchall())
        rows, unmatched = [], set()
        now = datetime.now(timezone.utc).isoformat()
        for p in payrolls:
            puuid = p.get("payroll_uuid") or p.get("uuid")
            pp = p.get("pay_period", {})
            comps, page = [], 1  # employee_compensations pages at 25 by default
            while True:
                d = gget(f"{API}/v1/companies/{company_id}/payrolls/{puuid}", h, {"per": 100, "page": page})
                d.raise_for_status()
                batch = d.json().get("employee_compensations", [])
                comps += batch
                if not batch or len(comps) >= int(d.headers.get("X-Total-Count", len(comps))):
                    break
                page += 1
            for comp in comps:
                eid = emp.get(comp.get("employee_uuid"))
                if eid is None:
                    unmatched.add(comp.get("employee_uuid"))
                    continue
                mins = {"reg_min": 0, "ot_min": 0, "dot_min": 0}
                for hc in comp.get("hourly_compensations", []):
                    col = HOURLY.get(hc.get("name"))
                    if col:
                        mins[col] += round(float(hc.get("hours") or 0) * 60)
                pto = sum(round(float(t.get("hours") or 0) * 60) for t in comp.get("paid_time_off", []))
                if not any(mins.values()) and not pto:
                    continue  # salaried / no-hours rows
                rows.append({"employee_id": eid, "payroll_uuid": puuid,
                             "period_start": pp.get("start_date"), "period_end": pp.get("end_date"),
                             **mins, "adj_min": round(mins["reg_min"] + mins["ot_min"] * 1.5 + mins["dot_min"] * 2.0),
                             "pto_min": pto, "updated_at": now})
            time.sleep(0.15)
        upsert(cur, "gusto.payweek", ("employee_id", "payroll_uuid"), rows)
    return {"payrolls": len(payrolls), "rows": len(rows), "window": [start, end],
            "unmatched_gusto_uuids": sorted(u for u in unmatched if u)}


def norm_name(first, last):
    return f"{first} {SUFFIX_RE.sub('', last).strip()}".lower()


def clock_min(seg, end=False):
    """'6:28 AM - 2:44 PM' -> minutes after midnight of the start (or end). None if open or blank."""
    s = (seg or "").strip()
    if " - " not in s or "Now" in s:
        return None
    try:
        t = datetime.strptime(s.split(" - ")[1 if end else 0].strip(), "%I:%M %p").time()
    except ValueError:
        return None
    return t.hour * 60 + t.minute


def mins(x):
    try:
        return round(float(x) * 60)
    except (TypeError, ValueError):
        return 0


def parse_punch_csv(csv_text, source_file=None):
    """Gusto 'time tracking hours' CSV (one 'Hours for Last, First' block per employee) -> rows keyed by name."""
    rows_in = list(csv.reader(io.StringIO(csv_text)))
    starts = [(i, r[0].replace("Hours for ", "").strip())
              for i, r in enumerate(rows_in) if r and r[0].startswith("Hours for ")]
    out, now = [], datetime.now(timezone.utc).isoformat()
    for n, (b0, name) in enumerate(starts):
        block = rows_in[b0 + 1:starts[n + 1][0] if n + 1 < len(starts) else len(rows_in)]
        if not block:
            continue
        col = {h: i for i, h in enumerate(block[0])}
        seg_cols = [i for h, i in col.items() if h.startswith("Hours")]  # "Hours", "Hours 2", ...

        def val(r, h):
            i = col.get(h)
            return r[i] if i is not None and i < len(r) else ""

        for r in block[1:]:
            day = None
            for fmt in ("%m/%d/%y", "%m/%d/%Y"):
                try:
                    day = datetime.strptime((r[0] if r else "").strip(), fmt).date()
                    break
                except ValueError:
                    pass
            if day is None:
                continue
            segs = [r[i].strip() for i in seg_cols if i < len(r) and r[i].strip()]
            outs = [m for m in (clock_min(s, end=True) for s in segs) if m is not None]
            out.append({"employee_name": name, "day": str(day),
                        "clock_in_min": clock_min(segs[0]) if segs else None,
                        "clock_out_min": outs[-1] if outs else None,
                        "punches": " | ".join(segs) or None,
                        "worked_min": mins(val(r, "Total hours")), "reg_min": mins(val(r, "Regular hours")),
                        "ot_min": mins(val(r, "Overtime")), "dot_min": mins(val(r, "Double overtime")),
                        "pto_min": mins(val(r, "Paid time off")), "unpaid_min": mins(val(r, "Unpaid time off")),
                        "approval_status": val(r, "Approval status").strip() or None,
                        "source_file": source_file, "loaded_at": now})
    return out


def land_punch_csv(db_url, csv_text, source_file=None):
    """Gusto time-tracking CSV -> gusto.punch_day, raw (no zone fix, no holidays, no call-outs)."""
    rows = parse_punch_csv(csv_text, source_file)
    with psycopg.connect(db_url) as c, c.cursor() as cur:
        cur.execute("select first_name, last_name, id from public.employees "
                    "where first_name is not null and last_name is not null")
        emp = {norm_name(f, l): i for f, l, i in cur.fetchall()}
        unmatched = set()
        for row in rows:
            last, _, first = row["employee_name"].partition(",")
            row["employee_id"] = emp.get(norm_name(first.strip(), last.strip())) if first else None
            if row["employee_id"] is None:
                unmatched.add(row["employee_name"])
        rows = [r for r in rows if r["employee_id"] is not None]
        upsert(cur, "gusto.punch_day", ("employee_id", "day"), rows)
    days = [r["day"] for r in rows]
    return {"rows": len(rows), "day_range": [min(days), max(days)] if days else None,
            "unmatched_names": sorted(unmatched)}


def main(kind: str, start: str = "", end: str = "", days_back: int = 45,
         csv_text: str = "", source_file: str = ""):
    """kind: 'payweek' | 'punch_csv'."""
    import wmill
    db = wmill.get_variable("f/warehouse/land_url")
    if kind == "payweek":
        return land_payweek(db, wmill.get_variable("f/gusto/personal_access_token"),
                            wmill.get_variable("f/gusto/company_id"), days_back, start or None, end or None)
    if kind == "punch_csv":
        return land_punch_csv(db, csv_text, source_file or None)
    raise ValueError(f"unknown kind {kind!r}")


if __name__ == "__main__":  # parser self-check, no network or database
    sample = ("Hours for Doe Jr., John\nDate,Hours,Hours 2,Total hours,Regular hours,Overtime,"
              "Double overtime,Paid time off,Unpaid time off,Approval status\n"
              "09/02/26,6:28 AM - 11:00 AM,11:30 AM - 2:44 PM,7.75,7.75,0,0,0,0,Approved\n"
              "09/03/26,,,0,0,0,0,8,0,Approved\nTotal,,,7.75\n")
    a, b = parse_punch_csv(sample, "t.csv")
    assert (a["clock_in_min"], a["clock_out_min"], a["worked_min"]) == (388, 884, 465), a
    assert b["clock_in_min"] is None and b["pto_min"] == 480 and b["punches"] is None, b
    assert norm_name("John", "Doe Jr.") == "john doe"
    print("ok")

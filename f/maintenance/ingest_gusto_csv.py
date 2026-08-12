# f/maintenance/ingest_gusto_csv — Gusto time-tracking CSV → maintenance.gusto_day
# Port of maint_meeting/parse_gusto.py block logic (stdlib csv, no pandas).
# Input: the raw text of Gusto's "time tracking hours" export (per-employee
# "Hours for <Last, First>" blocks). Idempotent upsert on (employee_id, day).
# This is the manual/month-end writer; a daily API pull writes the same table
# once Gusto grants the report scopes.
import csv
import io
import re
from datetime import datetime, timezone

import wmill
from supabase import create_client

SUFFIX_RE = re.compile(r"[\s,]+(jr|sr|ii|iii|iv|v)\.?$", re.IGNORECASE)
HOLIDAYS_2026 = {"2026-01-01", "2026-05-25", "2026-07-04",
                 "2026-09-07", "2026-11-26", "2026-12-25"}


def norm_name(last_first):
    if "," not in last_first:
        return None
    last, first = [p.strip() for p in last_first.split(",", 1)]
    last = SUFFIX_RE.sub("", last).strip()
    return f"{first} {last}".lower()


def start_min(hours_str):
    s = str(hours_str or "").strip()
    if not s or "Now" in s or " - " not in s:
        return None
    try:
        t = datetime.strptime(s.split(" - ")[0].strip(), "%I:%M %p").time()
        return t.hour * 60 + t.minute
    except ValueError:
        return None


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def main(csv_text: str):
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"),
                       wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))
    # suffix-stripped "first last" -> employee id
    emp = {}
    for e in sb.table("employees").select("id,first_name,last_name").execute().data:
        if e["first_name"] and e["last_name"]:
            last = SUFFIX_RE.sub("", e["last_name"]).strip()
            emp[f'{e["first_name"]} {last}'.lower()] = e["id"]

    rows_in = list(csv.reader(io.StringIO(csv_text)))
    starts = [(i, r[0].replace("Hours for ", "").strip())
              for i, r in enumerate(rows_in) if r and r[0].startswith("Hours for ")]

    out, unmatched, days = [], set(), set()
    for idx, (blk_start, name) in enumerate(starts):
        blk_end = starts[idx + 1][0] if idx + 1 < len(starts) else len(rows_in)
        block = rows_in[blk_start + 1:blk_end]
        if not block:
            continue
        headers = block[0]
        col = {h: i for i, h in enumerate(headers)}
        key = norm_name(name)
        eid = emp.get(key) if key else None
        if eid is None:
            unmatched.add(name)
            continue

        def val(r, h):
            i = col.get(h)
            return r[i] if i is not None and i < len(r) else ""

        for r in block[1:]:
            if not (r and r[0] and r[0][:2].isdigit() and "/" in r[0][:5]):
                continue
            try:
                day = datetime.strptime(r[0].strip(), "%m/%d/%Y").date()
            except ValueError:
                continue
            total = fnum(val(r, "Total hours"))
            reg = fnum(val(r, "Regular hours"))
            ot = fnum(val(r, "Overtime"))
            dot = fnum(val(r, "Double overtime"))
            pto = fnum(val(r, "Paid time off"))
            upto = fnum(val(r, "Unpaid time off"))
            callout = (total == 0 and pto == 0 and upto == 0
                       and str(val(r, "Approval status")).strip() == "Approved"
                       and day.isoweekday() < 6
                       and str(day) not in HOLIDAYS_2026)
            out.append({"employee_id": eid, "day": str(day),
                        "clock_in_min": start_min(val(r, "Hours")),
                        "worked_min": round(total * 60),
                        "adj_min": round((reg + ot * 1.5 + dot * 2.0) * 60),
                        "pto_min": round((pto + upto) * 60),
                        "callout": callout, "source": "csv",
                        "updated_at": datetime.now(timezone.utc).isoformat()})
            days.add(str(day))

    for i in range(0, len(out), 400):
        sb.schema("maintenance").table("gusto_day").upsert(out[i:i + 400]).execute()
    return {"rows": len(out), "employees": len(starts) - len(unmatched),
            "day_range": [min(days), max(days)] if days else None,
            "unmatched_names": sorted(unmatched)}

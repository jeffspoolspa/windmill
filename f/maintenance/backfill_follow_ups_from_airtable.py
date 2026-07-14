# f/maintenance/backfill_follow_ups_from_airtable
#
# One-shot batch import of historical Airtable "Maintenance Follow up" tickets
# into maintenance.follow_ups, with customer + tech mapping and media re-hosting.
#
# Module: docs/modules/maintenance/operations.md
# Status: [active]
# Concurrency key: airtable_api
#
# Triggered by:
#   - manual (Carter). mode='dry_run' reads+reports only; 'import_rows' writes
#     rows; 'rehost_media' downloads Airtable attachments into our storage.
#
# Tables touched:
#   maintenance.follow_ups   [write]  upsert imported rows (source='airtable_backfill')
#   public."Customers"       [read]   customer name -> id matching pool
#   public.employees         [read]   tech name -> id (hire_date + branch + initial)
#   public.branches          [read]   branch code (BWK/CAM/RH) -> branch name
#   maintenance.tasks        [read]   task-linked customer surnames (household match)
#   storage 'follow-ups'     [write]  re-hosted historical media
#
# External APIs:
#   - Airtable: GET base apppQeFQh1Mi6Mv3p / table tbltojdp1l9k4xmSN (+ attachment CDN)
#
# Why this exists:
#   The office tracked field follow-ups in Airtable for years; the app now owns
#   new tickets but the history matters. Customer must match (or the row is
#   skipped - useless data); tech resolves via hire_date/branch/initial or stays
#   NULL with the raw name kept. Airtable attachment URLs rotate, so media is
#   downloaded and re-hosted in our own bucket. Idempotent on airtable_record_id.

# requirements:
# wmill
# requests
# supabase

import re
import difflib
from datetime import datetime, date
from zoneinfo import ZoneInfo

import requests
import wmill
from supabase import create_client

BASE_ID = "apppQeFQh1Mi6Mv3p"
TABLE_ID = "tbltojdp1l9k4xmSN"
AIRTABLE_URL = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}"
MAINT_DEPT = "757659e3-d73f-48c3-999f-6f071f1e3587"
BRANCH_CODE = {"BWK": "Brunswick, GA", "CAM": "Saint Marys, GA", "RH": "Richmond Hill, GA"}

# Confirmed customer rescues (normalized name -> Customers.id) from manual review.
CUST_OVERRIDE = {
    "aylor charlotte": 333, "faith hamilton trent": 3107, "hampton inn ssi": 3125,
    "house island": 7788, "asher l": 299, "bohmer": 706, "blue heron inn": 680,
    "blue heron": 680, "blue harron inn": 680, "chad marlowe": 319287,
    "oaks on triver": 5744, "golliday": 2856, "frank trudau": 1994,
}
# Tech nickname -> (first, last). Confirmed with Carter.
NICK = {
    "emman": ("emmanuel", "thornton"), "emmanual": ("emmanuel", "thornton"),
    "emmanuel": ("emmanuel", "thornton"), "mary": ("marie", "kidd"),
    "dave": ("william", "bland"), "damien": ("damian", "elmore"),
    "ty": ("tynisa", "darden"), "jack": ("jackson", "morey"),
    "will": ("william", "frost"), "william": ("william", "mcintyre"),
    "josh": ("joshua", "carroll"), "gabe": ("gabriel", "cooper"),
    "redmon": ("travis", "redmon"), "abass": ("aaron", "bass"),
}
DEFAULT_TECH = {"joshua": ("joshua", "francis")}  # bare 'joshua' -> Francis (Carter)

ISSUE_MAP = {}  # historical issues stored as-is (CHECK dropped); no remap needed


# ---------- helpers ----------
def _sb():
    return create_client(wmill.get_variable("f/SUPABASE/URL"),
                         wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))

def _at_key():
    r = wmill.get_resource("u/carter/airtable")
    k = r.get("apiKey") if isinstance(r, dict) else r
    if isinstance(k, str) and k.startswith("$var:"):
        k = wmill.get_variable(k.replace("$var:", ""))
    return k

def _strip_notes(s):
    s = re.sub(r"\([^)]*\)", " ", str(s or ""))
    return re.split(r"\s[-–]\s", s)[0]

def norm(s):
    return " ".join(sorted(re.findall(r"[a-z0-9]+", _strip_notes(s).lower())))

def surname(s):
    # Household match keys off the family surname. Residential "LAST, FIRST"
    # uses the part before the comma; a bare single-word name uses that word.
    # Multi-word commercial names (esp. with a "- BWK/CAM" branch suffix) return
    # "" so they can't false-match on a branch code or a stray token.
    s = _strip_notes(s).strip()
    if "," in s:
        return re.sub(r"[^a-z]", "", s.split(",")[0].lower())
    toks = [t for t in re.findall(r"[a-z]+", s.lower()) if t.upper() not in BRANCH_CODE]
    return toks[0] if len(toks) == 1 else ""

def _paginate(sb, table, select, schema=None):
    out, start = [], 0
    q = sb.schema(schema).table(table) if schema else sb.table(table)
    while True:
        rows = q.select(select).range(start, start + 999).execute().data
        out += rows
        if len(rows) < 1000:
            return out
        start += 1000
        q = sb.schema(schema).table(table) if schema else sb.table(table)

def load_airtable(headers):
    recs, offset = [], None
    while True:
        params = {"pageSize": 100}
        if offset:
            params["offset"] = offset
        r = requests.get(AIRTABLE_URL, headers=headers, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        recs += j.get("records", [])
        offset = j.get("offset")
        if not offset:
            return recs


# ---------- matcher build ----------
def _phone10(s):
    d = re.sub(r"\D", "", str(s or ""))
    return d[-10:] if len(d) >= 10 else ""

def build_maps(sb):
    custs = _paginate(sb, "Customers", "id,display_name,first_name,last_name,company,account_name,phone")
    pool = {}
    disp = {}
    phone_idx = {}
    for c in custs:
        disp[c["id"]] = c.get("display_name")
        for v in (c.get("display_name"),
                  f"{c.get('first_name') or ''} {c.get('last_name') or ''}",
                  c.get("company"), c.get("account_name")):
            k = norm(v)
            if k:
                pool.setdefault(k, c["id"])
        p = _phone10(c.get("phone"))
        if p:
            phone_idx.setdefault(p, set()).add(c["id"])
    pool_keys = list(pool.keys())

    # household surname index over task-linked customers
    task_cids = {r["customer_id"] for r in _paginate(sb, "tasks", "customer_id", schema="maintenance")}
    surn = {}
    for cid in task_cids:
        s = surname(disp.get(cid))
        if s:
            surn.setdefault(s, []).append(cid)

    # employees + branches
    branches = {b["id"]: b["name"] for b in sb.table("branches").select("id,name").execute().data}
    emps = _paginate(sb, "employees", "id,first_name,last_name,hire_date,department_id,branch_id")
    E = [{"id": e["id"], "first": (e.get("first_name") or "").lower(),
          "last": (e.get("last_name") or "").lower(), "hire": e.get("hire_date") or "0000-01-01",
          "branch": branches.get(e.get("branch_id")),
          "maint": e.get("department_id") == MAINT_DEPT} for e in emps]
    byfirst = {}
    for e in E:
        byfirst.setdefault(e["first"], []).append(e)

    def find_emp(first, last):
        for e in E:
            if e["first"] == first and e["last"].startswith(last[:4]):
                return e["id"]
        return None

    return {"pool": pool, "pool_keys": pool_keys, "surn": surn, "phone_idx": phone_idx,
            "byfirst": byfirst, "find_emp": find_emp}


def match_customer(name, phone, M):
    k = norm(name)
    if not k:
        return None, "blank"
    if k in CUST_OVERRIDE:
        return CUST_OVERRIDE[k], "override"
    if k in M["pool"]:
        return M["pool"][k], "exact"
    cm = difflib.get_close_matches(k, M["pool_keys"], n=1, cutoff=0.88)
    if cm:
        return M["pool"][cm[0]], "fuzzy"
    # context clue: a phone that maps to exactly one customer
    p = _phone10(phone)
    if p:
        ids = M["phone_idx"].get(p)
        if ids and len(ids) == 1:
            return next(iter(ids)), "phone"
    cands = M["surn"].get(surname(name), [])
    if len(cands) == 1:
        return cands[0], "household"
    return None, ("household_ambig" if len(cands) > 1 else "none")


def match_tech(name, tdate, M):
    codes = [c.strip().upper() for c in re.findall(r"\(([^)]*)\)", str(name or ""))]
    br = next((BRANCH_CODE[c] for c in codes if c in BRANCH_CODE), None)
    toks = re.findall(r"[a-z]+", re.sub(r"\([^)]*\)", "", str(name or "")).lower())
    if not toks or toks[0] in ("other", "anonymous"):
        return None, "null"
    tok, li = toks[0], (toks[1][:1] if len(toks) > 1 else "")
    if tok in NICK:
        eid = M["find_emp"](*NICK[tok])
        if eid:
            return eid, "nick"
    cands = [e for e in M["byfirst"].get(tok, []) if e["hire"] <= tdate]
    if br:
        fb = [e for e in cands if e["branch"] == br]
        if fb:
            cands = fb
    if li:
        fl = [e for e in cands if e["last"][:1] == li]
        if fl:
            cands = fl
    if len(cands) == 1:
        return cands[0]["id"], "confident"
    if len(cands) > 1:
        m = [e for e in cands if e["maint"]]
        if len(m) == 1:
            return m[0]["id"], "assumed_maint"
        if tok in DEFAULT_TECH:
            eid = M["find_emp"](*DEFAULT_TECH[tok])
            if eid and any(e["id"] == eid for e in cands):
                return eid, "default"
        return None, "ambiguous"
    if tok in DEFAULT_TECH:
        eid = M["find_emp"](*DEFAULT_TECH[tok])
        if eid:
            return eid, "default"
    return None, "null_prehire"


def _created(rec):
    fld = rec.get("fields", {})
    return (fld.get("Created 2") or fld.get("Created") or "")

def resolve(rec, M):
    fld = rec.get("fields", {})
    created = _created(rec)
    cid, cw = match_customer(fld.get("Customer Name"), fld.get("Phone Number"), M)
    if not cid:
        return None, cw, None
    eid, ew = match_tech(fld.get("Tech Name"), created[:10], M)
    status = "closed" if "Done" in (fld.get("Status") or []) else "open"
    media = []
    for f in ((fld.get("Images") or []) + (fld.get("video") or [])):
        t = "video" if str(f.get("type", "")).startswith("video") else "image"
        media.append({"type": t, "source_url": f.get("url"), "airtable_id": f.get("id")})
    et = datetime.fromisoformat(created).astimezone(ZoneInfo("America/New_York")) if created else None
    row = {
        "created_at": et.isoformat() if et else None,
        "customer_id": cid,
        "tech_employee_id": eid,
        "issue": ISSUE_MAP.get(fld.get("Issue"), fld.get("Issue")) or "Other",
        "description": fld.get("Description of Issue") or "",
        "media": media,
        "equipment_off": {"TRUE": True, "FALSE": False}.get(fld.get("Equipment Off?")),
        "status": status,
        "source": "airtable_backfill",
        "source_tech_name": fld.get("Tech Name"),
        "source_customer_name": fld.get("Customer Name"),
        "airtable_record_id": rec["id"],
        "airtable_synced_at": "now()",
    }
    return row, cw, ew


# ---------- main ----------
def main(mode: str = "dry_run", since: str = "2023-01-01", batch: int = 300):
    sb = _sb()
    headers = {"Authorization": f"Bearer {_at_key()}", "Content-Type": "application/json"}
    recs = load_airtable(headers)
    recs = [r for r in recs if _created(r)[:10] >= since]
    M = build_maps(sb)

    if mode == "dry_run":
        cust_t, tech_t = {}, {}
        skips, flagged = [], []
        for r in recs:
            row, cw, ew = resolve(r, M)
            cust_t[cw] = cust_t.get(cw, 0) + 1
            if not row:
                if cw not in ("blank",):
                    skips.append(r["fields"].get("Customer Name"))
                continue
            tech_t[ew] = tech_t.get(ew, 0) + 1
            if cw in ("fuzzy", "household", "override", "phone") or ew == "assumed_maint":
                flagged.append({"cust": row["source_customer_name"], "cust_via": cw,
                                "tech": row["source_tech_name"], "tech_via": ew,
                                "customer_id": row["customer_id"]})
        matched = sum(v for k, v in cust_t.items()
                      if k in ("exact", "override", "fuzzy", "household", "phone"))
        return {
            "mode": "dry_run", "since": since, "total": len(recs),
            "customer_matched": matched, "customer_by_tier": cust_t,
            "skipped_named": sorted(set(x for x in skips if x))[:60],
            "skipped_named_count": len(set(x for x in skips if x)),
            "tech_by_tier": tech_t,
            "flagged_sample": flagged[:40], "flagged_total": len(flagged),
        }

    if mode == "import_rows":
        rows = [resolve(r, M)[0] for r in recs]
        rows = [x for x in rows if x]
        n = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i:i + batch]
            sb.schema("maintenance").table("follow_ups").upsert(
                chunk, on_conflict="airtable_record_id").execute()
            n += len(chunk)
        return {"mode": "import_rows", "imported": n}

    if mode == "rehost_media":
        pend = (sb.schema("maintenance").table("follow_ups")
                .select("id,media").eq("source", "airtable_backfill")
                .limit(batch).execute().data)
        done = 0
        for row in pend:
            media = row.get("media") or []
            if not any("source_url" in m for m in media):
                continue
            newm = []
            for i, m in enumerate(media):
                if "path" in m or not m.get("source_url"):
                    newm.append(m)
                    continue
                resp = requests.get(m["source_url"], timeout=120)
                if not resp.ok:
                    newm.append(m)  # leave for retry
                    continue
                ext = "mp4" if m["type"] == "video" else "jpg"
                path = f"backfill/{row['id']}/{i}.{ext}"
                sb.storage.from_("follow-ups").upload(
                    path, resp.content,
                    {"content-type": resp.headers.get("Content-Type", "application/octet-stream"),
                     "upsert": "true"})
                newm.append({"type": m["type"], "path": path})
            sb.schema("maintenance").table("follow_ups").update(
                {"media": newm}).eq("id", row["id"]).execute()
            done += 1
        return {"mode": "rehost_media", "processed": done,
                "note": "re-run until processed=0"}

    return {"error": f"unknown mode {mode}"}

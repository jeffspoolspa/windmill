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

# Commercial accounts typed many ways in Airtable -> our Customers.id, matched
# by required tokens (order-independent) on the normalized name. Confirmed by
# DB lookup (company/account_name + task counts).
COMMERCIAL = [
    (frozenset({"carriage", "gate"}), 1199),
    (frozenset({"island", "retreat"}), 3777),
    (frozenset({"sugarmill"}), 7590),
    (frozenset({"frederica", "golf"}), 2609),
    (frozenset({"fredrica", "golf"}), 2609),
    (frozenset({"azalea"}), 335),
    (frozenset({"azela"}), 335),
    (frozenset({"broadfield"}), 860),
    (frozenset({"queens", "court"}), 6289),
    (frozenset({"queen", "court"}), 6289),
    (frozenset({"howard", "coffin"}), 3612),
    (frozenset({"seafarer"}), 6968),
    (frozenset({"grants", "ferry"}), 2943),
    (frozenset({"coastal", "rv"}), 1424),
    (frozenset({"costal", "rv"}), 1424),  # "Costal" misspelling
]

# Airtable statuses that mean the follow-up is complete (Carter).
DONE_STATUSES = {"Done", "Scheduled"}

def _commercial(toks):
    for req, cid in COMMERCIAL:
        if req <= toks:
            return cid
    if {"best", "western"} <= toks:
        if "main" in toks or "ssi" in toks or "simons" in toks:
            return 578
        if "kingsland" in toks or "plus" in toks:
            return 579
        return 580  # Brunswick / Venture Drive (the account with tasks)
    return None


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

    emp_name = {e["id"]: " ".join(w.title() for w in (e["first"], e["last"]) if w) for e in E}

    return {"pool": pool, "pool_keys": pool_keys, "surn": surn, "phone_idx": phone_idx,
            "byfirst": byfirst, "find_emp": find_emp, "disp": disp, "emp_name": emp_name}


def match_customer(name, phone, M):
    k = norm(name)
    if not k:
        return None, "blank"
    if k in CUST_OVERRIDE:
        return CUST_OVERRIDE[k], "override"
    if k in M["pool"]:
        return M["pool"][k], "exact"
    comm = _commercial(set(k.split()))
    if comm:
        return comm, "commercial"
    cm = difflib.get_close_matches(k, M["pool_keys"], n=1, cutoff=0.88)
    if cm:
        return M["pool"][cm[0]], "fuzzy"
    # context clue: a phone that maps to exactly one customer
    p = _phone10(phone)
    if p:
        ids = M["phone_idx"].get(p)
        if ids and len(ids) == 1:
            return next(iter(ids)), "phone"
    # Household: residential ticket named after a family member whose account is
    # under another member. Comma form -> the surname; a bare "First Last" tries
    # both tokens; match only if exactly one task-linked household results.
    hs = _strip_notes(name).strip()
    htoks = re.findall(r"[a-z]+", hs.lower())
    if "," in hs:
        hkeys = [re.sub(r"[^a-z]", "", hs.split(",")[0].lower())]
    elif len(htoks) == 1:
        hkeys = htoks
    elif len(htoks) == 2 and not any(t.upper() in BRANCH_CODE for t in htoks):
        hkeys = htoks
    else:
        hkeys = []
    hh = set()
    for hk in hkeys:
        hh.update(M["surn"].get(hk, []))
    if len(hh) == 1:
        return next(iter(hh)), "household"
    return None, ("household_ambig" if len(hh) > 1 else "none")


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
    status = "closed" if (DONE_STATUSES & set(fld.get("Status") or [])) else "open"
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
def _signed_url(sb, path):
    res = sb.storage.from_("follow-ups").create_signed_url(path, 3600)
    return res.get("signedURL") or res.get("signedUrl") or res.get("signed_url")

def _airtable_fields(sb, r, M):
    created = r.get("created_at")
    ts = (datetime.fromisoformat(created).astimezone(ZoneInfo("America/New_York"))
          .strftime("%m/%d/%Y %I:%M %p") if created else "")
    images, videos = [], []
    for m in (r.get("media") or []):
        u = _signed_url(sb, m["path"]) if m.get("path") else None
        if u:
            (videos if m.get("type") == "video" else images).append({"url": u})
    fields = {
        "Timestamp": ts,
        "Tech Name": M["emp_name"].get(r.get("tech_employee_id"), r.get("source_tech_name") or ""),
        "Customer Name": M["disp"].get(r.get("customer_id"), r.get("source_customer_name") or ""),
        "Issue": r.get("issue"),
        "Description of Issue": r.get("description") or "",
    }
    if r.get("equipment_off") is not None:
        fields["Equipment Off?"] = "TRUE" if r["equipment_off"] else "FALSE"
    if r.get("next_steps"):
        fields["Next Steps"] = r["next_steps"]
    if images:
        fields["Images"] = images
    if videos:
        fields["video"] = videos
    return fields


def _push_pending_app_rows(sb, headers):
    # Real-time push of new app submissions to Airtable. Fired per-insert by the
    # (guarded) wake trigger and as a daily backstop. concurrent_limit=1 on this
    # script serializes pushes so two quick submissions can't double-create.
    fu = sb.schema("maintenance").table("follow_ups")
    rows = (fu.select("*").eq("source", "app").is_("airtable_record_id", "null")
            .lt("sync_attempts", 5).order("created_at").limit(200).execute().data)
    if not rows:
        return {"mode": "push", "pushed": 0}
    cust_ids = list({r["customer_id"] for r in rows if r.get("customer_id")})
    emp_ids = list({r["tech_employee_id"] for r in rows if r.get("tech_employee_id")})
    disp = {c["id"]: c.get("display_name") for c in
            (sb.table("Customers").select("id,display_name").in_("id", cust_ids).execute().data if cust_ids else [])}
    enm = {}
    for e in (sb.table("employees").select("id,first_name,last_name").in_("id", emp_ids).execute().data if emp_ids else []):
        enm[e["id"]] = " ".join(w.title() for w in (e.get("first_name") or "", e.get("last_name") or "") if w)
    M = {"disp": disp, "emp_name": enm}
    pushed = 0
    for r in rows:
        att = (r.get("sync_attempts") or 0) + 1
        try:
            resp = requests.post(AIRTABLE_URL, headers=headers,
                                 json={"records": [{"fields": _airtable_fields(sb, r, M)}], "typecast": True},
                                 timeout=30)
            if resp.ok:
                fu.update({"airtable_record_id": resp.json()["records"][0]["id"], "airtable_synced_at": "now()",
                           "sync_error": None, "sync_attempts": att}).eq("id", r["id"]).execute()
                pushed += 1
            else:
                fu.update({"sync_error": resp.text[:500], "sync_attempts": att}).eq("id", r["id"]).execute()
        except Exception as e:
            fu.update({"sync_error": str(e)[:500], "sync_attempts": att}).eq("id", r["id"]).execute()
    return {"mode": "push", "pushed": pushed}


def _rehost_media(sb, batch, after_id):
    q = (sb.schema("maintenance").table("follow_ups")
         .select("id,media").in_("source", ["airtable_backfill", "airtable_ingest"]))
    if after_id:
        q = q.gt("id", after_id)
    rows = q.order("id").limit(batch).execute().data
    if not rows:
        return {"mode": "rehost_media", "done": True, "rehosted": 0}
    done = 0
    for row in rows:
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
                newm.append(m)
                continue
            ext = "mp4" if m["type"] == "video" else "jpg"
            path = f"backfill/{row['id']}/{i}.{ext}"
            sb.storage.from_("follow-ups").upload(path, resp.content,
                {"content-type": resp.headers.get("Content-Type", "application/octet-stream"), "upsert": "true"})
            newm.append({"type": m["type"], "path": path})
        sb.schema("maintenance").table("follow_ups").update({"media": newm}).eq("id", row["id"]).execute()
        done += 1
    return {"mode": "rehost_media", "rehosted": done, "last_id": rows[-1]["id"], "done": len(rows) < batch}


def main(mode: str = "dry_run", since: str = "2023-01-01", batch: int = 300,
         after_id: str = "", apply: bool = True):
    sb = _sb()
    headers = {"Authorization": f"Bearer {_at_key()}", "Content-Type": "application/json"}

    # Lightweight modes skip the full Airtable load + matcher build.
    if mode == "push":
        return _push_pending_app_rows(sb, headers)
    if mode == "rehost_media":
        return _rehost_media(sb, batch, after_id)

    recs = load_airtable(headers)
    recs = [r for r in recs if _created(r)[:10] >= since]
    M = build_maps(sb)

    if mode == "dry_run":
        cust_t, tech_t, status_t = {}, {}, {}
        skips, flagged = [], []
        for r in recs:
            row, cw, ew = resolve(r, M)
            cust_t[cw] = cust_t.get(cw, 0) + 1
            if not row:
                if cw not in ("blank",):
                    skips.append(r["fields"].get("Customer Name"))
                continue
            tech_t[ew] = tech_t.get(ew, 0) + 1
            status_t[row["status"]] = status_t.get(row["status"], 0) + 1
            if cw in ("fuzzy", "household", "override", "phone", "commercial") or ew == "assumed_maint":
                flagged.append({"cust": row["source_customer_name"], "cust_via": cw,
                                "tech": row["source_tech_name"], "tech_via": ew,
                                "customer_id": row["customer_id"]})
        matched = sum(v for k, v in cust_t.items()
                      if k in ("exact", "override", "fuzzy", "household", "phone", "commercial"))
        return {
            "mode": "dry_run", "since": since, "total": len(recs),
            "customer_matched": matched, "customer_by_tier": cust_t,
            "skipped_named": sorted(set(x for x in skips if x))[:60],
            "skipped_named_count": len(set(x for x in skips if x)),
            "tech_by_tier": tech_t, "status_split": status_t,
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

    if mode == "daily_sync":
        # One daily reconcile: push pending app rows -> Airtable, ingest Airtable
        # rows not yet in our DB, refresh open tickets (status + next_steps).
        by_id = {r["id"]: r for r in recs}
        fu = sb.schema("maintenance").table("follow_ups")

        existing = set()
        start = 0
        while True:
            rows = (fu.select("airtable_record_id").not_.is_("airtable_record_id", "null")
                    .range(start, start + 999).execute().data)
            existing |= {r["airtable_record_id"] for r in rows}
            if len(rows) < 1000:
                break
            start += 1000

        # 1) push new app rows to Airtable
        push_rows = (fu.select("*").eq("source", "app").is_("airtable_record_id", "null")
                     .limit(500).execute().data)
        pushed = 0
        for r in push_rows:
            if not apply:
                pushed += 1
                continue
            resp = requests.post(AIRTABLE_URL, headers=headers,
                                 json={"records": [{"fields": _airtable_fields(sb, r, M)}], "typecast": True},
                                 timeout=30)
            if resp.ok:
                rid = resp.json()["records"][0]["id"]
                fu.update({"airtable_record_id": rid, "airtable_synced_at": "now()"}).eq("id", r["id"]).execute()
                pushed += 1

        # 2) ingest Airtable records not already in our DB
        ingest = []
        for r in recs:
            if r["id"] in existing:
                continue
            row = resolve(r, M)[0]
            if row:
                row["source"] = "airtable_ingest"
                ingest.append(row)
        if apply and ingest:
            for i in range(0, len(ingest), batch):
                fu.upsert(ingest[i:i + batch], on_conflict="airtable_record_id").execute()

        # 3) refresh open tickets from their Airtable record
        open_rows = (fu.select("id,airtable_record_id,status,next_steps").eq("status", "open")
                     .not_.is_("airtable_record_id", "null").execute().data)
        closed_n, notes_n = 0, 0
        for r in open_rows:
            rec = by_id.get(r["airtable_record_id"])
            if not rec:
                continue
            flds = rec.get("fields", {})
            upd = {}
            if DONE_STATUSES & set(flds.get("Status") or []):
                upd["status"] = "closed"
                closed_n += 1
            ns = flds.get("Next Steps")
            if ns and ns != r.get("next_steps"):
                upd["next_steps"] = ns
                notes_n += 1
            if upd and apply:
                fu.update(upd).eq("id", r["id"]).execute()

        return {"mode": "daily_sync", "apply": apply, "pushed": pushed,
                "ingested": len(ingest), "refresh_closed": closed_n, "refresh_notes": notes_n}

    return {"error": f"unknown mode {mode}"}

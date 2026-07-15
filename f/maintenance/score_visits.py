# f/maintenance/score_visits — DPH-style demerit scorer (rubric v2)
# Spec: maint_meeting/VISIT_QC_RULES.md. Population = REGULAR ROUTE only
# (qc_visit_bundle filters to POOL MAINTENANCE / FLAT RATE, serviceable).
# Every visit starts at 100; each applicable item is Full/Half/Zero; score =
# earned / applicable * 100; letter grade A/B/C/F; critical failures force F.
# Two passes: deterministic detection + LLM note-judgment (yes/partial/no).
# Defaults = FULL RUN June 2026 (dry_run=False, all visits). Failed LLM batches
# come back in failed_visit_ids for a targeted re-run (only_visit_ids).
import json
import re
import time

import requests
import wmill
from supabase import create_client

RUBRIC = "v2"
SCORED_BY = "f/maintenance/score_visits@v2"
MODEL = "claude-haiku-4-5"

FILTER_TASKS = {"Backwashed Filter", "Cleaned Cartridges", "Cleaned Filter"}
VAC_TASKS = {"Vacuum Pool", "Vacuum Through System"}
PSI_BEFORE = ("FILTER PSI BEFORE", "Current Filter PSI")
CORE_READINGS = {"Free Chlorine", "pH", "Total Alkalinity", "Cyanuric Acid"}
SALT_RANGE = (2700, 3400)
PSI_OVER = 8

# item, weight, missing-label, exception keys
# TA & CYA are low-weight (slow-moving; recording matters, a miss barely dents).
CHEM = [("fc", 16, "Free Chlorine", ("fc_low",)),
        ("ph", 10, "pH", ("ph_high", "ph_low")),
        ("ta", 4, "Total Alkalinity", ("ta_low", "ta_high")),
        ("cya", 4, "Cyanuric Acid", ("cya_low", "cya_high")),
        ("psi", 8, "Filter PSI", ("psi_high",)),
        ("salt", 5, "Salinity", ("salt_range",))]
CHEM_LABEL = {"fc": "Free Chlorine", "ph": "pH", "ta": "Total Alkalinity",
              "cya": "CYA", "psi": "Filter PSI", "salt": "Salinity"}
# item, weight, miss-name, task names (done if any is completed)
SERVICE = [("vacbrush", 9, "Vacuum/Brush", ("Vacuum Pool", "Brushed Pool")),
           ("skimmer", 6, "Emptied Skimmer Baskets", ("Emptied Skimmer Baskets",)),
           ("pump", 6, "Emptied Pump Baskets", ("Emptied Pump Baskets",)),
           ("skimnet", 6, "Skim/Net Surface", ("Skim/Net Surface",)),
           ("cleaner", 3, "Emptied Cleaner Bag", ("Emptied Cleaner Bag",))]
SERVICE_LABEL = {"vacbrush": "Vacuum / brush", "skimmer": "Skimmer baskets",
                 "pump": "Pump baskets", "skimnet": "Skim / net", "cleaner": "Cleaner bag"}
PHOTOS_W = 16
# No standalone "notes" line — the note is what marks each reading/task
# addressed vs. not (via the LLM verdict in frac()); it's baked in everywhere.

# human-readable issue descriptions for the LLM (label, condition + right fix)
EXC_DESC = {
    "fc_low": ("Free Chlorine", "below 1 — needs shock / liquid chlorine (tabs don't count)"),
    "ph_high": ("pH", "above 7.8 — needs acid"),
    "ph_low": ("pH", "below 7.2 — needs soda ash / bicarb"),
    "ta_low": ("Total Alkalinity", "below 60 — needs bicarb"),
    "ta_high": ("Total Alkalinity", "above 120 — needs acid"),
    "cya_low": ("Cyanuric Acid", "below 30 — needs stabilizer"),
    "cya_high": ("Cyanuric Acid", "above 80 — needs dilution / note"),
    "psi_high": ("Filter PSI", "high vs baseline — needs backwash / filter clean"),
    "salt_range": ("Salinity", "out of range — needs salt"),
}


def exc_desc(key, reads):
    lbl, cond = EXC_DESC.get(key, (key, ""))
    if lbl == "Filter PSI":
        val = next((reads.get(n) for n in PSI_BEFORE if reads.get(n) is not None), None)
    else:
        val = reads.get(lbl)
    v = f" (reading {val:g})" if val is not None else ""
    return f"{lbl}{v}: {cond}"


def num(x):
    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)", str(x or ""))
    return float(m.group(1)) if m else None


def kinds_of(cons):
    k = set()
    for c in cons or []:
        n = (c.get("n") or "").lower()
        if any(s in n for s in ("shock", "cal hypo", "hypochlorite", "liquid chlorine", "liq chlor")):
            k.add("shock")
        if "tab" in n: k.add("tabs")
        if "acid" in n: k.add("acid")
        if "bicarb" in n or "alkalinity" in n: k.add("bicarb")
        if "soda ash" in n: k.add("soda_ash")
        if "stabilizer" in n or "conditioner" in n or "cyanuric" in n: k.add("stabilizer")
        if re.search(r"\bsalt\b", n) and "cell" not in n: k.add("salt")
    return k


def evaluate(v):
    reads = {}
    for r in (v["readings"] or []):
        reads.setdefault(r["n"], num(r["v"]))
    tasks = {t["n"]: bool(t["c"]) for t in (v["tasks"] or [])}
    kinds = kinds_of(v["consumables"])
    form = set(v["form_fields"] or [])
    note = (v["notes"] or "").strip()

    fc = reads.get("Free Chlorine"); ph = reads.get("pH")
    ta = reads.get("Total Alkalinity"); cya = reads.get("Cyanuric Acid")
    psi = next((reads.get(n) for n in PSI_BEFORE if reads.get(n) is not None), None)
    sal = reads.get("Salinity")
    tabs_added = ("tabs" in kinds or (reads.get("Tablets Used") or 0) > 0
                  or (reads.get("Customer Tabs (Not to be billed)") or 0) > 0)

    missing = []
    for lbl in CORE_READINGS:
        if lbl in form and reads.get(lbl) is None:
            missing.append(lbl)
    if any(n in form for n in PSI_BEFORE) and psi is None:
        missing.append("Filter PSI")
    if v["is_salt"] and "Salinity" in form and sal is None:
        missing.append("Salinity")

    exc = []  # (key, addressed, severe)
    if fc is not None and fc < 1:
        exc.append(("fc_low", "shock" in kinds, fc == 0))
    if ph is not None and ph > 7.8:
        exc.append(("ph_high", "acid" in kinds, False))
    if ph is not None and ph < 7.2:
        exc.append(("ph_low", "soda_ash" in kinds or "bicarb" in kinds, False))
    if ta is not None and ta < 60:
        exc.append(("ta_low", "bicarb" in kinds, False))
    if ta is not None and ta > 120:
        exc.append(("ta_high", "acid" in kinds, False))
    if cya is not None and cya < 30:
        exc.append(("cya_low", "stabilizer" in kinds, False))
    if cya is not None and cya > 80:
        exc.append(("cya_high", False, False))
    base = num(v["psi_baseline"])
    if psi is not None and base is not None and psi > base + PSI_OVER:
        exc.append(("psi_high", any(tasks.get(t) for t in FILTER_TASKS), False))
    if v["is_salt"] and sal is not None and not (SALT_RANGE[0] <= sal <= SALT_RANGE[1]):
        exc.append(("salt_range", "salt" in kinds, False))

    misses = [t for t in ("Emptied Skimmer Baskets", "Emptied Pump Baskets",
                          "Skim/Net Surface", "Emptied Cleaner Bag")
              if t in tasks and not tasks[t]]
    vb = [t for t in ("Vacuum Pool", "Brushed Pool") if t in tasks]
    if vb and not any(tasks[t] for t in vb):
        misses.append("Vacuum/Brush")
    if (tasks.get("Visible Algae") or tasks.get("Cloudy Water")) and \
       not any(tasks.get(t) for t in VAC_TASKS):
        misses.append("Algae/cloudy flagged but no vacuum")

    green = bool((tasks.get("Visible Algae") or tasks.get("Cloudy Water"))
                 and "shock" not in kinds and not note)
    sold_names = sorted({(c.get("n") or "").strip()
                         for c in (v["consumables"] or []) if c.get("n")})

    return {"reads": {k: x for k, x in reads.items() if x is not None},
            "kinds": sorted(kinds), "note": note, "missing": missing, "form": form,
            "is_salt": v["is_salt"], "tasks": tasks, "exceptions": exc,
            "misses": misses, "photos": v["photo_count"], "green": green,
            "sold_names": sold_names, "tabs_ok": (not v["is_tab"]) or tabs_added}


def judge_batch(client_key, items):
    prompt = (
        "You are grading pool-maintenance visit logs. Each visit gives the tech's note, the list "
        "of chemicals/products SOLD or used on the visit, and the issues found. For each issue "
        "decide whether it was addressed, using BOTH the note and what was sold.\n"
        "'yes' = clearly handled — the right product was used to treat it (e.g. shock/liquid "
        "chlorine for low chlorine, acid for high pH) and/or the note explains it or a valid "
        "reason to skip (customer-supplied chems like 'added cust acid', a 'didn't test X' "
        "admission, or a follow-up filed).\n"
        "'partial' = touched thinly — e.g. some action but not the right fix, or a vague note.\n"
        "'no' = neither treated nor explained.\n"
        "Return ONLY a JSON array: [{\"id\": ..., \"verdicts\": {\"<key>\": \"yes|partial|no\"}}].\n\n"
        + json.dumps(items, ensure_ascii=False))
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": client_key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": 2000,
                            "messages": [{"role": "user", "content": prompt}]}, timeout=120)
    r.raise_for_status()
    m = re.search(r"\[.*\]", r.json()["content"][0]["text"], re.S)
    return {row["id"]: row.get("verdicts", {}) for row in json.loads(m.group(0))}


def retry_wait(e, attempt):
    resp = getattr(e, "response", None)
    ra = resp.headers.get("retry-after") if resp is not None else None
    try:
        return min(float(ra), 120) + 1
    except (TypeError, ValueError):
        return 5 * (attempt + 1)


def score_visit(ev, verdicts):
    """DPH points: each applicable item Full(1)/Half(.5)/Zero(0)."""
    vd = verdicts or {}
    exc_by_key = {k: (addr, sev) for (k, addr, sev) in ev["exceptions"]}

    def frac(key, addressed):  # ok=1 / thin=.5 / bad=0
        j = vd.get(key)
        if addressed:
            return 1.0 if j == "yes" else 0.5
        return 1.0 if j == "yes" else 0.5 if j == "partial" else 0.0

    applicable = 0.0
    earned = 0.0
    items = []
    chem_e = svc_e = doc_e = 0.0

    # chemistry
    for key, w, miss_lbl, exkeys in CHEM:
        if key == "salt":
            appl = ev["is_salt"] and "Salinity" in ev["form"]
        elif key == "psi":
            appl = any(n in ev["form"] for n in PSI_BEFORE)
        else:
            appl = miss_lbl in ev["form"]
        if not appl:
            continue
        applicable += w
        if miss_lbl in ev["missing"]:
            f, why = 0.0, "not recorded"
        else:
            hit = next((k for k in exkeys if k in exc_by_key), None)
            if hit is None:
                f, why = 1.0, "in range"
            else:
                addr, _ = exc_by_key[hit]
                f = frac(hit, addr)
                why = "off · " + ("addressed" if f == 1 else "corrected, not noted" if f == .5 else "unaddressed")
        earned += w * f; chem_e += w * f
        items.append({"k": CHEM_LABEL[key], "w": w, "f": f, "why": why})

    # service
    for key, w, miss_name, tnames in SERVICE:
        if not any(t in ev["tasks"] for t in tnames):
            continue
        applicable += w
        if any(ev["tasks"].get(t) for t in tnames):
            f, why = 1.0, "done"
        else:
            f = frac("miss:" + miss_name, False)
            why = "done/explained" if f == 1 else "explained thinly" if f == .5 else "missed, unexplained"
        earned += w * f; svc_e += w * f
        items.append({"k": SERVICE_LABEL[key], "w": w, "f": f, "why": why})

    # documentation: photos (note quality is already scored via each item's addressed-ness)
    p = ev["photos"]
    pf = 1.0 if p >= 2 else 0.5 if p == 1 else 0.0
    applicable += PHOTOS_W; earned += PHOTOS_W * pf; doc_e += PHOTOS_W * pf
    items.append({"k": "Photos", "w": PHOTOS_W, "f": pf, "why": f"{p} photo(s)"})

    score = round(earned / applicable * 100, 1) if applicable else 0.0

    # critical failures
    criticals = []
    fc_hit = exc_by_key.get("fc_low")
    if fc_hit is not None and frac("fc_low", fc_hit[0]) == 0.0:
        criticals.append("No sanitizer — free chlorine below 1, untreated")
    if ev["green"]:
        criticals.append("Unsafe water (algae/cloudy) untreated, no note")

    grade = ("F" if criticals or score < 70 else
             "A" if score >= 90 else "B" if score >= 80 else "C")
    return score, grade, round(chem_e), round(svc_e), round(doc_e), items, criticals


def main(p_start: str = "2026-06-01", p_end: str = "2026-06-30",
         dry_run: bool = False, max_visits: int = 0, skip_llm: bool = False,
         only_visit_ids: list = ["3a89ddf2-8b5d-4490-a689-31d19cf41a76","e02917ac-0537-4dce-a643-7c3141685dc9","5548064b-2389-4604-94ff-37c448629d2f","43587891-007c-4f4c-a5d4-8dc1582dfd19","e90adeb5-81d6-4433-b9bd-e4105f12a6fd","55dc7062-88b8-428c-b8fa-ce3523499786","c281c302-6d9e-44c3-9d31-8a41bb3ad198","61d802c9-dc5c-48e2-b0b1-4b3a022c33de","5cead687-0fbe-45d0-b3ba-3fd5a21cc96e","4d21f701-95e7-4a3b-b2cc-c7aa887223d5","695954c8-acef-4c5b-bda8-4b66ffabbe68","289a6000-f646-466d-a71d-eb048ea4cdc6","73a059e2-0eaa-4f8d-a43f-4a639930c2bb","a51febe2-e457-446e-837d-fcfce08fb27a","95e9718b-db95-470b-9a77-966493a565c9","50cd10f5-3c22-4de1-b779-21801f1b7758","86678022-316b-4e37-aad9-654973a1b258","cafb950e-eea2-45c3-bd8b-bc0f425a7c78","da4e62c9-de7a-41ac-ac35-7b57a8476f83","4753b39b-7625-4a9a-9ff2-13244c35b88b","fa2b5149-7355-4cbf-82e4-2d2440e824da","107e4f98-eadf-4079-a2d5-f8da4cbf44ad","5b51d6f1-041e-489f-b3d6-d90760c496ac"]):  # ponytail: temp scope to Justin Powell recovery run; revert to None after
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"),
                       wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))
    akey = wmill.get_variable("f/service_billing/ANTHROPIC_API_KEY")

    visits, off = [], 0
    while True:
        page = sb.rpc("qc_visit_bundle", {"p_start": p_start, "p_end": p_end,
                                          "p_limit": 400, "p_offset": off}).execute().data
        visits.extend(page); off += 400
        if len(page) < 400 or (max_visits and len(visits) >= max_visits):
            break
    if max_visits:
        visits = visits[:max_visits]
    if only_visit_ids:
        want = set(only_visit_ids); visits = [v for v in visits if v["visit_id"] in want]

    evs = {v["visit_id"]: evaluate(v) for v in visits}

    judge_items = []
    for vid, ev in evs.items():
        pend = [{"key": k, "desc": exc_desc(k, ev["reads"])} for (k, a, s) in ev["exceptions"]] + \
               [{"key": "miss:" + m, "desc": "checklist item not done: " + m} for m in ev["misses"]]
        if pend and ev["note"]:
            judge_items.append({"id": vid, "note": ev["note"][:600],
                                "sold": ev["sold_names"], "items": pend})
    verdicts, llm_calls, failed = {}, 0, []
    if judge_items and not skip_llm:
        for i in range(0, len(judge_items), 12):
            batch = judge_items[i:i + 12]
            for attempt in range(5):
                try:
                    verdicts.update(judge_batch(akey, batch)); llm_calls += 1; break
                except requests.HTTPError as e:
                    code = getattr(e.response, "status_code", None)
                    body = getattr(e.response, "text", "") or ""
                    if code == 400 and "credit balance" in body:
                        raise RuntimeError(
                            "ANTHROPIC CREDITS EXHAUSTED — top up f/service_billing/"
                            "ANTHROPIC_API_KEY, then re-run (upsert makes it idempotent).") from e
                    if code == 400:  # malformed request won't fix on retry
                        print(f"batch {i}: 400 {body[:120]}")
                        failed.extend(it["id"] for it in batch); break
                    if attempt == 4:
                        print(f"batch {i} failed: {e}"); failed.extend(it["id"] for it in batch)
                    else:
                        time.sleep(retry_wait(e, attempt))
                except Exception as e:
                    if attempt == 4:
                        print(f"batch {i} failed: {e}"); failed.extend(it["id"] for it in batch)
                    else:
                        time.sleep(retry_wait(e, attempt))
            time.sleep(1)

    failset = set(failed)
    rows, gdist = [], {}
    for v in visits:
        if v["visit_id"] in failset:
            continue
        ev = evs[v["visit_id"]]
        score, grade, chem_e, svc_e, doc_e, items, crit = score_visit(ev, verdicts.get(v["visit_id"]))
        gdist[grade] = gdist.get(grade, 0) + 1
        rows.append({
            "visit_id": v["visit_id"], "rubric_version": RUBRIC,
            "checklist_score": svc_e, "chemicals_score": chem_e, "documentation_score": doc_e,
            "visit_score": score, "grade": grade,
            "detail": {"tech": v["tech_name"], "date": v["visit_date"],
                       "photos": ev["photos"], "readings": ev["reads"], "sold_kinds": ev["kinds"],
                       "grade": grade, "score": score, "criticals": crit, "items": items,
                       "note_present": bool(ev["note"])},
            "scored_by": SCORED_BY + " " + MODEL})

    written = 0
    if not dry_run:
        for i in range(0, len(rows), 300):
            written += sb.rpc("qc_upsert_scores", {"p": rows[i:i + 300]}).execute().data

    avg = round(sum(r["visit_score"] for r in rows) / max(len(rows), 1), 1)
    return {"visits": len(rows), "written": written, "dry_run": dry_run,
            "avg_score": avg, "grade_dist": gdist, "llm_batches": llm_calls,
            "needed_judgment": len(judge_items), "failed_visit_ids": failed,
            "sample": rows[:3] if dry_run else None}

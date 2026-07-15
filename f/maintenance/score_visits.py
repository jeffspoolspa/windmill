# f/maintenance/score_visits — see VISIT_QC_RULES.md (rubric v1)
# PILOT defaults: dry_run=True, max_visits=250. Flip for the full run.
import json
import re
import time

import requests
import wmill
from supabase import create_client

RUBRIC = "v1"
SCORED_BY = "f/maintenance/score_visits@" + RUBRIC
MODEL = "claude-haiku-4-5"

EXPECTED_TASKS = {"Vacuum Pool", "Brushed Pool", "Emptied Skimmer Baskets",
                  "Emptied Pump Baskets", "Skim/Net Surface", "Emptied Cleaner Bag"}
FILTER_TASKS = {"Backwashed Filter", "Cleaned Cartridges", "Cleaned Filter"}
VAC_TASKS = {"Vacuum Pool", "Vacuum Through System"}
PSI_BEFORE = ("FILTER PSI BEFORE", "Current Filter PSI")
CORE_READINGS = {"fc": "Free Chlorine", "ph": "pH", "ta": "Total Alkalinity",
                 "cya": "Cyanuric Acid"}
SALT_RANGE = (2700, 3400)
PSI_OVER = 8


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
    # tabs "added" = sold OR recorded on the form (Tablets Used / customer-supplied tabs)
    tabs_added = ("tabs" in kinds
                  or (reads.get("Tablets Used") or 0) > 0
                  or (reads.get("Customer Tabs (Not to be billed)") or 0) > 0)

    missing = [lbl for key, lbl in CORE_READINGS.items()
               if lbl in form and reads.get(lbl) is None]
    if any(n in form for n in PSI_BEFORE) and psi is None:
        missing.append("Filter PSI")
    if "FILTER PSI AFTER" in form and reads.get("FILTER PSI AFTER") is None:
        missing.append("Filter PSI after")
    if v["is_salt"] and "Salinity" in form and sal is None:
        missing.append("Salinity")

    exc = []
    if fc is not None and fc < 1:
        exc.append(("fc_low", f"Free chlorine {fc:g} (<1) — needs shock (cal hypo/liquid); tabs alone don't count",
                    "shock" in kinds, fc == 0))
    if v["is_tab"] and not tabs_added:
        exc.append(("tabs_skipped", "Tablet pool but no tabs added this visit — fine only if noted (CYA high / chlorinator stocked)",
                    False, False))
    if ph is not None and ph > 7.8:
        exc.append(("ph_high", f"pH {ph:g} (>7.8) — needs acid (may be on-site cust acid; note counts)",
                    "acid" in kinds, False))
    if ph is not None and ph < 7.2:
        exc.append(("ph_low", f"pH {ph:g} (<7.2) — needs soda ash/bicarb",
                    "soda_ash" in kinds or "bicarb" in kinds, False))
    if ta is not None and ta < 60:
        exc.append(("ta_low", f"TA {ta:g} (<60) — needs bicarb", "bicarb" in kinds, False))
    if ta is not None and ta > 120:
        exc.append(("ta_high", f"TA {ta:g} (>120) — needs acid + note", "acid" in kinds, False))
    if cya is not None and cya < 30:
        exc.append(("cya_low", f"CYA {cya:g} (<30) — needs stabilizer",
                    "stabilizer" in kinds, False))
    if cya is not None and cya > 80:
        exc.append(("cya_high", f"CYA {cya:g} (>80) — needs note (dilution plan)", False, False))
    base = num(v["psi_baseline"])
    if psi is not None and base is not None and psi > base + PSI_OVER:
        exc.append(("psi_high", f"Filter PSI {psi:g} vs pool baseline {base:g} — needs backwash/filter clean or note",
                    any(tasks.get(t) for t in FILTER_TASKS), False))
    if v["is_salt"] and sal is not None and not (SALT_RANGE[0] <= sal <= SALT_RANGE[1]):
        exc.append(("salt_range", f"Salinity {sal:g} outside {SALT_RANGE[0]}-{SALT_RANGE[1]} — needs salt or note",
                    "salt" in kinds, False))

    misses = [t for t in EXPECTED_TASKS if t in tasks and not tasks[t]]
    if (tasks.get("Visible Algae") or tasks.get("Cloudy Water")) and \
       not any(tasks.get(t) for t in VAC_TASKS):
        misses.append("Algae/cloudy flagged but no vacuum")

    return {"reads": {k: v2 for k, v2 in reads.items() if v2 is not None},
            "kinds": sorted(kinds), "note": note, "missing": missing,
            "exceptions": exc, "misses": misses, "photos": v["photo_count"]}


def judge_batch(client_key, items):
    prompt = (
        "You are grading pool-maintenance visit logs. For each visit, decide whether the tech's "
        "note explains each listed exception. 'yes' = the note clearly explains or resolves it "
        "(names the issue and what was done, or why it was intentionally skipped, incl. customer-"
        "supplied chems like 'added cust acid', or a follow-up filed). 'partial' = the note "
        "touches it but thinly. 'no' = the note doesn't address it.\n"
        "Return ONLY a JSON array: [{\"id\": ..., \"verdicts\": {\"<key>\": \"yes|partial|no\"}}].\n\n"
        + json.dumps(items, ensure_ascii=False))
    r = requests.post("https://api.anthropic.com/v1/messages",
                      headers={"x-api-key": client_key, "anthropic-version": "2023-06-01",
                               "content-type": "application/json"},
                      json={"model": MODEL, "max_tokens": 2000,
                            "messages": [{"role": "user", "content": prompt}]},
                      timeout=120)
    r.raise_for_status()
    txt = r.json()["content"][0]["text"]
    m = re.search(r"\[.*\]", txt, re.S)
    out = {}
    for row in json.loads(m.group(0)):
        out[row["id"]] = row.get("verdicts", {})
    return out


def finalize(ev, verdicts):
    vd = verdicts or {}

    def status(key, addressed):
        j = vd.get(key)
        if addressed:
            return "ok" if j == "yes" else "thin"
        if j == "yes": return "ok"
        if j == "partial": return "thin"
        return "bad"

    miss_status = [status("miss:" + m, False) for m in ev["misses"]]
    eff = sum(1 for s in miss_status if s == "bad") + 0.5 * sum(1 for s in miss_status if s == "thin")
    checklist = 4 if eff == 0 else 3 if eff <= 1 else 2 if eff <= 2 else 1

    chem_keys = [(k, d, a, sev) for (k, d, a, sev) in ev["exceptions"]]
    chem_status = {k: status(k, a) for (k, d, a, sev) in chem_keys}
    severe_bad = any(sev and chem_status[k] == "bad" for (k, d, a, sev) in chem_keys)
    any_bad = any(s == "bad" for s in chem_status.values())
    any_thin = any(s == "thin" for s in chem_status.values())
    if severe_bad: chem = 1
    elif any_bad: chem = 2
    elif any_thin: chem = 3
    else: chem = 4
    if ev["missing"]: chem = min(chem, 2)

    to_explain = {**{k: s for k, s in chem_status.items()},
                  **{"miss:" + m: s for m, s in zip(ev["misses"], miss_status)}}
    unexplained = any(s == "bad" for s in to_explain.values())
    thin = any(s == "thin" for s in to_explain.values())
    p = ev["photos"]
    if p == 0:
        docs = 1 if unexplained else 2
    elif p < 3:
        docs = 2 if unexplained else 3
    else:
        docs = 2 if unexplained else 3 if thin else 4

    total = round((checklist + chem + docs) / 3, 2)
    return checklist, chem, docs, total, chem_status, miss_status


def main(p_start: str = "2026-06-01", p_end: str = "2026-06-30",
         dry_run: bool = True, max_visits: int = 250, skip_llm: bool = False):
    sb = create_client(wmill.get_variable("f/SUPABASE/URL"),
                       wmill.get_variable("f/SUPABASE/SERVICE_ROLE_KEY"))
    akey = wmill.get_variable("f/service_billing/ANTHROPIC_API_KEY")

    visits, off = [], 0
    while True:
        page = sb.rpc("qc_visit_bundle", {"p_start": p_start, "p_end": p_end,
                                          "p_limit": 400, "p_offset": off}).execute().data
        visits.extend(page)
        off += 400
        if len(page) < 400 or (max_visits and len(visits) >= max_visits):
            break
    if max_visits:
        visits = visits[:max_visits]

    evs = {v["visit_id"]: evaluate(v) for v in visits}

    judge_items = []
    for vid, ev in evs.items():
        pend = [{"key": k, "desc": d} for (k, d, a, s) in ev["exceptions"]] + \
               [{"key": "miss:" + m, "desc": "Checklist item not done: " + m} for m in ev["misses"]]
        if pend and ev["note"]:
            judge_items.append({"id": vid, "note": ev["note"][:600], "items": pend})
    verdicts = {}
    llm_calls = 0
    if judge_items and not skip_llm:
        for i in range(0, len(judge_items), 12):
            batch = judge_items[i:i + 12]
            for attempt in range(3):
                try:
                    verdicts.update(judge_batch(akey, batch))
                    llm_calls += 1
                    break
                except Exception as e:
                    if attempt == 2:
                        print(f"batch {i} failed: {e}")
                    time.sleep(5 * (attempt + 1))

    rows, dist = [], {}
    for v in visits:
        ev = evs[v["visit_id"]]
        c, ch, d, tot, chem_status, miss_status = finalize(ev, verdicts.get(v["visit_id"]))
        dist[tot] = dist.get(tot, 0) + 1
        rows.append({
            "visit_id": v["visit_id"], "rubric_version": RUBRIC,
            "checklist_score": c, "chemicals_score": ch, "documentation_score": d,
            "visit_score": tot,
            "detail": {"tech": v["tech_name"], "date": v["visit_date"],
                       "photos": ev["photos"], "readings": ev["reads"],
                       "sold_kinds": ev["kinds"], "missing_readings": ev["missing"],
                       "exceptions": [{"key": k, "desc": dd, "addressed": a,
                                       "status": chem_status.get(k)}
                                      for (k, dd, a, s) in ev["exceptions"]],
                       "checklist_misses": [{"item": m, "status": s}
                                            for m, s in zip(ev["misses"], miss_status)],
                       "note_present": bool(ev["note"]),
                       "llm_judged": v["visit_id"] in verdicts},
            "scored_by": SCORED_BY + " " + MODEL,
        })

    written = 0
    if not dry_run:
        for i in range(0, len(rows), 300):
            written += sb.rpc("qc_upsert_scores",
                              {"p": rows[i:i + 300]}).execute().data

    avg = round(sum(r["visit_score"] for r in rows) / max(len(rows), 1), 2)
    return {"visits": len(rows), "written": written, "dry_run": dry_run,
            "avg_visit_score": avg, "llm_batches": llm_calls,
            "needed_judgment": len(judge_items),
            "score_histogram": {str(k): dist[k] for k in sorted(dist)},
            "sample": rows[:3] if dry_run else None}

# f/maintenance/rubric — Visit QC rubric v3 (pure domain module, no I/O).
# Spec: maint_meeting/VISIT_QC_RULES.md. The runner (score_visits) feeds it
# visit bundles + LLM verdicts; this module owns every scoring rule.
#
# v3 (Carter, 2026-08-13): two flat-value categories.
#   Core readings 10 pts each: FC, pH, TA, CYA, Filter PSI, Salinity (salt).
#   Checklist 5 pts each: vac/brush, skimmer, pump baskets, skim/net.
#     (cleaner bag dropped from the rubric)
#   Photos 15: 2+ full / 1 half / 0 zero.
# Credit ladder: in range/done = FULL; off/missing/skipped but the NOTE
# addresses it (binary judge) = FULL; chem reading off + right product used,
# no note = HALF (chem readings only); everything else = ZERO.
# PSI: flat line at 25 — under = full; 25+ = full only via backwash/cartridge
# task (self-documenting) or a note; no half. Criticals & A/B/C/F unchanged.
import re

RUBRIC = "v3"
W_CHECK, W_PHOTOS = 5, 15
# per-reading weights (Carter 2026-08-13): sanitizer/comfort readings heavy,
# slow-drift readings light
W_READ = {"fc": 15, "ph": 15, "ta": 5, "cya": 5, "psi": 10, "salt": 10}
PSI_HIGH = 25
PSI_LOW = 5    # under 5 = gauge/pump problem — needs a note (backwash can't fix low)
SALT_RANGE = (2700, 3400)

# Service types where the cleaning checklist doesn't apply (spas, fountains,
# splash features, chem checks) even though the tasks appear on the form —
# readings + photos only, normalization handles the rest. (Carter 2026-08-13:
# kiddie/baby pools, lazy rivers, zero-depth entries stay full-checklist.)
NO_CHECKLIST_RE = re.compile(r"spa|hot ?tub|fountain|splash|sprayground|chem ?check",
                             re.IGNORECASE)

FILTER_TASKS = {"Backwashed Filter", "Cleaned Cartridges", "Cleaned Filter"}
VAC_TASKS = {"Vacuum Pool", "Vacuum Through System"}
PSI_BEFORE = ("FILTER PSI BEFORE", "Current Filter PSI")
CORE_READINGS = {"Free Chlorine", "pH", "Total Alkalinity", "Cyanuric Acid"}

# key, form label, exception keys. Chem keys (not psi) are half-credit eligible.
CHEM = [("fc", "Free Chlorine", ("fc_low",)),
        ("ph", "pH", ("ph_high", "ph_low")),
        ("ta", "Total Alkalinity", ("ta_low", "ta_high")),
        ("cya", "Cyanuric Acid", ("cya_low", "cya_high")),
        ("psi", "Filter PSI", ("psi_high", "psi_low")),
        ("salt", "Salinity", ("salt_range",))]
CHEM_LABEL = {"fc": "Free Chlorine", "ph": "pH", "ta": "Total Alkalinity",
              "cya": "CYA", "psi": "Filter PSI", "salt": "Salinity"}
HALF_ELIGIBLE = {"fc", "ph", "ta", "cya", "salt"}   # chem readings only — not psi

SERVICE = [("vacbrush", "Vacuum/Brush", ("Vacuum Pool", "Brushed Pool")),
           ("skimmer", "Emptied Skimmer Baskets", ("Emptied Skimmer Baskets",)),
           ("pump", "Emptied Pump Baskets", ("Emptied Pump Baskets",)),
           ("skimnet", "Skim/Net Surface", ("Skim/Net Surface",))]
SERVICE_LABEL = {"vacbrush": "Vacuum / brush", "skimmer": "Skimmer baskets",
                 "pump": "Pump baskets", "skimnet": "Skim / net"}

EXC_DESC = {
    "fc_low": ("Free Chlorine", "below 1 — needs shock / liquid chlorine (tabs don't count)"),
    "ph_high": ("pH", "above 7.8 — needs acid"),
    "ph_low": ("pH", "below 7.2 — needs soda ash / bicarb"),
    "ta_low": ("Total Alkalinity", "below 60 — needs bicarb"),
    "ta_high": ("Total Alkalinity", "above 120 — needs acid"),
    "cya_low": ("Cyanuric Acid", "below 30 — needs stabilizer"),
    "cya_high": ("Cyanuric Acid", "above 80 — needs dilution / note"),
    "psi_high": ("Filter PSI", f"at or above {PSI_HIGH} — needs backwash / filter clean"),
    "psi_low": ("Filter PSI", f"under {PSI_LOW} — gauge or pump problem, needs a note"),
    "salt_range": ("Salinity", "out of range — needs salt"),
}

JUDGE_PROMPT = (
    "You are grading pool-maintenance visit logs. Each visit gives the tech's note, the list "
    "of chemicals/products SOLD or used on the visit, and the issues found. For each issue "
    "answer a single question: does the note (with the sold list as context) ADDRESS it?\n"
    "'yes' = the note explains the fix, gives a valid reason it was skipped, admits a reading "
    "wasn't taken, references customer-supplied chemicals, or says a follow-up was filed — "
    "for THAT specific issue.\n"
    "'no' = the note does not speak to that issue (vague notes and unrelated notes are 'no').\n"
    "Return ONLY a JSON array: [{\"id\": ..., \"verdicts\": {\"<key>\": \"yes|no\"}}]."
)


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


def exc_desc(key, reads):
    lbl, cond = EXC_DESC.get(key, (key, ""))
    if lbl == "Filter PSI":
        val = next((reads.get(n) for n in PSI_BEFORE if reads.get(n) is not None), None)
    else:
        val = reads.get(lbl)
    v = f" (reading {val:g})" if val is not None else ""
    return f"{lbl}{v}: {cond}"


def evaluate(v):
    """Deterministic pass: readings, misses, exceptions, product matches."""
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

    missing = []
    for lbl in CORE_READINGS:
        if lbl in form and reads.get(lbl) is None:
            missing.append(lbl)
    if any(n in form for n in PSI_BEFORE) and psi is None:
        missing.append("Filter PSI")
    if v["is_salt"] and "Salinity" in form and sal is None:
        missing.append("Salinity")

    exc = []  # (key, product_used)
    if fc is not None and fc < 1:
        exc.append(("fc_low", "shock" in kinds))
    if ph is not None and ph > 7.8:
        exc.append(("ph_high", "acid" in kinds))
    if ph is not None and ph < 7.2:
        exc.append(("ph_low", "soda_ash" in kinds or "bicarb" in kinds))
    if ta is not None and ta < 60:
        exc.append(("ta_low", "bicarb" in kinds))
    if ta is not None and ta > 120:
        exc.append(("ta_high", "acid" in kinds))
    if cya is not None and cya < 30:
        exc.append(("cya_low", "stabilizer" in kinds))
    if cya is not None and cya > 80:
        exc.append(("cya_high", False))
    if psi is not None and psi >= PSI_HIGH:
        exc.append(("psi_high", any(tasks.get(t) for t in FILTER_TASKS)))
    if psi is not None and psi < PSI_LOW:
        exc.append(("psi_low", False))   # only a note can save a too-low PSI
    if v["is_salt"] and sal is not None and not (SALT_RANGE[0] <= sal <= SALT_RANGE[1]):
        exc.append(("salt_range", "salt" in kinds))

    no_checklist = bool(NO_CHECKLIST_RE.search(v.get("service_type") or ""))
    if no_checklist:
        misses = []
    else:
        misses = [t for t in ("Emptied Skimmer Baskets", "Emptied Pump Baskets", "Skim/Net Surface")
                  if t in tasks and not tasks[t]]
        vb = [t for t in ("Vacuum Pool", "Brushed Pool") if t in tasks]
        if vb and not any(tasks[t] for t in vb):
            misses.append("Vacuum/Brush")

    green = bool((tasks.get("Visible Algae") or tasks.get("Cloudy Water"))
                 and "shock" not in kinds and not note)
    sold_names = sorted({(c.get("n") or "").strip()
                         for c in (v["consumables"] or []) if c.get("n")})

    return {"reads": {k: x for k, x in reads.items() if x is not None},
            "kinds": sorted(kinds), "note": note, "missing": missing, "form": form,
            "is_salt": v["is_salt"], "tasks": tasks, "exceptions": exc,
            "misses": misses, "photos": v["photo_count"], "green": green,
            "sold_names": sold_names, "no_checklist": no_checklist}


def judge_items(ev):
    """Issues the (binary) note-judge should rule on. Empty if no note."""
    if not ev["note"]:
        return []
    items = [{"key": k, "desc": exc_desc(k, ev["reads"])} for (k, _p) in ev["exceptions"]]
    items += [{"key": "missing:" + m, "desc": f"{m} was not recorded at all"} for m in ev["missing"]]
    items += [{"key": "miss:" + m, "desc": "checklist item not done: " + m} for m in ev["misses"]]
    return items


def score_visit(ev, verdicts):
    """v3 ladder: FULL (right / note-addressed) · HALF (chem product, no note) · ZERO."""
    vd = verdicts or {}
    exc_by_key = {k: p for (k, p) in ev["exceptions"]}
    yes = lambda key: vd.get(key) == "yes"

    applicable = earned = 0.0
    items = []
    chem_e = svc_e = doc_e = 0.0

    for key, form_lbl, exkeys in CHEM:
        if key == "salt":
            appl = ev["is_salt"] and "Salinity" in ev["form"]
        elif key == "psi":
            appl = any(n in ev["form"] for n in PSI_BEFORE)
        else:
            appl = form_lbl in ev["form"]
        if not appl:
            continue
        w = W_READ[key]
        applicable += w
        miss_lbl = "Filter PSI" if key == "psi" else ("Salinity" if key == "salt" else form_lbl)
        if miss_lbl in ev["missing"]:
            f = 1.0 if yes("missing:" + miss_lbl) else 0.0
            why = "not recorded — explained" if f else "not recorded"
        else:
            hit = next((k for k in exkeys if k in exc_by_key), None)
            if hit is None:
                f, why = 1.0, "in range"
            elif key == "psi":
                f = 1.0 if (exc_by_key[hit] or yes(hit)) else 0.0
                if hit == "psi_low":
                    why = "under 5 — explained" if f else "under 5 — not addressed"
                else:
                    why = ("high — backwashed" if exc_by_key[hit] else
                           "high — explained" if f else "high — no backwash, no note")
            else:
                if yes(hit):
                    f, why = 1.0, "off — addressed in note"
                elif exc_by_key[hit] and key in HALF_ELIGIBLE:
                    f, why = 0.5, "off — treated, but not noted"
                else:
                    f, why = 0.0, "off — not addressed"
        earned += w * f; chem_e += w * f
        items.append({"k": CHEM_LABEL[key], "w": w, "f": f, "why": why})

    for key, miss_name, tnames in SERVICE:
        if ev.get("no_checklist"):
            break   # spas / fountains / chem checks: readings + photos only
        if not any(t in ev["tasks"] for t in tnames):
            continue
        applicable += W_CHECK
        if any(ev["tasks"].get(t) for t in tnames):
            f, why = 1.0, "done"
        else:
            f = 1.0 if yes("miss:" + miss_name) else 0.0
            why = "skipped — explained" if f else "not done"
        earned += W_CHECK * f; svc_e += W_CHECK * f
        items.append({"k": SERVICE_LABEL[key], "w": W_CHECK, "f": f, "why": why})

    p = ev["photos"]
    pf = 1.0 if p >= 2 else 0.5 if p == 1 else 0.0
    applicable += W_PHOTOS; earned += W_PHOTOS * pf; doc_e += W_PHOTOS * pf
    items.append({"k": "Photos", "w": W_PHOTOS, "f": pf, "why": f"{p} photo(s)"})

    score = round(earned / applicable * 100, 1) if applicable else 0.0

    criticals = []
    fc_hit = exc_by_key.get("fc_low")
    if fc_hit is not None and not (fc_hit or yes("fc_low")):
        criticals.append("No sanitizer — free chlorine below 1, untreated")
    if ev["green"]:
        criticals.append("Unsafe water (algae/cloudy) untreated, no note")

    grade = ("F" if criticals or score < 70 else
             "A" if score >= 90 else "B" if score >= 80 else "C")
    return score, grade, round(chem_e), round(svc_e), round(doc_e), items, criticals


def demo():
    """Self-check: one synthetic visit exercising every credit tier."""
    v = {"readings": [{"n": "Free Chlorine", "v": "0"}, {"n": "pH", "v": "8.0"},
                      {"n": "Total Alkalinity", "v": "50"}, {"n": "Cyanuric Acid", "v": "40"},
                      {"n": "FILTER PSI BEFORE", "v": "30"}],
         "tasks": [{"n": "Vacuum Pool", "c": True}, {"n": "Brushed Pool", "c": False},
                   {"n": "Emptied Skimmer Baskets", "c": True},
                   {"n": "Emptied Pump Baskets", "c": False},
                   {"n": "Skim/Net Surface", "c": True},
                   {"n": "Backwashed Filter", "c": True}],
         "consumables": [{"n": "LIQUID CHLORINE 2.5GAL"}, {"n": "SODIUM BICARB 1LB"}],
         "form_fields": ["Free Chlorine", "pH", "Total Alkalinity", "Cyanuric Acid",
                         "FILTER PSI BEFORE"],
         "notes": "0 chlorine on arrival, shocked and retested — holding.",
         "is_salt": False, "is_tab": False, "photo_count": 1, "psi_baseline": None}
    ev = evaluate(v)
    verdicts = {"fc_low": "yes", "ph_high": "no", "ta_low": "no",
                "miss:Emptied Pump Baskets": "no"}
    score, grade, *_rest, items, crit = score_visit(ev, verdicts)
    by = {i["k"]: i["f"] for i in items}
    assert by["Free Chlorine"] == 1.0        # off, note addressed -> full
    assert by["pH"] == 0.0                   # off, no product, note doesn't cover -> zero
    assert by["Total Alkalinity"] == 0.5     # off, bicarb used, not noted -> half (chem only)
    assert by["CYA"] == 1.0                  # in range
    assert by["Filter PSI"] == 1.0           # 30 >= 25 but backwashed -> full, no note needed
    assert by["Vacuum / brush"] == 1.0 and by["Pump baskets"] == 0.0
    assert by["Photos"] == 0.5 and not crit
    # chem 15+0+2.5+5+10=32.5 + svc 5+5+0+5=15 + photos 7.5 = 55 / 85 = 64.7 F
    assert (score, grade) == (64.7, "F"), (score, grade)

    # spa: checklist not applicable; PSI 2 (under 5) unexplained -> zero
    v2 = dict(v, service_type="POOL MAINTENANCE 45 SPA")
    v2["readings"] = [{"n": "Free Chlorine", "v": "3"}, {"n": "pH", "v": "7.4"},
                      {"n": "FILTER PSI BEFORE", "v": "2"}]
    v2["form_fields"] = ["Free Chlorine", "pH", "FILTER PSI BEFORE"]
    ev2 = evaluate(v2)
    assert ev2["no_checklist"] and ev2["misses"] == []
    assert ("psi_low", False) in ev2["exceptions"]
    s2, g2, *_r2, items2, crit2 = score_visit(ev2, {})
    by2 = {i["k"]: i for i in items2}
    assert "Vacuum / brush" not in by2 and "Skim / net" not in by2
    assert by2["Filter PSI"]["f"] == 0.0 and by2["Filter PSI"]["why"] == "under 5 — not addressed"
    # chem 15+15+0 = 30 + photos 7.5 = 37.5 / 55 applicable = 68.2 F
    assert (s2, g2) == (68.2, "F"), (s2, g2)
    print("rubric v3 self-check OK:", score, grade, "| spa/psi-low:", s2, g2)


if __name__ == "__main__":
    demo()

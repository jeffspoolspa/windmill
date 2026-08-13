# f/maintenance/score_visits — thin runner for the visit-QC rubric.
# ALL scoring rules live in f/maintenance/rubric (pure domain module, v3).
# This job: fetch visit bundles, batch the binary note-judge (Haiku), apply the
# rubric, upsert into maintenance.visit_scores. Idempotent on (visit_id,
# rubric_version); defaults = current month to date (ET) for the nightly cron.
import json
import re
import time

import requests
import wmill

from f.maintenance.rubric import (RUBRIC, JUDGE_PROMPT, evaluate, judge_items,
                                  score_visit)
from supabase import create_client

SCORED_BY = f"f/maintenance/score_visits@{RUBRIC}"
MODEL = "claude-haiku-4-5"


def judge_batch(client_key, items):
    prompt = JUDGE_PROMPT + "\n\n" + json.dumps(items, ensure_ascii=False)
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


def main(p_start: str = "", p_end: str = "",
         dry_run: bool = False, max_visits: int = 0, skip_llm: bool = False,
         only_visit_ids: list = None):
    if not p_start:
        from datetime import datetime, timedelta, timezone
        today = datetime.now(timezone(timedelta(hours=-4))).date()
        p_start = str(today.replace(day=1))
        p_end = str(today)

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

    to_judge = []
    for vid, ev in evs.items():
        pend = judge_items(ev)
        if pend:
            to_judge.append({"id": vid, "note": ev["note"][:600],
                             "sold": ev["sold_names"], "items": pend})
    verdicts, llm_calls, failed = {}, 0, []
    if to_judge and not skip_llm:
        for i in range(0, len(to_judge), 12):
            batch = to_judge[i:i + 12]
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
                    if code == 400:
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
            "rubric": RUBRIC, "avg_score": avg, "grade_dist": gdist,
            "llm_batches": llm_calls, "needed_judgment": len(to_judge),
            "failed_visit_ids": failed}

# requirements:
# requests
# beautifulsoup4
# psycopg2-binary
# wmill
import json, re
from datetime import date as _date, datetime
import requests, wmill
from bs4 import BeautifulSoup
from f.ION._lib.upsert import _connect

CUST = "2367390"; SL = 8493; START = "2026-05-01"; END = "2026-06-01"


def _combine(d, t):
    try:
        dd = _date.fromisoformat(d)
        tt = datetime.strptime(t.strip(), "%I:%M %p").time()
        return datetime.combine(dd, tt).isoformat()
    except Exception:
        return None


def main(dry_run: bool = True):
    sb = wmill.get_resource("u/carter/supabase")
    sess = wmill.get_variable("f/ION/session_cache")
    if isinstance(sess, str):
        sess = json.loads(sess)
    origin = sess["ionOrigin"]
    host = origin.split("//")[1].split("/")[0]
    parts = []
    for c in sess["cookies"]:
        dmn = (c.get("domain") or "").lstrip(".")
        if host == dmn or host.endswith("." + dmn):
            parts.append(f"{c['name']}={c['value']}")
    cookie = "; ".join(parts)
    H = {"Cookie": cookie, "User-Agent": "Mozilla/5.0", "Accept": "text/html, */*"}
    PH = {**H, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "X-Requested-With": "XMLHttpRequest", "Referer": f"{origin}/main.cfm", "Origin": origin}

    requests.get(f"{origin}/customers/customerTabs.cfm?customerid={CUST}", headers=H, allow_redirects=False, timeout=60)
    listr = requests.post(f"{origin}/customers/logs/loglist.cfm", headers=PH, data="limit=400", allow_redirects=False, timeout=90)
    soup = BeautifulSoup(listr.text, "html.parser")
    logs = []
    for a in soup.find_all("a", href=re.compile("addLog.cfm")):
        m = re.search(r"LogID=(\d+)", a.get("href", ""))
        dm = re.search(r"(\d{2})/(\d{2})/(\d{4})", a.get_text())
        if m and dm:
            iso = f"{dm.group(3)}-{dm.group(1)}-{dm.group(2)}"
            if START <= iso < END:
                logs.append((m.group(1), iso))

    links = []
    for logid, d in logs:
        r = requests.get(f"{origin}/tasks/addLog.cfm?LogID={logid}&Source=ServiceLog", headers=H, allow_redirects=False, timeout=60)
        s = BeautifulSoup(r.text, "html.parser")

        def iv(n):
            el = s.find("input", attrs={"name": n})
            return el.get("value", "") if el else ""
        eid = iv("EventID"); tin = iv("timeinvalue"); tout = iv("timeoutvalue"); fail = iv("OriginalFailureID")
        serviceable = not (tin and tin == tout) and not fail
        if eid:
            links.append({"event_id": eid, "date": d, "timein": tin or None, "serviceable": serviceable})

    conn = _connect(sb)
    stats = {"logs": len(logs), "links": len(links), "updated": 0, "no_match": 0, "event_not_in_db": 0, "dry_run": dry_run}
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(t.ion_task_id, ts.ion_task_id), t.id
                           FROM maintenance.tasks t
                           LEFT JOIN maintenance.task_schedules ts ON ts.task_id=t.id
                           WHERE COALESCE(t.ion_task_id, ts.ion_task_id) IS NOT NULL""")
            ion2task = {}
            for iid, tid in cur.fetchall():
                ion2task.setdefault(str(iid), tid)
            for lk in links:
                eid = lk["event_id"]
                if eid not in ion2task:
                    stats["event_not_in_db"] += 1
                    continue
                started = _combine(lk["date"], lk["timein"]) if lk["timein"] else None
                cur.execute("""UPDATE maintenance.visits
                               SET ion_task_id=%s, task_id=%s, is_serviceable=COALESCE(%s, is_serviceable), updated_at=now()
                               WHERE service_location_id=%s AND scheduled_date=%s
                                 AND (%s::timestamptz IS NULL OR started_at=%s::timestamptz)""",
                            (eid, ion2task[eid], lk["serviceable"], SL, lk["date"], started, started))
                if cur.rowcount:
                    stats["updated"] += cur.rowcount
                else:
                    stats["no_match"] += 1
            cur.execute("""SELECT ion_task_id,
                                  count(DISTINCT scheduled_date) FILTER (WHERE is_serviceable AND COALESCE(price_cents,0)>0) AS billable,
                                  max(price_cents) AS rate
                           FROM maintenance.visits
                           WHERE service_location_id=%s AND scheduled_date>=%s AND scheduled_date<%s
                           GROUP BY ion_task_id ORDER BY ion_task_id""", (SL, START, END))
            wr = [{"ion_task_id": r[0], "billable": r[1], "rate": r[2], "expected_usd": round((r[1] or 0) * (r[2] or 0) / 100.0, 2)} for r in cur.fetchall()]
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            stats["committed"] = True
        stats["winding_river_after"] = wr
        stats["wr_expected_total_usd"] = round(sum((x["billable"] or 0) * (x["rate"] or 0) for x in wr) / 100.0, 2)
        return stats
    finally:
        conn.close()

# f/ops/email_usage_digest
#
# Daily Windmill execution ledger -> email (via public.system_alerts + the
# send_pending_system_alerts poller). Pure projection of ops.script_usage_daily
# (written by f/ops/audit_script_usage) — recomputes nothing, so it can be
# re-sent for any past day from stored data.
#
# The email IS the ledger: every script that fired, ranked by COMPUTE per day
# (worker occupancy), each with a one-line description pulled live from the
# script's Windmill `summary`. Runaway rows are flagged inline and bump the
# alert to severity=high (>RUNAWAY_RUNS/day, or a high-volume failing loop).
#
# send=False returns render metadata without queuing an email (safe preview).

import os
import math
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import requests
import wmill

SUPABASE_RESOURCE = "u/carter/supabase"
RECIPIENT = "carter@jeffspoolspa.com"
DASHBOARD_URL = "https://claude.ai/code/artifact/3e5065a9-3ab8-435e-8474-1c5e53a373e8"

RUNAWAY_RUNS = 5000      # nothing legitimate here runs this many times/day
FAILING_LOOP_RUNS = 200  # + majority-failing => a broken trigger firing hot
FAILING_LOOP_RATE = 0.30

SEAT_QUOTA = 10000       # Windmill: included billable executions per seat / month
USER_SEATS = 1           # developers+operators seats (from the billing page)


def _seats(monthly_billable):
    extra = math.ceil(max(0, monthly_billable - SEAT_QUOTA * USER_SEATS) / SEAT_QUOTA)
    return USER_SEATS + extra


def _conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def _describe(script_paths):
    """path -> one-line description, from each script's Windmill `summary`
    (falls back to the first line of its description). Non-script rows
    (<flow>, <preview>, hub/...) get a generic label. One GET per path/day."""
    base = os.environ.get("BASE_INTERNAL_URL") or "https://app.windmill.dev"
    token = os.environ.get("WM_TOKEN", "")
    ws = os.environ.get("WM_WORKSPACE", "jps-internal")
    h = {"Authorization": f"Bearer {token}"}
    out = {}
    for p in script_paths:
        if p.startswith("<"):
            out[p] = {"<flow>": "inline flow step", "<preview>": "ad-hoc preview run",
                      "<http>": "HTTP-triggered run"}.get(p, "(non-script job)")
            continue
        if p.startswith("hub/"):
            out[p] = "Windmill Hub script"
            continue
        try:
            r = requests.get(f"{base}/api/w/{ws}/scripts/get/p/{p}", headers=h, timeout=20)
            if r.ok:
                d = r.json()
                desc = (d.get("summary") or "").strip()
                if not desc:
                    desc = (d.get("description") or "").strip().splitlines()[0] if d.get("description") else ""
                out[p] = desc or "(no summary set)"
            else:
                out[p] = "(not found)" if r.status_code == 404 else f"(lookup {r.status_code})"
        except Exception:
            out[p] = ""
    return out


def _runaway_reason(r) -> str:
    if r["runs"] >= RUNAWAY_RUNS:
        return f"{r['runs']:,} runs/day"
    if r["runs"] >= FAILING_LOOP_RUNS and r["failed"] / max(r["runs"], 1) >= FAILING_LOOP_RATE:
        return f"{r['failed']:,}/{r['runs']:,} failing"
    return ""


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render(day, rows, descs):
    total_runs = sum(r["runs"] for r in rows)
    total_s = sum(float(r["compute_s"]) for r in rows)
    total_failed = sum(r["failed"] for r in rows)
    total_bill = sum(r["billable_execs"] for r in rows)
    n_runaway = sum(1 for r in rows if _runaway_reason(r))
    monthly_est = total_bill * 30
    seats_est = _seats(monthly_est)

    C = {"ink": "#0e2230", "dim": "#4a5f6d", "mute": "#7c93a1", "line": "#dbe4ea",
         "bg": "#f4f7f9", "card": "#ffffff", "crit": "#c0362c"}
    mono = "font-family:'SF Mono',Menlo,Consolas,monospace;"

    def cell(v, style=""):
        return f'<td style="padding:8px 10px;border-bottom:1px solid {C["line"]};vertical-align:top;{style}">{v}</td>'

    body_rows = []
    for r in rows:
        reason = _runaway_reason(r)
        path = r["script_path"]
        name_style = f"{mono}font-size:12px;color:{C['crit'] if reason else C['ink']};white-space:nowrap;"
        srun = float(r["compute_s"]) / r["runs"] if r["runs"] else 0
        flag = f'<div style="color:{C["crit"]};font-weight:600;font-size:11px;">&#9888; {reason}</div>' if reason else ""
        body_rows.append(
            "<tr>"
            + cell(f'<span style="{name_style}">{_esc(path)}</span>{flag}')
            + cell(f'<span style="color:{C["dim"]};font-size:12.5px;">{_esc(descs.get(path, ""))}</span>',
                   "white-space:normal;min-width:200px;")
            + cell(f'{r["billable_execs"]:,}', f'{mono}text-align:right;font-weight:700;white-space:nowrap;')
            + cell(f'{float(r["compute_s"]):,.0f}s', f'{mono}text-align:right;color:{C["dim"]};white-space:nowrap;')
            + cell(f'{r["runs"]:,}', f'{mono}text-align:right;color:{C["mute"]};white-space:nowrap;')
            + cell(f'{r["failed"]:,}', f'{mono}text-align:right;color:{C["crit"] if r["failed"] else C["mute"]};white-space:nowrap;')
            + "</tr>"
        )

    th = f'padding:8px 10px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:{C["mute"]};border-bottom:2px solid {C["line"]};'
    subtitle = (f'<b style="color:{C["ink"]}">{total_bill:,}</b> billable executions &middot; '
                f'&asymp;{monthly_est:,} / month &rarr; <b style="color:{C["ink"]}">{seats_est} seats</b> at this rate &middot; '
                f'{total_s/60:,.0f} min compute &middot; {total_failed:,} failures &middot; {len(rows)} scripts')
    if n_runaway:
        subtitle = f'<span style="color:{C["crit"]};font-weight:600;">&#9888; {n_runaway} runaway</span> &middot; ' + subtitle

    html = (
        f'<div style="background:{C["bg"]};padding:24px 0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:{C["ink"]};">'
        f'<div style="max-width:820px;margin:0 auto;padding:0 20px;">'
        f'<div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:{C["mute"]};">Windmill execution ledger &middot; jps-internal</div>'
        f'<h1 style="font-size:22px;margin:4px 0 4px;color:{C["ink"]};">{day}</h1>'
        f'<div style="font-size:13px;color:{C["dim"]};margin-bottom:16px;">{subtitle}</div>'
        f'<div style="background:{C["card"]};border:1px solid {C["line"]};border-radius:8px;overflow:hidden;">'
        f'<table role="presentation" width="100%" style="border-collapse:collapse;font-size:13px;">'
        f'<tr><th style="{th}">Script</th><th style="{th}">What it does</th>'
        f'<th style="{th}text-align:right;">Billable</th><th style="{th}text-align:right;">Compute</th>'
        f'<th style="{th}text-align:right;">Runs</th><th style="{th}text-align:right;">Failed</th></tr>'
        f'{"".join(body_rows)}'
        f'</table></div>'
        f'<p style="font-size:12px;color:{C["mute"]};margin:16px 0 0;">Ranked by <b>billable executions</b> = what Windmill charges '
        f'(max(1, ceil(seconds)) per job &times; 2GB memory blocks). Source <span style="{mono}">ops.script_usage_daily</span>; '
        f'descriptions from each script&#39;s Windmill summary. <a href="{DASHBOARD_URL}" style="color:#2b8c62;">dashboard</a></p>'
        f'</div></div>'
    )

    text = (f"Windmill ledger {day}: {total_bill:,} billable execs "
            f"(~{monthly_est:,}/mo -> {seats_est} seats), {total_s/60:,.0f}m compute, "
            f"{total_failed:,} failed, {len(rows)} scripts.\n")
    text += "\n".join(
        f"  {r['billable_execs']:>7,} bill {float(r['compute_s']):>7,.0f}s {r['runs']:>6,}x  {r['script_path']}  — {descs.get(r['script_path'],'')}"
        + (f"  [RUNAWAY {_runaway_reason(r)}]" if _runaway_reason(r) else "")
        for r in rows
    )
    return html, text, {"total_runs": total_runs, "total_s": total_s, "total_billable": total_bill,
                        "monthly_est": monthly_est, "seats_est": seats_est,
                        "total_failed": total_failed, "runaways": n_runaway, "scripts": len(rows)}


def main(day: str = "", send: bool = True):
    """Email the day's execution ledger (every script, ranked by compute).
    Default day = yesterday. send=False = preview only (no email queued)."""
    if not day:
        day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT script_path, runs, compute_s, failed, billable_execs
                 FROM ops.script_usage_daily
                WHERE day = %s AND runs > 0
                ORDER BY billable_execs DESC""",
            (day,),
        )
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return {"day": day, "status": "no_data",
                    "note": "no ops.script_usage_daily rows — run f/ops/audit_script_usage for this day first"}

        descs = _describe([r["script_path"] for r in rows])
        html, text, meta = _render(day, rows, descs)
        subject = f"[JPS Windmill] {day} — {meta['total_billable']:,} billable execs (~{meta['seats_est']} seats/mo)"
        if meta["runaways"]:
            subject = f"⚠ RUNAWAY — {subject}"

        if not send:
            return {"day": day, "status": "preview", "would_send_to": RECIPIENT,
                    "subject": subject, **meta, "html_len": len(html),
                    "sample_desc": {r["script_path"]: descs.get(r["script_path"]) for r in rows[:6]}}

        cur = conn.cursor()
        cur.execute(
            """INSERT INTO public.system_alerts
                 (source, severity, subject, body_html, body_text, recipient, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending') RETURNING id""",
            ("ops.usage_digest", "high" if meta["runaways"] else "low",
             subject, html, text, RECIPIENT),
        )
        alert_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return {"day": day, "status": "queued", "alert_id": str(alert_id),
                "recipient": RECIPIENT, "subject": subject, **meta}
    finally:
        conn.close()

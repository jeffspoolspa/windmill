# f/ops/email_usage_digest
#
# Daily Windmill usage digest -> email (via public.system_alerts + the
# send_pending_system_alerts poller). Pure projection of ops.script_usage_daily
# (written by f/ops/audit_script_usage) — it recomputes nothing, so it can be
# re-sent for any past day from stored data.
#
# Ordered by COMPUTE per day (worker occupancy), not run count, per request.
# Flags runaways: any script over RUNAWAY_RUNS/day, or a high-volume failing
# loop (a broken wake/trigger). A runaway bumps the alert to severity=high.
#
# send=False returns the rendered HTML without queuing an email (safe preview).

from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras
import wmill

SUPABASE_RESOURCE = "u/carter/supabase"
RECIPIENT = "carter@jeffspoolspa.com"
DASHBOARD_URL = "https://claude.ai/code/artifact/3e5065a9-3ab8-435e-8474-1c5e53a373e8"

RUNAWAY_RUNS = 5000      # nothing legitimate here runs this many times/day
FAILING_LOOP_RUNS = 200  # + majority-failing => a broken trigger firing hot
FAILING_LOOP_RATE = 0.30


def _conn():
    sb = wmill.get_resource(SUPABASE_RESOURCE)
    return psycopg2.connect(
        host=sb["host"], port=sb.get("port", 6543),
        dbname=sb.get("dbname", "postgres"), user=sb["user"],
        password=sb["password"], sslmode=sb.get("sslmode", "require"),
    )


def _fmt_kinds(kinds) -> str:
    if not kinds:
        return ""
    return ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))


def _runaway_reason(r) -> str:
    if r["runs"] >= RUNAWAY_RUNS:
        return f"{r['runs']:,} runs/day"
    if r["runs"] >= FAILING_LOOP_RUNS and r["failed"] / max(r["runs"], 1) >= FAILING_LOOP_RATE:
        return f"{r['failed']:,}/{r['runs']:,} failing"
    return ""


def _render(day, rows):
    total_runs = sum(r["runs"] for r in rows)
    total_s = sum(float(r["compute_s"]) for r in rows)
    total_failed = sum(r["failed"] for r in rows)
    runaways = [(r, _runaway_reason(r)) for r in rows if _runaway_reason(r)]

    C = {
        "ink": "#0e2230", "dim": "#4a5f6d", "mute": "#7c93a1", "line": "#dbe4ea",
        "bg": "#f4f7f9", "card": "#ffffff", "crit": "#c0362c", "critbg": "#fbeceb",
        "good": "#2b8c62", "head": "#0e2230",
    }
    mono = "font-family:'SF Mono',Menlo,Consolas,monospace;"

    def cell(v, style=""):
        return f'<td style="padding:7px 10px;border-bottom:1px solid {C["line"]};{style}">{v}</td>'

    body_rows = []
    for r in rows:
        reason = _runaway_reason(r)
        name_style = f"{mono}font-size:12px;color:{C['crit'] if reason else C['ink']};"
        srun = float(r["compute_s"]) / r["runs"] if r["runs"] else 0
        flag = f' <span style="color:{C["crit"]};font-weight:600;">&#9888; {reason}</span>' if reason else ""
        body_rows.append(
            "<tr>"
            + cell(f'<span style="{name_style}">{r["script_path"]}</span>{flag}')
            + cell(f'{float(r["compute_s"]):,.0f}s', f'{mono}text-align:right;font-weight:600;')
            + cell(f'{r["runs"]:,}', f'{mono}text-align:right;color:{C["dim"]};')
            + cell(f'{srun:,.1f}s', f'{mono}text-align:right;color:{C["mute"]};')
            + cell(f'{r["failed"]:,}', f'{mono}text-align:right;color:{C["crit"] if r["failed"] else C["mute"]};')
            + cell(f'<span style="color:{C["mute"]};font-size:12px;">{_fmt_kinds(r.get("kinds"))}</span>')
            + "</tr>"
        )

    runaway_block = ""
    if runaways:
        items = "".join(
            f'<li style="margin:3px 0;"><span style="{mono}">{r["script_path"]}</span> &mdash; {reason}</li>'
            for r, reason in runaways
        )
        runaway_block = (
            f'<div style="background:{C["critbg"]};border:1px solid {C["crit"]};border-radius:8px;'
            f'padding:14px 16px;margin:0 0 18px;">'
            f'<div style="font-weight:700;color:{C["crit"]};margin-bottom:6px;">'
            f'&#9888; {len(runaways)} runaway signal(s) &mdash; investigate</div>'
            f'<ul style="margin:0;padding-left:20px;color:{C["ink"]};font-size:14px;">{items}</ul></div>'
        )

    summary = (
        f'<table role="presentation" width="100%" style="border-collapse:collapse;margin:0 0 18px;">'
        f'<tr>'
        f'<td style="padding:10px 14px;background:{C["card"]};border:1px solid {C["line"]};border-radius:8px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:700;{mono}color:{C["ink"]};">{total_runs:,}</div>'
        f'<div style="font-size:11px;color:{C["mute"]};text-transform:uppercase;letter-spacing:.06em;">executions</div></td>'
        f'<td style="width:10px;"></td>'
        f'<td style="padding:10px 14px;background:{C["card"]};border:1px solid {C["line"]};border-radius:8px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:700;{mono}color:{C["ink"]};">{total_s/60:,.0f} min</div>'
        f'<div style="font-size:11px;color:{C["mute"]};text-transform:uppercase;letter-spacing:.06em;">compute</div></td>'
        f'<td style="width:10px;"></td>'
        f'<td style="padding:10px 14px;background:{C["card"]};border:1px solid {C["line"]};border-radius:8px;text-align:center;">'
        f'<div style="font-size:22px;font-weight:700;{mono}color:{C["crit"] if total_failed else C["good"]};">{total_failed:,}</div>'
        f'<div style="font-size:11px;color:{C["mute"]};text-transform:uppercase;letter-spacing:.06em;">failures</div></td>'
        f'</tr></table>'
    )

    th = f'padding:8px 10px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:{C["mute"]};border-bottom:2px solid {C["line"]};'
    html = (
        f'<div style="background:{C["bg"]};padding:24px 0;font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:{C["ink"]};">'
        f'<div style="max-width:760px;margin:0 auto;padding:0 20px;">'
        f'<div style="font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:{C["mute"]};">Windmill usage &middot; jps-internal</div>'
        f'<h1 style="font-size:22px;margin:4px 0 16px;color:{C["head"]};">Daily compute report &mdash; {day}</h1>'
        f'{runaway_block}{summary}'
        f'<div style="background:{C["card"]};border:1px solid {C["line"]};border-radius:8px;overflow:hidden;">'
        f'<table role="presentation" width="100%" style="border-collapse:collapse;font-size:13px;">'
        f'<tr><th style="{th}">Script</th><th style="{th}text-align:right;">Compute/day</th>'
        f'<th style="{th}text-align:right;">Runs</th><th style="{th}text-align:right;">s/run</th>'
        f'<th style="{th}text-align:right;">Failed</th><th style="{th}">Triggers</th></tr>'
        f'{"".join(body_rows)}'
        f'</table></div>'
        f'<p style="font-size:12px;color:{C["mute"]};margin:16px 0 0;">Ranked by compute time (worker occupancy). '
        f'Source: <span style="{mono}">ops.script_usage_daily</span> &middot; '
        f'<a href="{DASHBOARD_URL}" style="color:{C["good"]};">live dashboard</a></p>'
        f'</div></div>'
    )

    text = f"Windmill usage {day}: {total_runs:,} executions, {total_s/60:,.0f} min compute, {total_failed:,} failures.\n"
    if runaways:
        text += "RUNAWAYS: " + "; ".join(f"{r['script_path']} ({reason})" for r, reason in runaways) + "\n"
    text += "Top by compute:\n" + "\n".join(
        f"  {float(r['compute_s']):>7,.0f}s  {r['runs']:>6,}x  {r['script_path']}" for r in rows[:12]
    )
    return html, text, {"total_runs": total_runs, "total_s": total_s,
                        "total_failed": total_failed, "runaways": len(runaways)}


def main(day: str = "", send: bool = True):
    """Email yesterday's usage digest (ordered by compute/day). send=False = preview only."""
    if not day:
        day = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    conn = _conn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT script_path, runs, compute_s, failed, kinds
                 FROM ops.script_usage_daily
                WHERE day = %s AND runs > 0
                ORDER BY compute_s DESC""",
            (day,),
        )
        rows = cur.fetchall()
        cur.close()

        if not rows:
            return {"day": day, "status": "no_data",
                    "note": "no ops.script_usage_daily rows — run f/ops/audit_script_usage for this day first"}

        html, text, meta = _render(day, rows)
        subject = f"[JPS Windmill] {day} — {meta['total_runs']:,} execs, {meta['total_s']/60:,.0f}m compute"
        if meta["runaways"]:
            subject = f"⚠ RUNAWAY — {subject}"

        if not send:
            return {"day": day, "status": "preview", "would_send_to": RECIPIENT,
                    "subject": subject, **meta, "html_len": len(html)}

        cur = conn.cursor()
        cur.execute(
            """INSERT INTO public.system_alerts
                 (source, severity, subject, body_html, body_text, recipient, status)
               VALUES (%s, %s, %s, %s, %s, %s, 'pending')
               RETURNING id""",
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

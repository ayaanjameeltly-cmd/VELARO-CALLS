"""
Velaro VAPI Results Dashboard
Run: python vapi_results.py
"""

import sqlite3
import os
from datetime import datetime

DB = os.path.join(os.path.dirname(__file__), "velaro.db")


def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def count(conn, where: str = "", args: tuple = ()) -> int:
    q = "SELECT COUNT(*) FROM vapi_calls"
    if where:
        q += " WHERE " + where
    return conn.execute(q, args).fetchone()[0]


def run():
    conn = get_db()

    # ensure table exists
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vapi_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_name TEXT, firm_name TEXT, phone_used TEXT,
            call_type TEXT, state TEXT, lead_type TEXT, outcome TEXT,
            email_collected TEXT, phone_collected TEXT,
            best_day TEXT, best_time TEXT, callback_date TEXT,
            called_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            attempt_number INTEGER DEFAULT 1,
            next_action TEXT, next_action_date TEXT, notes TEXT, vapi_call_id TEXT
        )
    """)

    today_sql = "DATE(called_at) = DATE('now')"

    calls_today    = count(conn, today_sql)
    direct_today   = count(conn, f"{today_sql} AND call_type='DIRECT_CALL'")
    mainline_today = count(conn, f"{today_sql} AND call_type IN ('MAIN_LINE','MAIN_LINE_FALLBACK')")
    hot_today      = count(conn, f"{today_sql} AND outcome='HOT'")
    warm_today     = count(conn, f"{today_sql} AND outcome='WARM'")
    callback_today = count(conn, f"{today_sql} AND outcome='CALLBACK'")
    vm_today       = count(conn, f"{today_sql} AND outcome='VOICEMAIL'")
    ni_today       = count(conn, f"{today_sql} AND outcome='NOT_INTERESTED'")
    na_today       = count(conn, f"{today_sql} AND outcome='NO_ANSWER'")
    exhausted      = count(conn, "attempt_number >= 3 AND outcome IN ('NO_ANSWER','VOICEMAIL')")
    total_all      = count(conn)

    w = 42

    def row(label, val):
        line = f" {label}"
        val_str = str(val)
        pad = w - len(line) - len(val_str) - 1
        return f"║{line}{' ' * pad}{val_str} ║"

    print("\n╔" + "═" * w + "╗")
    print("║" + " VAPI Results".center(w) + "║")
    print("╠" + "═" * w + "╣")
    print(row("Calls today:",             calls_today))
    print(row("Direct number calls:",     direct_today))
    print(row("Main line calls:",         mainline_today))
    print("╠" + "═" * w + "╣")
    print(row("HOT — calls booked:",      hot_today))
    print(row("WARM — emails sent:",      warm_today))
    print(row("Callbacks scheduled:",     callback_today))
    print(row("Voicemails left:",         vm_today))
    print(row("Not interested:",          ni_today))
    print(row("No answer:",               na_today))
    print(row("Exhausted (3 attempts):",  exhausted))
    print("╠" + "═" * w + "╣")
    print(row("Total all time:",          total_all))
    print("╚" + "═" * w + "╝")

    # ── TODAY'S ACTION LIST ────────────────────────────────────
    today = datetime.now().strftime("%Y-%m-%d")
    actions = conn.execute("""
        SELECT lead_name, firm_name, outcome, next_action, next_action_date,
               email_collected, phone_collected, best_day, best_time, callback_date
        FROM vapi_calls
        WHERE DATE(called_at) = DATE('now')
        ORDER BY
            CASE outcome
                WHEN 'HOT'      THEN 1
                WHEN 'WARM'     THEN 2
                WHEN 'CALLBACK' THEN 3
                ELSE 4
            END
    """).fetchall()

    if actions:
        print("\nTODAY'S ACTION LIST:")
        print("─" * 70)
        for r in actions:
            print(f"  {r['lead_name']:<22} | {r['firm_name']:<25} | {r['next_action']:<30} | {r['next_action_date'] or '—'}")

    # ── HOT ───────────────────────────────────────────────────
    hot = conn.execute("""
        SELECT lead_name, firm_name, email_collected, best_day, best_time
        FROM vapi_calls WHERE outcome='HOT' AND DATE(called_at)=DATE('now')
    """).fetchall()
    if hot:
        print("\nHOT — SEND CALENDAR INVITE NOW:")
        print("─" * 70)
        for r in hot:
            print(f"  {r['lead_name']:<22} | {r['firm_name']:<22} | {r['email_collected'] or '—':<28} | {r['best_day'] or '?'} {r['best_time'] or ''}")

    # ── WARM ──────────────────────────────────────────────────
    warm = conn.execute("""
        SELECT lead_name, firm_name, email_collected
        FROM vapi_calls WHERE outcome='WARM' AND DATE(called_at)=DATE('now')
    """).fetchall()
    if warm:
        print("\nWARM — SEND DEMO VIDEO NOW:")
        print("─" * 70)
        for r in warm:
            print(f"  {r['lead_name']:<22} | {r['firm_name']:<25} | {r['email_collected'] or '—'}")

    # ── CALLBACKS ─────────────────────────────────────────────
    callbacks = conn.execute("""
        SELECT lead_name, firm_name, phone_collected, callback_date
        FROM vapi_calls WHERE outcome='CALLBACK'
        ORDER BY callback_date ASC
    """).fetchall()
    if callbacks:
        print("\nCALLBACKS SCHEDULED:")
        print("─" * 70)
        for r in callbacks:
            print(f"  {r['lead_name']:<22} | {r['firm_name']:<25} | {r['phone_collected'] or '—':<16} | {r['callback_date'] or '—'}")

    # ── NEXT UP ───────────────────────────────────────────────
    retry = conn.execute("""
        SELECT lead_name, firm_name, next_action, next_action_date
        FROM vapi_calls
        WHERE next_action_date > DATE('now')
          AND outcome NOT IN ('HOT','NOT_INTERESTED','DO_NOT_CALL')
        ORDER BY next_action_date ASC
        LIMIT 10
    """).fetchall()
    if retry:
        print("\nUPCOMING RETRIES:")
        print("─" * 70)
        for r in retry:
            print(f"  {r['next_action_date']:<12} | {r['lead_name']:<22} | {r['firm_name']:<25} | {r['next_action']}")

    print()
    conn.close()


if __name__ == "__main__":
    run()

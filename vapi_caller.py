"""
Velaro Retell AI Outbound Caller
Run: python vapi_caller.py
"""

import sqlite3
import time
import os
import fcntl
import requests
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

LOCK_FILE = "/tmp/retell_caller.lock"

load_dotenv()

import random

RETELL_API_KEY = os.getenv("RETELL_API_KEY")

# phone number pool — rotates round-robin across all configured numbers
_FROM_NUMBERS = [n for n in [
    os.getenv("RETELL_PHONE_NUMBER",   "+18324089822"),
    os.getenv("RETELL_PHONE_NUMBER_2", ""),
    os.getenv("RETELL_PHONE_NUMBER_3", ""),
] if n]
_num_index = 0
_num_lock   = threading.Lock()

BATCH_SIZE = len(_FROM_NUMBERS)  # call all 3 numbers simultaneously

def pick_from_number() -> str:
    global _num_index
    with _num_lock:
        num = _FROM_NUMBERS[_num_index % len(_FROM_NUMBERS)]
        _num_index += 1
    return num

# A/B test variants — (agent_id, label, weight)
AB_VARIANTS = [
    (os.getenv("RETELL_AGENT_A", "agent_0eaaaafaff06fdaa34e2eab5c2"), "A-Pain",        30),
    (os.getenv("RETELL_AGENT_B", ""),                                  "B-Stat",         20),
    (os.getenv("RETELL_AGENT_C", ""),                                  "C-SocialProof",  20),
    (os.getenv("RETELL_AGENT_D", ""),                                  "D-UltraDirect",  10),
    (os.getenv("RETELL_AGENT_E", ""),                                  "E-Referral",     20),
]

def pick_variant() -> tuple:
    """Weighted random selection. Returns (agent_id, label)."""
    pool = [(aid, label) for aid, label, _ in AB_VARIANTS if aid]
    weights = [w for aid, _, w in AB_VARIANTS if aid]
    chosen = random.choices(pool, weights=weights, k=1)[0]
    return chosen

DB = os.path.join(os.path.dirname(__file__), "velaro.db")

CALL_START_HOUR        = 9    # 9am in lead's local timezone
CALL_END_HOUR          = 17   # 5pm in lead's local timezone
MAX_CALLS_PER_DAY      = 40   # default — overridden by DB setting if set
MAX_CALLS_PER_SESSION  = 4
GAP_BETWEEN_CALLS      = 360  # 6 minutes between calls
MAX_ATTEMPTS           = 9    # 3 per day × 3 days before giving up
MAX_NO_ANSWER_TODAY    = 2    # call + 1 retry same day, then dead
MIN_HOURS_BETWEEN_NO_ANSWER = 2
SKIP_WEEKENDS          = True

STATE_TZ = {
    "AL": "America/Chicago",  "AK": "America/Anchorage",
    "AZ": "America/Phoenix",  "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver",
    "CT": "America/New_York", "DE": "America/New_York",
    "FL": "America/New_York", "GA": "America/New_York",
    "HI": "Pacific/Honolulu", "ID": "America/Denver",
    "IL": "America/Chicago",  "IN": "America/Indiana/Indianapolis",
    "IA": "America/Chicago",  "KS": "America/Chicago",
    "KY": "America/New_York", "LA": "America/Chicago",
    "ME": "America/New_York", "MD": "America/New_York",
    "MA": "America/New_York", "MI": "America/Detroit",
    "MN": "America/Chicago",  "MS": "America/Chicago",
    "MO": "America/Chicago",  "MT": "America/Denver",
    "NE": "America/Chicago",  "NV": "America/Los_Angeles",
    "NH": "America/New_York", "NJ": "America/New_York",
    "NM": "America/Denver",   "NY": "America/New_York",
    "NC": "America/New_York", "ND": "America/Chicago",
    "OH": "America/New_York", "OK": "America/Chicago",
    "OR": "America/Los_Angeles", "PA": "America/New_York",
    "RI": "America/New_York", "SC": "America/New_York",
    "SD": "America/Chicago",  "TN": "America/Chicago",
    "TX": "America/Chicago",  "UT": "America/Denver",
    "VT": "America/New_York", "VA": "America/New_York",
    "WA": "America/Los_Angeles", "WV": "America/New_York",
    "WI": "America/Chicago",  "WY": "America/Denver",
}

def is_weekend_for(tz: ZoneInfo) -> bool:
    return datetime.now(tz).weekday() >= 5

def lead_timezone(lead: Dict[str, Any]) -> ZoneInfo:
    tz_str = (lead.get("timezone") or "").strip()
    if not tz_str:
        tz_str = STATE_TZ.get((lead.get("state") or "").upper(), "America/Chicago")
    try:
        return ZoneInfo(tz_str)
    except Exception:
        return ZoneInfo("America/Chicago")

HOOKS = {
    "HIRING_INTAKE": (
        "I saw {firm_name} is hiring an intake specialist right now — "
        "before you commit to that hire, this is genuinely worth 15 minutes of your time."
    ),
    "OVERNIGHT_INTAKE": (
        "I saw {firm_name} posted for an overnight intake specialist — "
        "that role exists because calls come in after hours and nobody's picking up. "
        "I automate exactly what that hire would do, for a fraction of the salary."
    ),
    "HIGH_VOLUME": (
        "{firm_name} has {google_reviews} Google reviews — that's serious volume. "
        "The firms at your level are the ones losing the most after hours when nobody picks up."
    ),
    "WARM_EMAIL": (
        "I emailed you last week about this — wanted to follow up directly "
        "because honestly it's much easier to show than explain in writing."
    ),
    "REDDIT_ENGAGED": (
        "You commented on something about intake recently — wanted to follow up directly "
        "since this is right in that space."
    ),
    "COLD": (
        "Firms we work with are recovering 3–5 cases per month they didn't even know they were losing."
    ),
}

VOICEMAIL_SCRIPT = (
    "Hey {first_name} — Brooke from Velaro. "
    "Quick one — we work with PI firms on their after-hours intake systems "
    "and I noticed {firm_hook}. "
    "I have something specific I want to show you for {firm_name} — "
    "it'll take about two minutes to see. "
    "Best number to reach me is {retell_number}. "
    "I'll try you again {next_attempt_day}. "
    "Again — Brooke from Velaro. {retell_number}."
)

# ── DB helpers ─────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vapi_calls (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_name           TEXT,
            firm_name           TEXT,
            phone_used          TEXT,
            call_type           TEXT,
            state               TEXT,
            lead_type           TEXT,
            outcome             TEXT,
            email_collected     TEXT,
            phone_collected     TEXT,
            best_day            TEXT,
            best_time           TEXT,
            callback_date       TEXT,
            attorneys           TEXT,
            runs_ads            TEXT,
            after_hours_process TEXT,
            weekly_inquiries    TEXT,
            avg_case_value      TEXT,
            called_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            attempt_number      INTEGER DEFAULT 1,
            next_action         TEXT,
            next_action_date    TEXT,
            notes               TEXT,
            vapi_call_id        TEXT,
            ab_variant          TEXT DEFAULT 'A-Pain'
        );
    """)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(vapi_calls)").fetchall()}
    for col, typ in [
        ("attorneys","TEXT"), ("runs_ads","TEXT"), ("after_hours_process","TEXT"),
        ("weekly_inquiries","TEXT"), ("avg_case_value","TEXT"), ("ab_variant","TEXT"),
        ("gatekeeper_status","TEXT"),  # DIRECT | BYPASSED | BLOCKED | UNKNOWN
    ]:
        if col not in existing:
            conn.execute(f"ALTER TABLE vapi_calls ADD COLUMN {col} {typ}")
    conn.commit()
    conn.close()

def ist_today() -> str:
    """Return today's date in IST (UTC+5:30) as YYYY-MM-DD."""
    from datetime import timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist).strftime("%Y-%m-%d")

def us_central_today() -> str:
    """Return today's date in US Central time (UTC-5 CDT) as YYYY-MM-DD.
    Used for same-day call checks — all leads are TX/FL/GA."""
    from datetime import timezone, timedelta
    ct = timezone(timedelta(hours=-5))
    return datetime.now(ct).strftime("%Y-%m-%d")

def calls_today() -> int:
    conn = get_db()
    # use IST date so 1:30am IST calls count as today, not yesterday UTC
    row = conn.execute(
        "SELECT COUNT(*) FROM vapi_calls WHERE DATE(datetime(called_at, '+330 minutes')) = ?",
        (ist_today(),)
    ).fetchone()
    conn.close()
    return row[0]

def attempts_for_lead(lead_name: str, firm_name: str) -> int:
    conn = get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM vapi_calls WHERE lead_name=? AND firm_name=?",
        (lead_name, firm_name)
    ).fetchone()
    conn.close()
    return row[0]

def last_call_record(lead_name: str, firm_name: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM vapi_calls WHERE lead_name=? AND firm_name=? ORDER BY called_at DESC LIMIT 1",
        (lead_name, firm_name)
    ).fetchone()
    conn.close()
    return dict(row) if row else None

def is_do_not_call(notes: str) -> bool:
    return "do_not_call" in (notes or "").lower()

def mark_no_answer_dead(lead_name: str, firm_name: str):
    """After 2 same-day no-answers, park the lead — never call again."""
    conn = get_db()
    conn.execute(
        "UPDATE vapi_leads SET no_answer_dead=1 WHERE name=? AND firm_name=?",
        (lead_name, firm_name)
    )
    conn.commit()
    conn.close()
    print(f"  [NO_ANSWER DEAD] {lead_name} ({firm_name}) — 2 no-answers today, moved to No Answer list")

def log_call(data: dict):
    conn = get_db()
    conn.execute("""
        INSERT INTO vapi_calls
            (lead_name, firm_name, phone_used, call_type, state, lead_type,
             outcome, email_collected, phone_collected, best_day, best_time,
             callback_date, attorneys, runs_ads, after_hours_process,
             weekly_inquiries, avg_case_value,
             attempt_number, next_action, next_action_date, notes, vapi_call_id, ab_variant,
             gatekeeper_status)
        VALUES
            (:lead_name, :firm_name, :phone_used, :call_type, :state, :lead_type,
             :outcome, :email_collected, :phone_collected, :best_day, :best_time,
             :callback_date, :attorneys, :runs_ads, :after_hours_process,
             :weekly_inquiries, :avg_case_value,
             :attempt_number, :next_action, :next_action_date, :notes, :vapi_call_id, :ab_variant,
             :gatekeeper_status)
    """, data)
    # score decay: each missed call costs 1 point, floor at -9
    outcome = (data.get("outcome") or "").upper()
    if outcome in ("NO_ANSWER", "VOICEMAIL"):
        conn.execute("""
            UPDATE vapi_leads SET score = MAX(score - 1, -9)
            WHERE name = ? AND firm_name = ?
        """, (data["lead_name"], data["firm_name"]))
    elif outcome == "CALLBACK":
        # they asked you to call back — small positive signal
        conn.execute("""
            UPDATE vapi_leads SET score = MIN(score + 1, 10)
            WHERE name = ? AND firm_name = ?
        """, (data["lead_name"], data["firm_name"]))
    conn.commit()
    conn.close()

# ── calling window ─────────────────────────────────────────────
def in_calling_window_for(tz: ZoneInfo) -> bool:
    now_local = datetime.now(tz)
    return CALL_START_HOUR <= now_local.hour < CALL_END_HOUR

def seconds_until_window_for(tz: ZoneInfo) -> int:
    now_local = datetime.now(tz)
    if now_local.hour < CALL_START_HOUR:
        target = now_local.replace(hour=CALL_START_HOUR, minute=0, second=0, microsecond=0)
    else:
        target = (now_local + timedelta(days=1)).replace(hour=CALL_START_HOUR, minute=0, second=0, microsecond=0)
    return int((target - now_local).total_seconds())

# ── Retell call dispatch ───────────────────────────────────────
def make_retell_call(phone: str, dynamic_vars: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Fire a Retell outbound call using a randomly picked A/B variant."""
    to_number = f"+1{phone}" if not phone.startswith("+") else phone
    agent_id, variant_label = pick_variant()
    from_number = pick_from_number()
    payload = {
        "from_number": from_number,
        "to_number": to_number,
        "agent_id": agent_id,
        "retell_llm_dynamic_variables": {**dynamic_vars, "ab_variant": variant_label},
    }
    # stash variant on the object so caller can log it
    payload["_variant"] = variant_label
    send_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    try:
        resp = requests.post(
            "https://api.retellai.com/v2/create-phone-call",
            headers={
                "Authorization": f"Bearer {RETELL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=send_payload,
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        result["_variant"] = variant_label   # pass through for logging
        return result
    except requests.RequestException as e:
        print(f"    [RETELL ERROR] {e}")
        if hasattr(e, 'response') and e.response is not None:
            print(f"    Response: {e.response.text[:300]}")
        return None

def get_retell_call_status(call_id: str) -> Optional[Dict[str, Any]]:
    try:
        resp = requests.get(
            f"https://api.retellai.com/v2/get-call/{call_id}",
            headers={"Authorization": f"Bearer {RETELL_API_KEY}"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException:
        return None

def poll_call_outcome(call_id: str, timeout: int = 300) -> Dict[str, Any]:
    """
    Poll Retell every 10s for up to `timeout` seconds until call_status is 'ended' or 'error'.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = get_retell_call_status(call_id)
        if not data:
            time.sleep(10)
            continue
        status = data.get("call_status", "")
        if status in ("ended", "error"):
            return parse_call_result(data)
        time.sleep(10)
    return {"outcome": "NO_ANSWER", "notes": "Timed out waiting for call result"}

def parse_call_result(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract outcome from Retell call data.
    Retell populates call_analysis after the call ends.
    disconnection_reason tells us why the call ended.
    """
    result: Dict[str, Any] = {
        "outcome": "NO_ANSWER",
        "email_collected": "",
        "phone_collected": "",
        "best_day": "",
        "best_time": "",
        "callback_date": "",
        "attorneys": "",
        "runs_ads": "",
        "after_hours_process": "",
        "weekly_inquiries": "",
        "avg_case_value": "",
        "notes": "",
        "vapi_call_id": data.get("call_id", ""),
    }

    disconnection_reason = data.get("disconnection_reason", "")
    call_status = data.get("call_status", "")
    analysis = data.get("call_analysis") or {}
    summary = analysis.get("call_summary", "") or ""
    transcript = data.get("transcript", "") or ""

    # map Retell disconnection reasons to outcomes
    reason_map = {
        "voicemail_reached":    "VOICEMAIL",
        "machine_detected":     "VOICEMAIL",
        "dial_no_answer":       "NO_ANSWER",
        "dial_busy":            "NO_ANSWER",
        "dial_failed":          "NO_ANSWER",
        "inactivity":           "NO_ANSWER",
        "concurrency_limit_reached": "NO_ANSWER",
        "user_hangup":          "ENGAGED",   # will refine from transcript below
        "agent_hangup":         "ENGAGED",
        "call_transfer":        "HOT",
        "error_inbound_webhook": "NO_ANSWER",
    }
    outcome = reason_map.get(disconnection_reason, "NO_ANSWER")

    # refine from transcript keywords — NOT_INTERESTED checked first
    t = (transcript + " " + summary).lower()
    if any(w in t for w in [
        "not interested", "remove me", "remove us", "remove from", "call list",
        "don't call", "do not call", "stop calling", "no thank you", "no thanks",
        "take us off", "unsubscribe", "no longer interested"
    ]):
        outcome = "NOT_INTERESTED"
    elif any(w in t for w in ["book", "schedule", "calendar", "15 minutes", "send me the link", "yes i'm interested"]):
        outcome = "HOT"
    elif any(w in t for w in ["send me an email", "email me", "send the demo", "sounds interesting"]):
        outcome = "WARM"
    elif any(w in t for w in ["call back", "better time", "call me", "try again"]):
        outcome = "CALLBACK"
    elif "voicemail" in t or disconnection_reason in ("voicemail_reached", "machine_detected"):
        outcome = "VOICEMAIL"
    elif outcome == "ENGAGED" and not t.strip():
        outcome = "NO_ANSWER"

    # use Retell's call_successful flag as a WARM signal — never override a firm rejection
    if analysis.get("call_successful") and outcome not in ("HOT", "WARM", "CALLBACK", "NOT_INTERESTED"):
        outcome = "WARM"

    result["outcome"] = outcome
    result["notes"] = summary or disconnection_reason
    result["gatekeeper_status"] = _detect_gatekeeper_status(t, outcome)
    return result


def _detect_gatekeeper_status(t: str, outcome: str) -> str:
    """BYPASSED = got through gatekeeper | BLOCKED = gatekeeper stopped us | DIRECT = DM answered | UNKNOWN"""
    transfer = any(w in t for w in [
        "one moment", "let me transfer", "let me put you through", "i'll connect",
        "putting you through", "let me get him", "let me get her", "just a moment",
        "i'll transfer", "hold for", "one second i'll get", "let me grab",
    ])
    screening = any(w in t for w in [
        "what's this regarding", "what is this regarding", "may i ask",
        "who's calling", "who is calling", "can i ask what", "is this a sales",
        "what company", "where are you calling from", "where are you from",
    ])
    blocked = any(w in t for w in [
        "not available", "in a meeting", "she's not in", "he's not in",
        "not in right now", "can i take a message", "try back", "try again later",
        "i'll let them know", "i can pass along",
    ])

    if transfer:
        return "BYPASSED"
    if outcome in ("HOT", "WARM", "CALLBACK") and screening:
        return "BYPASSED"   # had screening but still got a positive result
    if blocked or (screening and outcome in ("NO_ANSWER", "NOT_INTERESTED", "ENGAGED")):
        return "BLOCKED"
    if outcome in ("HOT", "WARM", "CALLBACK", "NOT_INTERESTED") and not screening:
        return "DIRECT"     # DM answered, no gatekeeper in the way
    return "UNKNOWN"

# ── next action logic ──────────────────────────────────────────
def next_action_for_outcome(outcome: str, attempt: int) -> Tuple[str, str]:
    today    = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    outcome_map = {
        "HOT":            ("Send calendar invite",   today),
        "WARM":           ("Send demo email",        today),
        "CALLBACK":       ("Retry call tomorrow",    tomorrow),
        "NO_ANSWER":      ("Retry same day",         today),
        "VOICEMAIL":      ("Retry call in 2 days",   (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")),
        "NOT_INTERESTED": ("No action for 30 days",  (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")),
        "DO_NOT_CALL":    ("Never retry",            ""),
    }
    if outcome in outcome_map:
        return outcome_map[outcome]
    if attempt >= MAX_ATTEMPTS:
        return ("Exhausted", today)
    return ("Retry", tomorrow)

# ── lead loading ───────────────────────────────────────────────
def load_leads() -> List[Dict[str, Any]]:
    conn = get_db()
    ist_date = ist_today()
    rows = conn.execute(
        """SELECT l.*,
             CASE WHEN (
               SELECT outcome FROM vapi_calls
               WHERE lead_name=l.name AND firm_name=l.firm_name
               ORDER BY called_at DESC LIMIT 1
             ) = 'CALLBACK'
             AND (
               SELECT COALESCE(callback_date, next_action_date, '') FROM vapi_calls
               WHERE lead_name=l.name AND firm_name=l.firm_name
               ORDER BY called_at DESC LIMIT 1
             ) <= ? THEN 0 ELSE 1 END AS callback_priority
           FROM vapi_leads l
           WHERE do_not_call = 0
             AND (no_answer_dead IS NULL OR no_answer_dead = 0)
           ORDER BY
             callback_priority ASC,
             CASE lead_type
               WHEN 'OVERNIGHT_INTAKE' THEN 0
               WHEN 'HIRING_INTAKE'    THEN 1
               ELSE 2
             END ASC,
             id ASC""",
        (ist_date,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def should_skip(lead: Dict[str, Any], attempt: int) -> Tuple[bool, str]:
    if is_do_not_call(lead.get("notes", "")):
        return True, "DO_NOT_CALL"
    if attempt >= MAX_ATTEMPTS:
        return True, f"Exhausted ({attempt} attempts)"

    last = last_call_record(lead["name"], lead["firm_name"])
    if not last:
        return False, ""

    outcome = (last.get("outcome") or "").upper()
    today = datetime.now().date()

    if outcome in ("HOT", "WARM", "NOT_INTERESTED", "DO_NOT_CALL"):
        return True, f"Already {outcome}"

    if outcome == "CALLBACK":
        # use explicit callback_date if captured, else next_action_date (defaults to tomorrow)
        cb_date = last.get("callback_date") or last.get("next_action_date") or ""
        if not cb_date:
            # gatekeeper said "call back" but gave no date — wait until tomorrow
            return True, "CALLBACK — no date given, retry tomorrow"
        try:
            call_on = datetime.strptime(cb_date[:10], "%Y-%m-%d").date()
            if today < call_on:
                return True, f"CALLBACK scheduled for {call_on}"
        except ValueError:
            pass
        return False, ""

    if outcome == "VOICEMAIL":
        next_date = last.get("next_action_date") or ""
        if next_date:
            try:
                retry_on = datetime.strptime(next_date[:10], "%Y-%m-%d").date()
                if today < retry_on:
                    return True, f"Voicemail retry on {retry_on}"
            except ValueError:
                pass
        return False, ""

    if outcome == "NO_ANSWER":
        conn = get_db()
        no_answer_today = conn.execute(
            """SELECT COUNT(*), MAX(called_at) FROM vapi_calls
               WHERE lead_name=? AND firm_name=? AND outcome='NO_ANSWER'
               AND DATE(datetime(called_at, '-300 minutes'))=?""",
            (lead["name"], lead["firm_name"], us_central_today())
        ).fetchone()
        conn.close()
        count_today = no_answer_today[0] or 0
        last_call_time = no_answer_today[1]

        # 2 no-answers same day → dead, move to No Answer list
        if count_today >= MAX_NO_ANSWER_TODAY:
            mark_no_answer_dead(lead["name"], lead["firm_name"])
            return True, f"NO_ANSWER x{count_today} today — moved to No Answer list"

        # wait at least 2 hours before same-day retry
        if last_call_time:
            try:
                last_dt = datetime.strptime(last_call_time[:19], "%Y-%m-%d %H:%M:%S")
                hours_since = (datetime.utcnow() - last_dt).total_seconds() / 3600
                if hours_since < MIN_HOURS_BETWEEN_NO_ANSWER:
                    return True, f"Called {hours_since:.1f}h ago — retry in {MIN_HOURS_BETWEEN_NO_ANSWER - hours_since:.1f}h"
            except ValueError:
                pass
        return False, ""

    return False, ""

# ── single-call worker (runs inside a thread) ─────────────────
def fire_and_poll(job: Dict[str, Any]) -> Dict[str, Any]:
    call_resp = make_retell_call(job["dial_number"], job["dynamic_vars"])
    if not call_resp:
        print(f"  [FAIL] {job['name']} | {job['firm_name']} — dispatch error")
        return {**job, "outcome": "NO_ANSWER", "call_id": "", "variant": "A-Pain",
                "email_collected": "", "phone_collected": "", "best_day": "", "best_time": "",
                "callback_date": "", "attorneys": "", "runs_ads": "", "after_hours_process": "",
                "weekly_inquiries": "", "avg_case_value": "", "notes": "dispatch_failed"}
    call_id = call_resp.get("call_id", "")
    variant  = call_resp.get("_variant", "A-Pain")
    print(f"  [LIVE] {job['name']} | {job['firm_name']} → {call_id} [{variant}]")
    result = poll_call_outcome(call_id, timeout=300)
    print(f"  [DONE] {job['name']} | {job['firm_name']} → {result['outcome']}")
    return {**job, **result, "call_id": call_id, "variant": variant}

# ── main run loop ──────────────────────────────────────────────
def run():
    init_db()
    print("\n" + "="*56)
    print("  VELARO RETELL OUTBOUND CALLER")
    print("="*56)

    if not RETELL_API_KEY:
        print("[ERROR] RETELL_API_KEY not set in .env")
        return
    if not any(aid for aid, _, __ in AB_VARIANTS if aid):
        print("[ERROR] No RETELL_AGENT_* set in .env")
        return

    leads_peek = load_leads()
    if leads_peek:
        first_tz = lead_timezone(leads_peek[0])
        if not in_calling_window_for(first_tz):
            secs = seconds_until_window_for(first_tz)
            h, m = divmod(secs // 60, 60)
            print(f"\n[WAIT] Outside 9am–5pm window ({first_tz}).")
            print(f"       Window opens in {h}h {m}m.")
            return

    # read daily limit from DB (warm-up mode overrides hardcoded default)
    conn_s = get_db()
    row_s = conn_s.execute("SELECT value FROM settings WHERE key='daily_call_limit'").fetchone()
    conn_s.close()
    daily_limit   = int(row_s[0]) if row_s else MAX_CALLS_PER_DAY
    session_limit = max(BATCH_SIZE, daily_limit // 4)  # 4 sessions × BATCH_SIZE calls each

    today_count = calls_today()
    if today_count >= daily_limit:
        print(f"\n[LIMIT] {daily_limit} calls already made today. Done.")
        return

    leads     = leads_peek
    remaining = min(daily_limit - today_count, session_limit)
    print(f"\nLoaded {len(leads)} leads | Called today: {today_count} | This session: up to {remaining}")
    print(f"Parallel batch size: {BATCH_SIZE} numbers\n")

    # ── Phase 1: collect callable jobs (check window, skip logic) ──
    jobs = []
    for lead in leads:
        if len(jobs) >= remaining:
            break

        tz = lead_timezone(lead)

        if SKIP_WEEKENDS and is_weekend_for(tz):
            continue

        if not in_calling_window_for(tz):
            continue

        name      = lead.get("name", "")
        firm      = lead.get("firm_name", "")
        phone     = (lead.get("phone") or "").strip()
        direct    = (lead.get("direct_number") or "").strip()
        city      = lead.get("city", "") or ""
        state     = lead.get("state", "") or ""
        lead_type = (lead.get("lead_type") or "COLD").upper()
        reviews   = lead.get("google_reviews", "") or ""
        notes     = lead.get("notes", "") or ""
        score     = lead.get("score", 5) or 5

        first_name = (name.split()[0] if name else "") or ""
        attempt    = attempts_for_lead(name, firm) + 1

        skip, reason = should_skip(lead, attempt - 1)
        if skip:
            print(f"  SKIP  {name} ({firm}) — {reason}")
            continue

        if not phone and not direct:
            print(f"  SKIP  {name} — no phone number")
            continue

        use_direct  = bool(direct)
        dial_number = direct if use_direct else phone
        call_type   = "DIRECT_CALL" if use_direct else "MAIN_LINE"

        hook_template = HOOKS.get(lead_type, HOOKS["COLD"])
        hook = hook_template.format(firm_name=firm, google_reviews=reviews, first_name=first_name)

        next_day = (datetime.now() + timedelta(days=3)).strftime("%A")
        if attempt == 1:
            attempt_context = "first_call"
        elif attempt <= 3:
            attempt_context = "follow_up"
        else:
            attempt_context = "final_attempt"

        jobs.append({
            "name":        name,
            "firm_name":   firm,
            "phone":       phone,
            "dial_number": dial_number,
            "call_type":   call_type,
            "use_direct":  use_direct,
            "state":       state,
            "lead_type":   lead_type,
            "notes":       notes,
            "attempt":     attempt,
            "dynamic_vars": {
                "lead_name":        name,
                "first_name":       first_name,
                "firm_name":        firm,
                "city":             city,
                "state":            state,
                "lead_type":        lead_type,
                "google_reviews":   str(reviews),
                "hook":             hook,
                "attempt":          str(attempt),
                "attempt_context":  attempt_context,
                "score":            str(score),
                "next_attempt_day": next_day,
                "firm_hook":        hook,
            },
        })

    if not jobs:
        print("\n[DONE] No callable leads this session.")
        return

    # ── Phase 2: fire in parallel batches of BATCH_SIZE ──────────
    called_this_run = 0
    batch_num       = 0

    for i in range(0, len(jobs), BATCH_SIZE):
        if called_this_run >= remaining:
            break

        batch = jobs[i : i + BATCH_SIZE]
        batch = batch[: remaining - called_this_run]
        batch_num += 1

        print(f"\n{'='*56}")
        print(f"  BATCH {batch_num} — firing {len(batch)} call(s) simultaneously")
        for j, job in enumerate(batch):
            print(f"  [{j+1}] {job['name']} | {job['firm_name']} | {job['dial_number']}")
        print(f"{'='*56}")

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            futures = {executor.submit(fire_and_poll, job): job for job in batch}
            results = [f.result() for f in as_completed(futures)]

        for res in results:
            outcome = res["outcome"]
            name    = res["name"]
            firm    = res["firm_name"]
            next_action, next_date = next_action_for_outcome(outcome, res["attempt"])

            log_data = {
                "lead_name":           name,
                "firm_name":           firm,
                "phone_used":          res["dial_number"],
                "call_type":           res["call_type"],
                "state":               res["state"],
                "lead_type":           res["lead_type"],
                "outcome":             outcome,
                "email_collected":     res.get("email_collected", ""),
                "phone_collected":     res.get("phone_collected", ""),
                "best_day":            res.get("best_day", ""),
                "best_time":           res.get("best_time", ""),
                "callback_date":       res.get("callback_date", ""),
                "attorneys":           res.get("attorneys", ""),
                "runs_ads":            res.get("runs_ads", ""),
                "after_hours_process": res.get("after_hours_process", ""),
                "weekly_inquiries":    res.get("weekly_inquiries", ""),
                "avg_case_value":      res.get("avg_case_value", ""),
                "attempt_number":      res["attempt"],
                "next_action":         next_action,
                "next_action_date":    next_date,
                "notes":               res.get("notes", ""),
                "vapi_call_id":        res.get("call_id", ""),
                "ab_variant":          res.get("variant", "A-Pain"),
                "gatekeeper_status":   res.get("gatekeeper_status", "UNKNOWN"),
            }
            log_call(log_data)
            called_this_run += 1

            if outcome in ("HOT", "WARM") and res.get("email_collected"):
                print(f"  → Email to {res['email_collected']}")
                subprocess.Popen([
                    "python3",
                    os.path.join(os.path.dirname(__file__), "send_followup_email.py"),
                    outcome, name, firm,
                    res["email_collected"],
                    res.get("best_day", ""),
                    res.get("best_time", ""),
                ])

            # fallback: direct no answer → try main line
            if res.get("use_direct") and outcome == "NO_ANSWER" and res.get("phone"):
                fb_resp = make_retell_call(res["phone"], res["dynamic_vars"])
                if fb_resp:
                    fb_id  = fb_resp.get("call_id", "")
                    fb_res = poll_call_outcome(fb_id, timeout=300)
                    fb_out = fb_res["outcome"]
                    print(f"  [FALLBACK] {name} main line → {fb_out}")
                    na2, nd2 = next_action_for_outcome(fb_out, res["attempt"])
                    log_call({**log_data, "phone_used": res["phone"],
                              "call_type": "MAIN_LINE_FALLBACK", "outcome": fb_out,
                              "next_action": na2, "next_action_date": nd2, "vapi_call_id": fb_id,
                              "gatekeeper_status": fb_res.get("gatekeeper_status", "UNKNOWN")})

        if i + BATCH_SIZE < len(jobs) and called_this_run < remaining:
            print(f"\n  [GAP] Waiting 6 minutes before next batch...")
            time.sleep(GAP_BETWEEN_CALLS)

    print(f"\n{'='*56}")
    print(f"  Session complete. Calls this run: {called_this_run}")
    print(f"  Total today: {calls_today()}/{MAX_CALLS_PER_DAY}")
    print("="*56 + "\n")

if __name__ == "__main__":
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        print("[SKIP] Another retell_caller is already running — exiting.")
        lock_fd.close()
        exit(0)
    try:
        run()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

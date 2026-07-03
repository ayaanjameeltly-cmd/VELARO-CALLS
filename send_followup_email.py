"""
Velaro VAPI Follow-up Email Sender
Called automatically by vapi_caller.py after HOT/WARM/CALLBACK outcomes.
Also runnable standalone:
  python send_followup_email.py HOT "James Amaro" "Amaro Law Firm" email@example.com "Thursday" "morning"
"""

import smtplib
import sqlite3
import sys
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

GMAIL_SENDER   = os.getenv("GMAIL_SENDER", "")
GMAIL_PASSWORD = os.getenv("GMAIL_PASSWORD", "")
SITE_LINK      = os.getenv("DEMO_LINK", "https://velaroreach.com/")
CALENDLY_LINK  = os.getenv("CALENDLY_LINK", "https://calendly.com/velora1/audit")
DB             = os.path.join(os.path.dirname(__file__), "velaro.db")

# ── email templates ────────────────────────────────────────────
def hot_email(name: str, firm: str, best_day: str) -> tuple[str, str]:
    first = name.split()[0] if name and name != "Managing Partner" else "there"
    day_str = best_day if best_day else "this week"
    subject = f"Velaro — {firm} audit {day_str}"
    body = f"""Hey {first},

Brooke here — great speaking with you just now.

As promised, here's our site — you can see exactly what we do and book the 15-minute audit with Ayaan directly from there:

{SITE_LINK}

The demo happens live on the call, built around {firm}'s specific setup. Ayaan will walk you through the whole thing.

Talk soon.

Brooke
Velaro | velaroreach.com"""
    return subject, body


def warm_email(name: str, firm: str) -> tuple[str, str]:
    first = name.split()[0] if name and name != "Managing Partner" else "there"
    subject = f"Velaro — what we build for {firm}"
    body = f"""Hey {first},

Brooke from Velaro — just got off the phone with you.

Here's our site as promised:

{SITE_LINK}

Shows what we build for PI firms. When it makes sense, hit "Know Pricing" — books a 15-minute audit with Ayaan directly.

The demo is live on that call, built specifically around how {firm} handles intake right now.

Talk soon.

Brooke
Velaro | velaroreach.com"""
    return subject, body


def callback_reminder_email(name: str, firm: str, phone: str, callback_date: str) -> tuple[str, str]:
    first = name.split()[0] if name and name != "Managing Partner" else "there"
    subject = f"Talk {callback_date or 'soon'} — Ayaan from Velaro"
    body = f"""Hey {first},

Quick note — Ayaan will be calling {firm} {('on ' + callback_date) if callback_date else 'soon'}.

In the meantime, here's our site if you want to take a look:

{SITE_LINK}

Talk soon.

Brooke
Velaro | velaroreach.com"""
    return subject, body


# ── send via Gmail SMTP ────────────────────────────────────────
def send_email(to_email: str, subject: str, body: str) -> bool:
    if not GMAIL_SENDER or not GMAIL_PASSWORD:
        print(f"[EMAIL] Gmail credentials not set — would send to {to_email}")
        print(f"[EMAIL] Subject: {subject}")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"Brooke from Velaro <{GMAIL_SENDER}>"
        msg["To"]      = to_email
        msg["Reply-To"] = GMAIL_SENDER
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_SENDER, to_email, msg.as_string())

        print(f"[EMAIL] Sent {subject[:50]} → {to_email}")
        return True
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")
        return False


def log_email(outcome: str, name: str, firm: str, to_email: str, subject: str, success: bool):
    try:
        conn = sqlite3.connect(DB)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS vapi_emails (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                outcome    TEXT,
                lead_name  TEXT,
                firm_name  TEXT,
                to_email   TEXT,
                subject    TEXT,
                sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                success    INTEGER
            )
        """)
        conn.execute(
            "INSERT INTO vapi_emails (outcome, lead_name, firm_name, to_email, subject, success) VALUES (?,?,?,?,?,?)",
            (outcome, name, firm, to_email, subject, 1 if success else 0)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB LOG ERROR] {e}")


def dispatch(outcome: str, name: str, firm: str, email: str,
             best_day: str = "", best_time: str = "",
             phone: str = "", callback_date: str = ""):

    outcome = outcome.upper()

    if outcome == "HOT":
        subject, body = hot_email(name, firm, best_day)
    elif outcome == "WARM":
        subject, body = warm_email(name, firm)
    elif outcome == "CALLBACK":
        subject, body = callback_reminder_email(name, firm, phone, callback_date)
    else:
        print(f"[EMAIL] No template for outcome: {outcome}")
        return

    success = send_email(email, subject, body)
    log_email(outcome, name, firm, email, subject, success)


# ── CLI entrypoint ─────────────────────────────────────────────
if __name__ == "__main__":
    # args: outcome name firm email [best_day] [best_time] [phone] [callback_date]
    if len(sys.argv) < 5:
        print("Usage: python send_followup_email.py <outcome> <name> <firm> <email> [best_day] [best_time] [phone] [callback_date]")
        sys.exit(1)

    _, outcome, name, firm, email = sys.argv[:5]
    best_day      = sys.argv[5] if len(sys.argv) > 5 else ""
    best_time     = sys.argv[6] if len(sys.argv) > 6 else ""
    phone         = sys.argv[7] if len(sys.argv) > 7 else ""
    callback_date = sys.argv[8] if len(sys.argv) > 8 else ""

    dispatch(outcome, name, firm, email, best_day, best_time, phone, callback_date)

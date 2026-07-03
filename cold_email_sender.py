"""
Velaro Cold Email Sender — First Touch Blaster
Sends 3–5 emails per run, each using a different template so no two look alike.

Usage:
  python cold_email_sender.py                    # interactive: prompts for recipient list
  python cold_email_sender.py --file leads.csv   # CSV with columns: email,first_name,firm_name,city
  python cold_email_sender.py --stats            # show sent log

Spam avoidance baked in:
  - Plain text only (no HTML, no images, no tracking pixels)
  - No links in first touch
  - Randomized 90–180s delays between sends
  - Subject lines vary per template (no repeated patterns)
  - No spam trigger words (FREE, GUARANTEED, LIMITED TIME, etc.)
  - Sender name is personal ("AJ") not corporate
  - Each email body is structurally different — different length, opener, angle
  - Deduplication via DB — never sends twice to same address
"""

import smtplib, sqlite3, os, sys, csv, time, random
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr, make_msgid
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

DB = "velaro.db"
GMAIL = os.getenv("GMAIL_SENDER", "")
PASSWORD = os.getenv("GMAIL_PASSWORD", "")
SENDER_NAME = "AJ"
FROM_DISPLAY = f"{SENDER_NAME} | Velaro"

MIN_SENDS = 3
MAX_SENDS = 5
MIN_DELAY = 90   # seconds between emails
MAX_DELAY = 180

# ─── 5 TEMPLATES — each a completely different angle ──────────────────────────
# Rules: plain text, no links, under 160 words, different subject every time,
# different opener, different closer. Rotate so each recipient sees unique copy.

TEMPLATES = [
    # 1. Ultra short — one question, barely any pitch
    {
        "subject": "quick question",
        "body": """Hi {first_name},

When a potential client calls {firm_name} at 9pm about an accident — where does that call go?

Asking because that window is where most PI firms in {city} are quietly bleeding cases. Not to competitors who are better. To whoever picks up first.

We fix that. Happy to show you what it looks like if it's relevant.

— AJ""",
    },

    # 2. Narrative / story — reads like a peer sharing something
    {
        "subject": "{first_name} — after hours",
        "body": """Hi {first_name},

Had a call last week with a managing partner at a PI firm. They'd just hired their third intake coordinator in two years.

Same problem each time — calls coming in after hours, web forms sitting overnight, staff burning out from volume. Good people, wrong fix.

We built them something different. Every inquiry handled in under 60 seconds, automatically, regardless of time. Their intake coordinator now only touches warm, pre-qualified leads.

Not sure if this maps to {firm_name} — but curious what your current after-hours setup looks like.

AJ
Velaro""",
    },

    # 3. Punchy, direct — reads fast, no fluff
    {
        "subject": "calls after 5pm",
        "body": """Hi {first_name},

Most PI firms lose the majority of their after-hours cases not because they lack good attorneys — because nobody answered.

Someone calls at 7pm about a car accident. You're closed. They call two more firms. One picks up. They hire that firm.

We built a system that answers every call, qualifies the case, and books the consult — automatically, at any hour.

Worth 15 minutes to see if it makes sense for {firm_name}?

AJ""",
    },

    # 4. Numbers-led — for the analytical partner type
    {
        "subject": "3-5 cases a week",
        "body": """Hi {first_name},

The PI firms we work with were typically missing 3-5 inquiries a week before we stepped in. Not from lack of leads — from slow response time.

At a $40k average case value, that's $6k-$10k walking out the door every week. Per month it adds up to more than most firms spend on ads.

The fix: every inbound inquiry — call, web form, text — responded to in under 60 seconds, 24/7. Cases pre-qualified before an attorney needs to get involved.

Is this something {firm_name} has looked into, or is intake running smoothly right now?

AJ
Velaro""",
    },

    # 5. Soft observation — no pressure, feels personal
    {
        "subject": "something I noticed",
        "body": """Hi {first_name},

I work with PI firms in {city} on their intake systems — specifically the gap between when a potential client reaches out and when they actually hear back.

Most firms I talk to assume their response time is fine. Then we look at the actual data and it's 2-4 hours on web forms, after-hours calls going straight to voicemail.

One firm we worked with thought they were converting well. After we fixed intake response, their booked consultations went up 3x on the same lead volume.

No pitch here — just genuinely curious if that gap is something {firm_name} has looked at recently.

AJ""",
    },

    # 6. Indeed / hiring angle — for firms actively posting intake roles
    {
        "subject": "saw {firm_name} is hiring intake",
        "body": """Hi {first_name},

Noticed {firm_name} has an open intake role.

Before you fill it — worth knowing there's an alternative a few PI firms in {city} have gone with recently. Instead of another hire, they automated the intake process entirely. Every call answered, every web form responded to in under 60 seconds, cases pre-qualified automatically.

One firm cancelled their intake hire after seeing it. Saved $45k/year and handled more volume than the person would have.

Might be worth a quick look before you commit to the role. Happy to show you what it looks like.

AJ
Velaro""",
    },
]



# ─── DB ───────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cold_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            first_name TEXT,
            firm_name TEXT,
            city TEXT,
            template_index INTEGER,
            subject TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'sent',
            UNIQUE(email)
        )
    """)
    conn.commit()
    conn.close()


def already_sent(email: str) -> bool:
    conn = get_db()
    row = conn.execute("SELECT id FROM cold_emails WHERE email=?", (email.lower().strip(),)).fetchone()
    conn.close()
    return row is not None


def log_send(email, first_name, firm_name, city, tmpl_idx, subject, status="sent"):
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO cold_emails
           (email, first_name, firm_name, city, template_index, subject, status)
           VALUES (?,?,?,?,?,?,?)""",
        (email.lower().strip(), first_name, firm_name, city, tmpl_idx, subject, status),
    )
    conn.commit()
    conn.close()


# ─── EMAIL SEND ───────────────────────────────────────────────────────────────

def build_email(to_email: str, tmpl: dict, first_name: str, firm_name: str, city: str):
    subject = tmpl["subject"].format(first_name=first_name, firm_name=firm_name, city=city)
    body = tmpl["body"].format(first_name=first_name, firm_name=firm_name, city=city)
    return subject, body


def send_email(to_email: str, subject: str, body: str) -> bool:
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = formataddr((FROM_DISPLAY, GMAIL))
        msg["To"] = to_email
        msg["Reply-To"] = GMAIL
        msg["Message-ID"] = make_msgid(domain=GMAIL.split("@")[-1])
        # No HTML, no images, no tracking — plain text only

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(GMAIL, PASSWORD)
            server.sendmail(GMAIL, to_email, msg.as_string())
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        return False


# ─── MAIN SEND LOGIC ──────────────────────────────────────────────────────────

def run_batch(recipients: list):
    """
    recipients: list of dicts with keys: email, first_name, firm_name, city
    Sends 3–5 emails, rotating templates, with random delays.
    """
    # Filter already-sent
    unsent = [r for r in recipients if not already_sent(r["email"])]
    if not unsent:
        print("No new recipients — all have already been emailed.")
        return

    # Pick how many to send this run
    send_count = min(random.randint(MIN_SENDS, MAX_SENDS), len(unsent))
    batch = random.sample(unsent, send_count) if len(unsent) > send_count else unsent[:send_count]

    # Assign templates — shuffle so no two consecutive emails use same template
    tmpl_indices = list(range(len(TEMPLATES)))
    random.shuffle(tmpl_indices)
    # Extend if batch > 5
    assigned = [(tmpl_indices[i % len(tmpl_indices)]) for i in range(len(batch))]

    print(f"\nSending {send_count} emails this run (from {len(unsent)} unsent recipients)\n")
    print(f"{'─'*55}")

    sent_ok = 0
    for i, (recipient, tmpl_idx) in enumerate(zip(batch, assigned)):
        email = recipient["email"]
        first_name = recipient.get("first_name", "there")
        firm_name = recipient.get("firm_name", "your firm")
        city = recipient.get("city", "your city")
        tmpl = TEMPLATES[tmpl_idx]

        subject, body = build_email(email, tmpl, first_name, firm_name, city)

        print(f"[{i+1}/{send_count}] → {email}")
        print(f"        {first_name} @ {firm_name} ({city})")
        print(f"        Template {tmpl_idx + 1}: \"{subject}\"")

        ok = send_email(email, subject, body)
        status = "sent" if ok else "failed"
        log_send(email, first_name, firm_name, city, tmpl_idx, subject, status)

        if ok:
            sent_ok += 1
            print(f"        ✓ Sent")
        else:
            print(f"        ✗ Failed — logged")

        if i < len(batch) - 1:
            delay = random.randint(MIN_DELAY, MAX_DELAY)
            print(f"        Waiting {delay}s before next send...")
            time.sleep(delay)

    print(f"\n{'─'*55}")
    print(f"Done. {sent_ok}/{send_count} sent successfully.\n")


# ─── INPUT HELPERS ────────────────────────────────────────────────────────────

def load_from_csv(path: str) -> list:
    recipients = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            email = (row.get("email") or row.get("Email") or "").strip()
            if not email or "@" not in email:
                continue
            recipients.append({
                "email": email,
                "first_name": (row.get("first_name") or row.get("First Name") or "there").strip().split()[0],
                "firm_name": (row.get("firm_name") or row.get("Company") or "your firm").strip(),
                "city": (row.get("city") or row.get("City") or "your city").strip(),
            })
    return recipients


def load_interactive() -> list:
    print("\nEnter recipients one per line in format:")
    print("  email, first_name, firm_name, city")
    print("Example: james@smithlaw.com, James, Smith Law Firm, Houston")
    print("Press Enter twice when done.\n")
    recipients = []
    while True:
        line = input("> ").strip()
        if not line:
            break
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 1 or "@" not in parts[0]:
            print("  Skipping — invalid format")
            continue
        recipients.append({
            "email": parts[0],
            "first_name": parts[1] if len(parts) > 1 else "there",
            "firm_name": parts[2] if len(parts) > 2 else "your firm",
            "city": parts[3] if len(parts) > 3 else "your city",
        })
    return recipients


def show_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) FROM cold_emails").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM cold_emails WHERE DATE(sent_at)=DATE('now')"
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM cold_emails WHERE status='failed'"
    ).fetchone()[0]
    recent = conn.execute(
        "SELECT email, first_name, firm_name, subject, sent_at FROM cold_emails ORDER BY sent_at DESC LIMIT 10"
    ).fetchall()
    conn.close()

    print(f"""
╔══ Cold Email Sender Stats ═══════════════════╗
║  Total emailed:   {total:<5}                      ║
║  Sent today:      {today:<5}                      ║
║  Failed:          {failed:<5}                      ║
╚══════════════════════════════════════════════╝

Last 10 sends:""")
    for r in recent:
        print(f"  {r['sent_at'][:16]}  {r['first_name']:<12} {r['firm_name']:<28}  {r['subject'][:40]}")


# ─── ENTRY ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not GMAIL or not PASSWORD:
        print("ERROR: Set GMAIL_SENDER and GMAIL_PASSWORD in .env")
        sys.exit(1)

    init_db()

    if "--stats" in sys.argv:
        show_stats()
        sys.exit(0)

    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 >= len(sys.argv):
            print("ERROR: --file requires a path, e.g. --file leads.csv")
            sys.exit(1)
        path = sys.argv[idx + 1]
        recipients = load_from_csv(path)
        if not recipients:
            print(f"No valid recipients found in {path}")
            sys.exit(1)
        print(f"Loaded {len(recipients)} recipients from {path}")
    else:
        recipients = load_interactive()
        if not recipients:
            print("No recipients entered.")
            sys.exit(0)

    run_batch(recipients)
    show_stats()

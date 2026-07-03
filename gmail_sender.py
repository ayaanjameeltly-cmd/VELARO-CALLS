"""
Velaro Gmail Sequence Sender
Rotates across multiple Gmail accounts to send cold email sequences
Run: python gmail_sender.py
"""
import smtplib, sqlite3, json, os, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()
DB = 'velaro.db'

# ─── CONFIGURE YOUR GMAIL ACCOUNTS HERE ───────────────────────
# Add your Gmail accounts. Use App Passwords (not your Gmail password)
# Get App Password: myaccount.google.com → Security → 2-Step → App passwords
GMAIL_ACCOUNTS = [
    {"email": os.getenv("GMAIL_1", "yourgmail1@gmail.com"), "password": os.getenv("GMAIL_PASS_1", "")},
    {"email": os.getenv("GMAIL_2", "yourgmail2@gmail.com"), "password": os.getenv("GMAIL_PASS_2", "")},
    {"email": os.getenv("GMAIL_3", "yourgmail3@gmail.com"), "password": os.getenv("GMAIL_PASS_3", "")},
    {"email": os.getenv("GMAIL_4", "yourgmail4@gmail.com"), "password": os.getenv("GMAIL_PASS_4", "")},
]
GMAIL_ACCOUNTS = [a for a in GMAIL_ACCOUNTS if a["password"]]

FROM_NAME = "AJ from Velaro"
DAILY_LIMIT_PER_ACCOUNT = 80
DELAY_BETWEEN_SENDS = 45  # seconds between emails

# ─── EMAIL TEMPLATES ───────────────────────────────────────────
TEMPLATES = {
    "email_1": {
        "subject": "{firm_name} — after-hours intake losing cases?",
        "body": """Hi {first_name},

Quick question — when a potential client calls your firm at 9pm about an accident they were just in, what happens to that inquiry?

Most PI firms lose 40–60% of after-hours leads to whichever firm responds first. For a firm handling $30k–$150k cases, that's significant revenue walking out the door every week.

We recently built a 24/7 AI intake system for a firm — every call answered, every web inquiry responded to in under 60 seconds, pre-qualified and booked automatically. Intake conversion rate tripled in 30 days.

I have a 2-minute demo that shows exactly how the intake flow works. Worth a look?

{sender_name}
Velaro | velaro.co

P.S. If this isn't relevant, just reply "no thanks" and I won't reach out again."""
    },
    "email_2": {
        "subject": "Re: {firm_name} — after-hours intake",
        "body": """Hi {first_name},

Following up on this.

Short version: if someone gets in an accident tonight and calls three PI firms, they'll hire whoever calls back first. Our system makes sure that firm is yours — automatically, every time.

15 minutes this week? calendly.com/velaro

{sender_name}"""
    },
    "email_3": {
        "subject": "Closing the loop",
        "body": """Hi {first_name},

I'll assume the timing isn't right — no problem.

I'll leave you with this: the average PI firm misses 3–5 potential cases every week from slow intake response. At your case values, that's real money monthly.

If that changes, calendar's always open: calendly.com/velaro

{sender_name}"""
    }
}

def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_email_tables():
    c = get_db()
    c.executescript('''
        CREATE TABLE IF NOT EXISTS email_sequences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            email TEXT,
            first_name TEXT,
            firm_name TEXT,
            sequence_stage INTEGER DEFAULT 1,
            last_sent TIMESTAMP,
            next_send TIMESTAMP,
            replied INTEGER DEFAULT 0,
            opted_out INTEGER DEFAULT 0,
            sender_account TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS email_sends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sequence_id INTEGER,
            stage INTEGER,
            subject TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sender_account TEXT,
            status TEXT DEFAULT 'sent',
            FOREIGN KEY (sequence_id) REFERENCES email_sequences(id)
        );
        CREATE TABLE IF NOT EXISTS account_daily_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_email TEXT,
            send_date DATE,
            count INTEGER DEFAULT 0,
            UNIQUE(account_email, send_date)
        );
    ''')
    c.commit()
    c.close()

def get_account_count(account_email):
    c = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    row = c.execute('SELECT count FROM account_daily_counts WHERE account_email=? AND send_date=?', 
                    (account_email, today)).fetchone()
    c.close()
    return row['count'] if row else 0

def increment_account_count(account_email):
    c = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    c.execute('''INSERT INTO account_daily_counts (account_email, send_date, count) VALUES (?,?,1)
                 ON CONFLICT(account_email, send_date) DO UPDATE SET count=count+1''',
              (account_email, today))
    c.commit()
    c.close()

def get_next_account():
    """Pick account with lowest send count today"""
    accounts_with_capacity = []
    for acc in GMAIL_ACCOUNTS:
        count = get_account_count(acc['email'])
        if count < DAILY_LIMIT_PER_ACCOUNT:
            accounts_with_capacity.append((acc, count))
    if not accounts_with_capacity:
        return None
    accounts_with_capacity.sort(key=lambda x: x[1])
    return accounts_with_capacity[0][0]

def send_email(account, to_email, subject, body, from_name):
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{from_name} <{account['email']}>"
        msg['To'] = to_email
        msg['Reply-To'] = account['email']
        msg.attach(MIMEText(body, 'plain'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(account['email'], account['password'])
            server.sendmail(account['email'], to_email, msg.as_string())
        print(f"  ✓ Sent to {to_email} via {account['email']}")
        return True
    except Exception as e:
        print(f"  ✗ Failed {to_email}: {e}")
        return False

def add_to_sequence(lead_id, email, first_name, firm_name):
    """Add a lead to the email sequence"""
    c = get_db()
    existing = c.execute('SELECT id FROM email_sequences WHERE lead_id=?', (lead_id,)).fetchone()
    if existing:
        c.close()
        return False
    next_send = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c.execute('''INSERT INTO email_sequences (lead_id, email, first_name, firm_name, sequence_stage, next_send)
                 VALUES (?,?,?,?,1,?)''', (lead_id, email, first_name, firm_name, next_send))
    c.commit()
    c.close()
    print(f"  Added {first_name} at {firm_name} to sequence")
    return True

def import_leads_to_sequence():
    """Import all leads with emails that aren't in sequence yet"""
    c = get_db()
    leads = c.execute('''SELECT l.id, l.name, l.email, l.company FROM leads l
        WHERE l.email != '' AND l.email IS NOT NULL AND l.niche = 'law'
        AND l.id NOT IN (SELECT lead_id FROM email_sequences WHERE lead_id IS NOT NULL)''').fetchall()
    c.close()
    count = 0
    for lead in leads:
        first_name = lead['name'].split()[0] if lead['name'] else 'there'
        firm_name = lead['company'] or 'your firm'
        add_to_sequence(lead['id'], lead['email'], first_name, firm_name)
        count += 1
    if count: print(f"\nImported {count} new leads to email sequence")

def run_sequences():
    """Send emails to leads that are due"""
    import_leads_to_sequence()
    c = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    due = c.execute('''SELECT * FROM email_sequences 
        WHERE replied=0 AND opted_out=0 AND sequence_stage <= 3
        AND (next_send <= ? OR next_send IS NULL)
        ORDER BY next_send ASC''', (now,)).fetchall()
    c.close()
    if not due:
        print("\nNo emails due to send right now.")
        return
    print(f"\nEmails due: {len(due)}")
    sent = 0
    for seq in due:
        account = get_next_account()
        if not account:
            print("\nDaily limit reached for all accounts. Come back tomorrow.")
            break
        stage = seq['sequence_stage']
        template_key = f'email_{stage}'
        if template_key not in TEMPLATES:
            continue
        tmpl = TEMPLATES[template_key]
        subject = tmpl['subject'].format(firm_name=seq['firm_name'], first_name=seq['first_name'])
        body = tmpl['body'].format(
            first_name=seq['first_name'], firm_name=seq['firm_name'],
            sender_name=FROM_NAME
        )
        success = send_email(account, seq['email'], subject, body, FROM_NAME)
        if success:
            c = get_db()
            # Calculate next send date
            if stage == 1: days_to_next = 5
            elif stage == 2: days_to_next = 5
            else: days_to_next = None
            next_send = (datetime.now() + timedelta(days=days_to_next)).strftime('%Y-%m-%d %H:%M:%S') if days_to_next else None
            new_stage = stage + 1 if stage < 3 else 3
            c.execute('''UPDATE email_sequences SET sequence_stage=?, last_sent=?, next_send=?, sender_account=?
                WHERE id=?''', (new_stage, now, next_send, account['email'], seq['id']))
            c.execute('''INSERT INTO email_sends (sequence_id, stage, subject, sender_account)
                VALUES (?,?,?,?)''', (seq['id'], stage, subject, account['email']))
            c.commit()
            c.close()
            increment_account_count(account['email'])
            sent += 1
            if sent < len(due):
                time.sleep(DELAY_BETWEEN_SENDS)
    print(f"\nDone. Sent {sent} emails.")

def mark_replied(email):
    """Call this when someone replies — stops further emails"""
    c = get_db()
    c.execute('UPDATE email_sequences SET replied=1 WHERE email=?', (email,))
    c.commit()
    c.close()
    print(f"Marked {email} as replied — sequence paused")

def show_stats():
    c = get_db()
    total = c.execute('SELECT COUNT(*) FROM email_sequences').fetchone()[0]
    replied = c.execute('SELECT COUNT(*) FROM email_sequences WHERE replied=1').fetchone()[0]
    stage1 = c.execute('SELECT COUNT(*) FROM email_sequences WHERE sequence_stage=1 AND replied=0').fetchone()[0]
    stage2 = c.execute('SELECT COUNT(*) FROM email_sequences WHERE sequence_stage=2 AND replied=0').fetchone()[0]
    stage3 = c.execute('SELECT COUNT(*) FROM email_sequences WHERE sequence_stage=3 AND replied=0').fetchone()[0]
    today_sends = c.execute("SELECT COUNT(*) FROM email_sends WHERE DATE(sent_at)=DATE('now')").fetchone()[0]
    c.close()
    print(f"""
╔══ Email Sequence Stats ══════════════╗
║ Total in sequence:    {total:<5}          ║
║ Replied:              {replied:<5}          ║
║ Awaiting Email 1:     {stage1:<5}          ║
║ Awaiting Email 2:     {stage2:<5}          ║
║ Awaiting Email 3:     {stage3:<5}          ║
║ Sent today:           {today_sends:<5}          ║
╚══════════════════════════════════════╝""")
    for acc in GMAIL_ACCOUNTS:
        count = get_account_count(acc['email'])
        print(f"  {acc['email']}: {count}/{DAILY_LIMIT_PER_ACCOUNT} today")

if __name__ == '__main__':
    import sys
    init_email_tables()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == 'stats': show_stats()
        elif cmd == 'replied' and len(sys.argv) > 2: mark_replied(sys.argv[2])
        elif cmd == 'import': import_leads_to_sequence()
    else:
        show_stats()
        print("\nRunning sequences...")
        run_sequences()
        show_stats()

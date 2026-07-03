from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file
import sqlite3, csv, io, os, zipfile, json, subprocess, threading, uuid
from datetime import datetime, timedelta
from functools import wraps
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = 'velaro_secret_2025'
DB = 'velaro.db'

# ── Remotion Studio process management ────────────────────────────────────────
_remotion_proc = None
_remotion_dir  = os.path.join(os.path.dirname(__file__), 'remotion')

def _start_remotion(props_file=None):
    global _remotion_proc
    if _remotion_proc and _remotion_proc.poll() is None:
        _remotion_proc.terminate()
        try: _remotion_proc.wait(timeout=5)
        except: pass
    cmd = ['npx', 'remotion', 'studio', '--port', '3001']
    if props_file:
        cmd += ['--props', props_file]
    _remotion_proc = subprocess.Popen(
        cmd, cwd=_remotion_dir,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

def db():
    c = sqlite3.connect(DB, timeout=10, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    return c

def init_db():
    c = db()
    cur = c.cursor()
    cur.executescript('''
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            company TEXT, email TEXT, phone TEXT,
            linkedin_url TEXT, instagram_url TEXT,
            niche TEXT DEFAULT 'law',
            source TEXT DEFAULT 'manual',
            pipeline_stage TEXT DEFAULT 'new',
            score INTEGER DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS outreach_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            platform TEXT, action_type TEXT,
            pipeline_day INTEGER DEFAULT 0,
            content_sent TEXT, reply_received TEXT, reply_type TEXT,
            completed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (lead_id) REFERENCES leads(id)
        );
        CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, category TEXT,
            niche TEXT DEFAULT 'all', content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS content_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT DEFAULT 'linkedin', content TEXT,
            post_type TEXT, scheduled_date DATE,
            posted INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS linkedin_pipeline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER UNIQUE,
            stage TEXT DEFAULT 'day1',
            has_posts INTEGER DEFAULT -1,
            connected INTEGER DEFAULT 0,
            stage_updated TEXT DEFAULT CURRENT_TIMESTAMP,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS linkedin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_id INTEGER,
            stage TEXT,
            done_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS work_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            clock_in TIMESTAMP,
            clock_out TIMESTAMP,
            hours REAL DEFAULT 0,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS action_plan_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phase INTEGER,
            task_text TEXT,
            is_done INTEGER DEFAULT 0,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS instagram_posted (
            day INTEGER PRIMARY KEY,
            posted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            notes TEXT DEFAULT ''
        );
    ''')
    if not cur.execute('SELECT COUNT(*) FROM templates').fetchone()[0]:
        seed_templates(cur)
    # Add mystery-shop email if not already present (migration-safe)
    if not cur.execute("SELECT 1 FROM templates WHERE name='Cold Email 1 — Mystery Shop'").fetchone():
        cur.execute("INSERT INTO templates (name, category, niche, content) VALUES (?,?,?,?)", (
            'Cold Email 1 — Mystery Shop', 'email1', 'law',
            "Subject: I called [Firm Name] last [day] at [time]\n\n"
            "Hi [First Name],\n\n"
            "I called [Firm Name] at [time] on [day] — I was testing how PI firms in [City] handle after-hours inquiries.\n\n"
            "Here's what happened: [voicemail / answering service picked up / rang out with no answer].\n\n"
            "If I'd actually been injured in an accident, I would've called the next firm on the list.\n\n"
            "I build AI intake systems specifically for PI firms — 24/7 call answering that qualifies callers, captures case details, and books consultations automatically. Whether it's 2pm Tuesday or 11pm Saturday, every call gets answered.\n\n"
            "I've built this for PI practices specifically, so the system knows what to ask — accident type, injuries, fault, insurance — and routes serious cases straight to your calendar without involving your staff.\n\n"
            "Would a 3-minute demo be worth it this week? You can literally dial the number yourself and hear how it handles a live call.\n\n"
            "[Your name]\nVelaro | velaro.co"
        ))
    # LinkedIn daily session log
    cur.execute('''
        CREATE TABLE IF NOT EXISTS li_session_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE DEFAULT CURRENT_DATE,
            action TEXT NOT NULL,
            count INTEGER DEFAULT 0,
            UNIQUE(date, action)
        )
    ''')
    # LinkedIn manual outreach log
    cur.execute('''
        CREATE TABLE IF NOT EXISTS li_manual_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            firm TEXT,
            username TEXT,
            action TEXT DEFAULT 'dm',
            notes TEXT,
            sent_at DATE DEFAULT CURRENT_DATE,
            follow_up_date DATE,
            replied INTEGER DEFAULT 0,
            reply_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Cold email manual outreach log (Gmail, sent by hand — same shape as li_manual_log)
    cur.execute('''
        CREATE TABLE IF NOT EXISTS email_manual_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            firm TEXT,
            email TEXT,
            stage TEXT DEFAULT 'email1',
            notes TEXT,
            sent_at DATE DEFAULT CURRENT_DATE,
            follow_up_date DATE,
            replied INTEGER DEFAULT 0,
            reply_notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Migration-safe column additions
    for stmt in [
        "ALTER TABLE linkedin_pipeline ADD COLUMN voice_note_date TEXT",
        "ALTER TABLE content_queue ADD COLUMN posted_at TIMESTAMP",
    ]:
        try: cur.execute(stmt)
        except: pass
    # Seed action plan tasks on first run
    if not cur.execute('SELECT COUNT(*) FROM action_plan_tasks').fetchone()[0]:
        _seed_action_plan(cur)
    c.commit()
    c.close()

def _seed_action_plan(cur):
    tasks = [
        # Phase 0
        (0, 'Audit all 406 leads — confirm PI practice area, tag correct state (TX/FL/GA/other)'),
        (0, 'Add voice note + scarcity script as a quick-copy button on every LinkedIn pipeline lead card'),
        (0, 'Publish 3 of the 19 queued LinkedIn posts to catch up'),
        (0, 'Send voice notes to every already-accepted LinkedIn connection'),
        (0, 'Verify LinkedIn phone number if commenting is restricted'),
        # Phase 1
        (1, 'Push all 63 pipeline leads forward one stage where due'),
        (1, 'Enroll 40–60 new leads/week from the 334 untouched leads into Day 1 stage'),
        (1, 'Publish remaining 16 queued posts, roughly 1/day'),
        (1, 'Comment on 5–10 active PI lawyer posts daily'),
        (1, '5 genuine comments/day in Reddit/Facebook legal communities'),
        (1, 'Reach 50+ LinkedIn connections total'),
        # Phase 2
        (2, 'Full LinkedIn pace: 20 connections + 30 DMs/day'),
        (2, 'Run D0/D3/D7/D14 follow-up cadence on every connection'),
        (2, 'First discovery calls booked — use demo.html + ROI calculator live on the call'),
        (2, 'Send proposal within 2 hours of every call'),
        (2, 'Re-warm Gmail in background: 5–10 personal emails/day, no cold sends yet'),
        (2, 'Keep posting 5x/week, track which formats get replies'),
        # Phase 3
        (3, 'First client closes (~day 55)'),
        (3, 'Deliver 4-week build: VAPI voice bot, n8n workflow, GHL integration'),
        (3, 'Document every result obsessively — this becomes the case study'),
        (3, 'Never pause outreach during delivery — keep pipeline moving'),
        (3, 'Resume cautious cold email at 10–15/day once warm-up is clean'),
        # Phase 4
        (4, 'Second client closes'),
        (4, 'First monthly retainer payment lands'),
        (4, 'Post anonymised case study — outreach conversion should jump'),
        (4, 'Raise prices for new inbound enquiries'),
        (4, 'Re-enable full email cadence with case study proof included'),
        (4, 'Plan month 4 expansion into CA/NY'),
    ]
    cur.executemany('INSERT INTO action_plan_tasks (phase, task_text) VALUES (?,?)', tasks)

def seed_templates(cur):
    t = [
        ('LI Comment — Overwhelmed with cases', 'li_comment', 'law', 'This is exactly where a client of ours was — leads coming in but the intake side couldn\'t keep up. Once the response system was fixed, qualified consultations went up significantly. You\'re identifying the right problem.'),
        ('LI Comment — Missed calls / after hours', 'li_comment', 'law', 'The after-hours gap is brutal in PI. Someone gets in an accident at 9pm, calls three firms, books with whoever picks up first. We solved this for a firm recently — the numbers were eye-opening.'),
        ('LI Comment — Lead quality from ads', 'li_comment', 'law', 'Interesting. What we found is it\'s rarely the leads — it\'s the 2-hour response window that kills conversions. Tested this properly with a firm and the difference was significant.'),
        ('LI Comment — Hiring intake staff', 'li_comment', 'law', 'We went a different direction for a client facing this — automated the intake side instead. Saved them $4k/month in staffing and honestly handled more volume. Happy to share how if useful.'),
        ('LI Comment — Growing the firm', 'li_comment', 'law', 'Scaling intake without losing quality is the hardest part of PI growth. Most firms solve it by hiring more staff when the fix is usually in the systems underneath.'),
        ('LI Connection Request', 'li_connect', 'law', 'Hey [Name] — commented on your post about [topic] earlier. Really resonated with what you shared. Worth connecting.'),
        ('LI DM after connect — PI Law', 'li_dm', 'law', 'Hey [Name], appreciate the connect.\n\nQuick question — when a potential client calls your firm at 9pm about an accident, what happens to that inquiry?\n\nWe build AI intake systems for PI firms — every call answered, every web inquiry responded to in 60 seconds, pre-qualified and booked automatically. Did this for a firm recently — intake conversion rate jumped significantly, zero cases missed after hours.\n\nI have a 2-min demo. Worth 15 minutes to see if it fits? [Calendly link]'),
        ('LI Follow-up DM', 'li_followup', 'law', 'Hey [Name] — bumping this in case it got buried.\n\nNot pushing — genuinely curious: is after-hours intake response something you\'re actively working on, or already solved?'),
        ('Cold Email 1 — PI Law', 'email1', 'law', 'Subject: [Firm name] — after-hours intake losing cases?\n\nHi [First Name],\n\nQuick question — when a potential client calls your firm at 9pm about an accident they were just in, what happens to that inquiry?\n\nMost PI firms we talk to lose 40–60% of after-hours leads to whichever firm responds first. For a firm handling $8k–$50k cases, that\'s significant revenue walking out the door every week.\n\nWe recently built a 24/7 AI intake system for a firm — every call answered, every web inquiry responded to in under 60 seconds, pre-qualified and booked automatically. Intake conversion rate tripled in 30 days.\n\nI have a 2-minute demo that shows exactly how the intake flow works. Worth a look?\n\n[Your name]\nVelaro | velaro.co'),
        ('Cold Email 1 — Mystery Shop', 'email1', 'law', 'Subject: I called [Firm Name] last [day] at [time]\n\nHi [First Name],\n\nI called [Firm Name] at [time] on [day] — I was testing how PI firms in [City] handle after-hours inquiries.\n\nHere\'s what happened: [voicemail / answering service picked up / rang out with no answer].\n\nIf I\'d actually been injured in an accident, I would\'ve called the next firm on the list.\n\nI build AI intake systems specifically for PI firms — 24/7 call answering that qualifies callers, captures case details, and books consultations automatically. Whether it\'s 2pm Tuesday or 11pm Saturday, every call gets answered.\n\nI\'ve built this for PI practices specifically, so the system knows what to ask — accident type, injuries, fault, insurance — and routes serious cases straight to your calendar without involving your staff.\n\nWould a 3-minute demo be worth it this week? You can literally dial the number yourself and hear how it handles a live call.\n\n[Your name]\nVelaro | velaro.co'),
        ('Cold Email 2 — Follow-up', 'email2', 'law', 'Subject: Re: [Firm name] — after-hours intake\n\nHi [First Name],\n\nFollowing up on this.\n\nShort version: if someone gets in an accident tonight and calls three PI firms, they\'ll hire whoever calls back first. Our system makes sure that firm is yours — automatically, every time.\n\n15 minutes this week? [Calendly link]\n\n[Your name]'),
        ('Cold Email 3 — Breakup', 'email3', 'law', 'Subject: Closing the loop\n\nHi [First Name],\n\nI\'ll assume the timing isn\'t right — no problem.\n\nI\'ll leave you with this: the average PI firm misses 3–5 potential cases every week from slow intake response. At average case values, that\'s real money monthly.\n\nIf that changes, calendar\'s always open: [Calendly link]\n\n[Your name]'),
        ('Objection — We have a receptionist', 'objection', 'law', 'That\'s great — this doesn\'t replace them. It handles everything they physically can\'t: 11pm calls, 3am web forms, five simultaneous inquiries. Your team focuses on the warm leads, the AI handles the volume they\'d miss.'),
        ('Objection — Tried something like this', 'objection', 'law', 'I hear that often. Usually it was a generic chatbot not built for PI intake specifically. Everything we build is completely custom — the qualification questions, the case type filtering, the booking flow. That\'s why I show a demo first, so you can see exactly what it looks like before deciding anything.'),
        ('Objection — Too expensive', 'objection', 'law', 'Fair. What\'s one missed case worth to your firm? [They answer.] Our setup fee is less than that. After that it runs automatically. How many inquiries a week are you currently not reaching in time?'),
        ('Objection — Not running ads', 'objection', 'law', 'Totally fine — the intake system works for organic leads too. Website forms, Google My Business calls, referrals. The point is no inquiry goes unanswered regardless of where it comes from.'),
        ('Discovery Call Open', 'discovery', 'law', 'Tell me about the firm — how many attorneys, and where do most of your cases come from?'),
        ('Discovery Call Dig', 'discovery', 'law', '• When someone calls after hours about an accident, what\'s your current process?\n• What\'s your average response time to a new web inquiry?\n• Are you running any paid ads? What\'s the monthly spend?\n• What\'s your average case value?\n• How many inquiries a week do you estimate don\'t convert because of response time?'),
        ('Discovery Call ROI Close', 'discovery', 'law', '"Based on what you\'ve told me — missing [X] inquiries a week at [Y] average case value — recovering even 30% of those is [Z]/month. Our retainer is [price]. You\'re profitable inside the first month."\n\n"Setup is [price] — we build everything, install, test, go live in 4 weeks. Monthly retainer is [price] to keep it optimised. Want to move forward this week?"'),
        ('LI Warm Email — Referenced post', 'li_warm_email', 'law', 'Subject: your post about [post topic]\n\nHi [First Name],\n\nLiked your post about [post topic] on LinkedIn — and left a comment on it too.\n\nThe point you made about [specific line or idea from post] is exactly the problem I work on for PI firms.\n\nShort version: most PI firms lose 40–60% of after-hours inquiries to whichever firm responds first. The attorneys posting about [intake / growth / staffing] are usually the ones who\'ve already felt this.\n\nI build 24/7 AI intake specifically for PI practices — every call answered in under 60 seconds, qualified, booked straight to the attorney\'s calendar. No staff involved.\n\nWorth 15 minutes this week to see if it fits [Firm Name]?\n\n[Your name]\nVelaro | velaro.co'),
        ('LI Warm Email — After like + comment', 'li_warm_email', 'law', 'Subject: [Firm Name] — saw your post\n\nHi [First Name],\n\nI\'ve been following your content for a bit — liked and commented on a few posts.\n\nYou clearly think about [intake / growth / case volume] seriously. That\'s actually rare among PI partners.\n\nOne thing I\'ve noticed working with PI firms: the ones posting about growth are usually the ones already losing cases they don\'t know about — specifically from after-hours calls that go to voicemail.\n\nI fix that. 24/7 AI intake that answers every call, qualifies the case, and books the consultation automatically. Built it for PI practices specifically.\n\nHappy to show you a live demo — 15 minutes, no slides.\n\n[Your name]\nVelaro | velaro.co\n\nP.S. Reply "not interested" and I won\'t follow up.'),
    ]
    cur.executemany('INSERT INTO templates (name, category, niche, content) VALUES (?,?,?,?)', t)

def get_followup_count():
    c = db()
    two_days_ago = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    five_days_ago = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
    n = c.execute('''
        SELECT COUNT(DISTINCT l.id) FROM leads l
        LEFT JOIN outreach_log o ON l.id = o.lead_id
        WHERE l.pipeline_stage IN ('day1','day2','day3','day4','day5','replied','proposal')
        GROUP BY l.id
        HAVING (l.pipeline_stage IN ('day1','day2','day3','day4','day5') AND (MAX(o.completed_at) < ? OR MAX(o.completed_at) IS NULL))
            OR (l.pipeline_stage = 'replied' AND MAX(o.completed_at) < ?)
            OR (l.pipeline_stage = 'proposal' AND MAX(o.completed_at) < ?)
    ''', (two_days_ago, two_days_ago, five_days_ago)).fetchone()
    c.close()
    return n[0] if n else 0

@app.context_processor
def inject_globals():
    return dict(followup_count=get_followup_count(), now=datetime.now)

@app.route('/')
def dashboard():
    c = db()
    stages = ['new','day1','day2','day3','day4','day5','emailed','replied','call_booked','proposal','closed','lost']
    stage_counts = {s: c.execute('SELECT COUNT(*) FROM leads WHERE pipeline_stage=?',(s,)).fetchone()[0] for s in stages}
    total = c.execute('SELECT COUNT(*) FROM leads').fetchone()[0]
    week_ago = (datetime.now()-timedelta(days=7)).strftime('%Y-%m-%d')
    replies = c.execute("SELECT COUNT(*) FROM outreach_log WHERE reply_received!='' AND reply_received IS NOT NULL AND completed_at>?", (week_ago,)).fetchone()[0]
    calls = c.execute("SELECT COUNT(*) FROM leads WHERE pipeline_stage='call_booked'").fetchone()[0]
    closed = c.execute("SELECT COUNT(*) FROM leads WHERE pipeline_stage='closed'").fetchone()[0]
    two_days_ago = (datetime.now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    overdue = c.execute('''SELECT l.*,MAX(o.completed_at) as last_action FROM leads l
        LEFT JOIN outreach_log o ON l.id=o.lead_id
        WHERE l.pipeline_stage IN ('day1','day2','day3','day4','day5','replied')
        GROUP BY l.id HAVING last_action<?  OR last_action IS NULL ORDER BY last_action LIMIT 8''',(two_days_ago,)).fetchall()
    recent = c.execute('''SELECT o.*,l.name,l.company FROM outreach_log o
        JOIN leads l ON o.lead_id=l.id ORDER BY o.completed_at DESC LIMIT 10''').fetchall()

    # LinkedIn pipeline snapshot
    li_stages = c.execute('''
        SELECT lp.stage, COUNT(*) as cnt FROM linkedin_pipeline lp WHERE lp.stage != 'done'
        GROUP BY lp.stage ORDER BY lp.stage
    ''').fetchall()
    li_due = c.execute('''
        SELECT lp.*, l.name, l.company, l.linkedin_url,
               ROUND((JULIANDAY('now') - JULIANDAY(lp.stage_updated)) * 24) as hours_since
        FROM linkedin_pipeline lp JOIN leads l ON l.id = lp.lead_id
        WHERE lp.stage != 'done'
        AND (JULIANDAY('now') - JULIANDAY(lp.stage_updated)) * 24 >= 20
        ORDER BY lp.stage_updated ASC LIMIT 6
    ''').fetchall()
    li_today_counts = {}
    for row in c.execute('''SELECT stage, COUNT(*) FROM linkedin_actions
            WHERE DATE(done_at)=DATE('now') GROUP BY stage''').fetchall():
        action = LI_STAGE_ACTION.get(row[0], 'like')
        li_today_counts[action] = li_today_counts.get(action, 0) + row[1]
    li_total = c.execute("SELECT COUNT(*) FROM linkedin_pipeline WHERE stage!='done'").fetchone()[0]

    # Live pipeline stage counts for dynamic task list
    li_pipeline_counts = {}
    for row in c.execute("SELECT stage, COUNT(*) as cnt FROM linkedin_pipeline WHERE stage!='done' GROUP BY stage").fetchall():
        li_pipeline_counts[row['stage']] = row['cnt']

    # Connections accepted but no voice note sent yet
    voice_pending = c.execute(
        "SELECT COUNT(*) FROM linkedin_pipeline WHERE connected=1 AND (voice_note_date IS NULL OR voice_note_date='')"
    ).fetchone()[0]

    # Leads with no pipeline activity (new and not in LI pipeline)
    new_idle = c.execute(
        "SELECT COUNT(*) FROM leads WHERE pipeline_stage='new' AND id NOT IN (SELECT lead_id FROM linkedin_pipeline WHERE lead_id IS NOT NULL)"
    ).fetchone()[0]

    # Content streak — consecutive days with posted content
    posted_dates = [r[0] for r in c.execute(
        "SELECT DISTINCT DATE(posted_at) FROM content_queue WHERE posted=1 AND posted_at IS NOT NULL ORDER BY posted_at DESC LIMIT 60"
    ).fetchall()]
    today_date = datetime.now().date()
    content_streak = 0
    for i, d in enumerate(posted_dates):
        check_d = datetime.strptime(d, '%Y-%m-%d').date()
        if check_d == today_date - timedelta(days=i):
            content_streak += 1
        elif i == 0 and check_d == today_date - timedelta(days=1):
            # streak from yesterday still valid (haven't posted today yet)
            content_streak += 1
        else:
            break
    posts_this_week = c.execute(
        "SELECT COUNT(*) FROM content_queue WHERE posted=1 AND posted_at > date('now','-7 days')"
    ).fetchone()[0]
    posted_today = c.execute(
        "SELECT COUNT(*) FROM content_queue WHERE posted=1 AND DATE(posted_at)=DATE('now')"
    ).fetchone()[0]

    c.close()

    return render_template('dashboard.html', stage_counts=stage_counts, total=total,
        replies=replies, calls=calls, closed=closed, overdue=overdue, recent=recent,
        li_stages=li_stages, li_due=li_due, li_today_counts=li_today_counts,
        li_limits=LI_DAILY_LIMITS, li_total=li_total, li_stage_label=LI_STAGE_LABEL,
        li_pipeline_counts=li_pipeline_counts, voice_pending=voice_pending,
        new_idle=new_idle, content_streak=content_streak,
        posts_this_week=posts_this_week, posted_today=posted_today)

@app.route('/crm')
def crm():
    c = db()
    stages = ['new','day1','day2','day3','day4','day5','emailed','replied','call_booked','proposal','closed','lost']
    stage_counts = {s: c.execute('SELECT COUNT(*) FROM leads WHERE pipeline_stage=?',(s,)).fetchone()[0] for s in stages}

    # Email stats from Gmail Sender tables (may not exist yet)
    try:
        emails_sent_total = c.execute("SELECT COUNT(*) FROM gmail_sends").fetchone()[0]
        emails_today = c.execute("SELECT COUNT(*) FROM gmail_sends WHERE DATE(sent_at)=DATE('now','localtime')").fetchone()[0]
        active_sequences = c.execute("SELECT COUNT(*) FROM gmail_sequences WHERE replied=0 AND completed=0 AND paused=0").fetchone()[0]
        replied_sequences = c.execute("SELECT COUNT(*) FROM gmail_sequences WHERE replied=1").fetchone()[0]
    except Exception:
        emails_sent_total = emails_today = active_sequences = replied_sequences = 0

    # Leads with email activity
    try:
        leads_with_email = c.execute("""
            SELECT l.id, l.name, l.company, l.email, l.pipeline_stage, l.score,
                   l.phone, l.linkedin_url, l.updated_at,
                   COUNT(gs.id) as emails_sent,
                   MAX(gs.sent_at) as last_emailed,
                   seq.replied as seq_replied,
                   seq.next_email as seq_next,
                   seq.completed as seq_done,
                   seq.paused as seq_paused,
                   seq.next_send_at
            FROM leads l
            LEFT JOIN gmail_sends gs ON LOWER(TRIM(l.email))=LOWER(TRIM(gs.to_email))
            LEFT JOIN gmail_sequences seq ON LOWER(TRIM(l.email))=LOWER(TRIM(seq.lead_email))
            GROUP BY l.id
            ORDER BY last_emailed DESC NULLS LAST, l.updated_at DESC
            LIMIT 200
        """).fetchall()
    except Exception:
        leads_with_email = c.execute("SELECT * FROM leads ORDER BY updated_at DESC LIMIT 200").fetchall()

    # Recent sends
    try:
        recent_sends = c.execute("""
            SELECT gs.*, l.name, l.company FROM gmail_sends gs
            LEFT JOIN leads l ON LOWER(TRIM(gs.to_email))=LOWER(TRIM(l.email))
            ORDER BY gs.sent_at DESC LIMIT 15
        """).fetchall()
    except Exception:
        recent_sends = []

    c.close()
    return render_template('crm.html',
        stage_counts=stage_counts, stages=stages,
        emails_sent_total=emails_sent_total, emails_today=emails_today,
        active_sequences=active_sequences, replied_sequences=replied_sequences,
        leads=leads_with_email, recent_sends=recent_sends)

@app.route('/leads')
def leads():
    c = db()
    niche = request.args.get('niche','')
    stage = request.args.get('stage','')
    q = request.args.get('q','')
    missing = request.args.getlist('missing')
    has_fields = request.args.getlist('has')
    page = int(request.args.get('page',1))
    per = 25
    sql = 'SELECT * FROM leads WHERE 1=1'
    p = []
    sort = request.args.get('sort', 'newest')
    needs_research = request.args.get('needs_research', '')
    if niche: sql+=' AND niche=?'; p.append(niche)
    if stage == 'not_emailed':
        sql+=" AND pipeline_stage NOT IN ('emailed','replied','call_booked','proposal','closed','lost')"
    elif stage:
        sql+=' AND pipeline_stage=?'; p.append(stage)
    if needs_research: sql+=" AND notes LIKE '%[needs research]%'"
    if q: sql+=' AND (name LIKE ? OR company LIKE ? OR email LIKE ?)'; p+=[f'%{q}%']*3
    field_map = {
        'email': 'email',
        'linkedin': 'linkedin_url',
        'phone': 'phone',
        'instagram': 'instagram_url',
    }
    for f in missing:
        col = field_map.get(f)
        if col: sql += f' AND ({col} IS NULL OR {col}="")'
    for f in has_fields:
        col = field_map.get(f)
        if col: sql += f' AND ({col} IS NOT NULL AND {col}!="")'
    total = c.execute(sql.replace('SELECT *','SELECT COUNT(*)'),p).fetchone()[0]
    today_count = c.execute("SELECT COUNT(*) FROM leads WHERE DATE(created_at)=DATE('now')").fetchone()[0]
    needs_research_count = c.execute("SELECT COUNT(*) FROM leads WHERE notes LIKE '%[needs research]%'").fetchone()[0]
    order = 'created_at ASC' if sort == 'oldest' else 'created_at DESC'
    sql+=f' ORDER BY {order} LIMIT {per} OFFSET {(page-1)*per}'
    rows = c.execute(sql,p).fetchall()
    c.close()
    return render_template('leads.html', leads=rows, total=total, total_leads=total, page=page, per=per,
        niche=niche, stage=stage, q=q, missing=missing, has_fields=has_fields, sort=sort,
        today_count=today_count, needs_research_count=needs_research_count,
        needs_research=needs_research)

@app.route('/api/leads/check', methods=['GET'])
def api_check_lead():
    """Quick duplicate check by email or linkedin URL."""
    c = db()
    email   = (request.args.get('email') or '').strip().lower()
    linkedin = (request.args.get('linkedin') or '').strip()
    existing = None
    if email:
        existing = c.execute(
            "SELECT id, name, company FROM leads WHERE LOWER(TRIM(email))=?", (email,)
        ).fetchone()
    if not existing and linkedin:
        existing = c.execute(
            "SELECT id, name, company FROM leads WHERE TRIM(linkedin_url)=?", (linkedin,)
        ).fetchone()
    c.close()
    if existing:
        return jsonify({'duplicate': True, 'id': existing['id'],
                        'name': existing['name'], 'company': existing['company']})
    return jsonify({'duplicate': False})

@app.route('/api/leads/add', methods=['POST'])
def api_add_lead():
    data  = request.get_json() or {}
    name  = (data.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Name required'}), 400
    email    = (data.get('email') or '').strip().lower()
    linkedin = (data.get('linkedin') or '').strip()
    c = db()
    # Deduplication — check email then LinkedIn before inserting
    if email:
        dup = c.execute(
            "SELECT id, name FROM leads WHERE LOWER(TRIM(email))=?", (email,)
        ).fetchone()
        if dup:
            c.close()
            return jsonify({'ok': False, 'duplicate': True,
                            'id': dup['id'], 'existing_name': dup['name']})
    if linkedin:
        dup = c.execute(
            "SELECT id, name FROM leads WHERE TRIM(linkedin_url)=?", (linkedin,)
        ).fetchone()
        if dup:
            c.close()
            return jsonify({'ok': False, 'duplicate': True,
                            'id': dup['id'], 'existing_name': dup['name']})
    notes = f"Source: {data['source_url']}" if data.get('source_url') else ''
    c.execute('''INSERT INTO leads (name,company,email,phone,linkedin_url,niche,source,notes)
        VALUES (?,?,?,?,?,?,?,?)''', (
        name, data.get('company',''), email, data.get('phone',''),
        linkedin, 'law', 'scraper', notes
    ))
    lid = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.commit(); c.close()
    return jsonify({'ok': True, 'id': lid})

@app.route('/leads/add', methods=['GET','POST'])
def add_lead():
    if request.method=='POST':
        c = db()
        c.execute('''INSERT INTO leads (name,company,email,phone,linkedin_url,instagram_url,niche,source,score,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (
            request.form.get('name'), request.form.get('company'),
            request.form.get('email'), request.form.get('phone'),
            request.form.get('linkedin_url'), request.form.get('instagram_url'),
            request.form.get('niche','law'), request.form.get('source','manual'),
            int(request.form.get('score',0)),
            ('[needs research] ' + request.form.get('notes','')) if request.form.get('needs_research') else request.form.get('notes','')
        ))
        c.commit(); c.close()
        flash('Lead added successfully','success')
        return redirect(url_for('leads'))
    return render_template('add_lead.html')

@app.route('/leads/import', methods=['POST'])
def import_leads():
    files = request.files.getlist('file')
    if not files or not files[0].filename:
        flash('No file selected','error'); return redirect(url_for('leads'))
    niche_in = request.form.get('niche','law')

    apollo_fields = {'first name','last name','email','company','work email'}
    c = db(); imported = skipped = filtered = 0

    for f in files:
        if not f or not f.filename:
            continue
        content = f.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))
        fieldnames = [h or '' for h in (reader.fieldnames or [])]

        # Detect Google Maps scraper CSV
        # Matches both garbled headers (rllt__details, OSrXXb) and user-renamed ones (type, stars, reviews, phone, time, web)
        gmaps_signals = {'rllt', 'type', 'stars', 'reviews', 'time', 'web', 'pxqao'}
        is_gmaps = (
            any(any(sig in h.lower() for sig in gmaps_signals) for h in fieldnames) or
            not any(h.lower() in apollo_fields for h in fieldnames)
        )

        for row in reader:
            vals = list(row.values())

            if is_gmaps:
                # ── Google Maps scraper format ────────────────────────────
                import re as _re
                _PHONE_RE  = _re.compile(r'\+?1?[\s.\-]?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}')
                _CITY_RE   = _re.compile(r'([A-Z][a-zA-Z\s\.]+),\s*([A-Z]{2})\b')
                _RATING_RE = _re.compile(r'^\d(\.\d)?$')

                def _v(i): return (vals[i] if i < len(vals) else '').strip()

                company = (row.get('Company') or row.get('company') or _v(0) or '').strip()
                if not company:
                    skipped += 1; continue

                # Filter 1: must be Personal injury attorney OR Law firm
                # Use named 'type' column if present, otherwise fall back to position 1
                type_val = row.get('type') or row.get('Type') or _v(1)
                category = type_val.lstrip('·•· ').strip().lower()
                allowed = ('personal injury attorney', 'law firm')
                if not any(a in category for a in allowed):
                    filtered += 1; continue

                # Filter 2: skip Open 24 hours — check every column
                all_vals_lower = ' '.join(v.lower() for v in vals)
                if 'open 24 hours' in all_vals_lower:
                    filtered += 1; continue

                # Deduplicate
                if c.execute('SELECT id FROM leads WHERE company=? AND source=?', (company, 'google_maps')).fetchone():
                    skipped += 1; continue

                # Rating: find first value like "4.7" (column index 2 usually, but verify)
                google_rating = ''
                for i, v in enumerate(vals[2:6], 2):
                    if _RATING_RE.match(v.strip()):
                        google_rating = v.strip(); break

                # Review count: find first value like "(867)" or "867"
                review_count = ''
                for v in vals:
                    clean = v.strip().strip('()')
                    if clean.isdigit() and 1 < len(clean) <= 6:
                        review_count = clean; break

                # Phone: find by regex across all columns (position varies by export)
                phone = ''
                for v in vals:
                    m = _PHONE_RE.search(v)
                    if m:
                        phone = m.group(0).strip(); break

                # City: find "City, ST" pattern across all columns
                city = ''
                for v in vals:
                    m = _CITY_RE.search(v)
                    if m:
                        city = f"{m.group(1).strip()}, {m.group(2)}"; break

                # Practice areas: look for "Provides:" or "Personal injury" text
                practice = ''
                for v in vals:
                    v_clean = v.strip().lstrip('Provides:').strip()
                    if v_clean and 'personal injury' in v_clean.lower():
                        practice = v_clean[:120]; break

                # Website URL — skip Google Maps direction/search links, take first real firm URL
                def _is_real_site(url):
                    url = url.strip()
                    if not url.startswith('http'):
                        return False
                    skip = ('google.com/maps', 'google.com/search', 'maps.google',
                            'goo.gl/maps', 'maps.app.goo', 'facebook.com', 'yelp.com')
                    return not any(s in url for s in skip)

                website = next((v.strip() for v in vals if _is_real_site(v)), '')

                notes_parts = []
                if practice: notes_parts.append(f'Practice: {practice}')
                if website:  notes_parts.append(f'Source: {website}')
                c.execute(
                    '''INSERT INTO leads (name,company,email,phone,linkedin_url,niche,source,notes,google_rating,review_count,city)
                       VALUES (?,?,?,?,?,?,\'google_maps\',?,?,?,?)''',
                    (company, company, '', phone, '', niche_in,
                     '\n'.join(notes_parts), google_rating, review_count, city)
                )
                imported += 1

            else:
                # ── Apollo / standard CSV format ──────────────────────────
                name = f"{row.get('First Name','')} {row.get('Last Name','')}".strip() or row.get('Name','Unknown')
                email = row.get('Email','') or row.get('Work Email','')
                if email and c.execute('SELECT id FROM leads WHERE email=?',(email,)).fetchone():
                    skipped += 1; continue
                company  = row.get('Company','') or row.get('Organization','')
                phone    = row.get('Phone','') or row.get('Mobile Phone','')
                linkedin = row.get('LinkedIn URL','') or row.get('Person Linkedin Url','')
                title    = row.get('Title','') or row.get('Job Title','')
                google_rating = row.get('google_rating','') or row.get('Rating','') or row.get('Google Rating','')
                review_count  = row.get('review_count','') or row.get('Reviews','') or row.get('Review Count','') or row.get('Total Reviews','')
                city     = row.get('city','') or row.get('City','') or row.get('Location','')
                c.execute(
                    '''INSERT INTO leads (name,company,email,phone,linkedin_url,niche,source,notes,google_rating,review_count,city)
                       VALUES (?,?,?,?,?,?,\'apollo\',?,?,?,?)''',
                    (name, company, email, phone, linkedin, niche_in,
                     f'Title: {title}' if title else '', google_rating, review_count, city)
                )
                imported += 1

    c.commit(); c.close()
    parts = [f'Imported {imported} leads']
    if filtered: parts.append(f'{filtered} filtered (non-PI or 24hr)')
    if skipped:  parts.append(f'{skipped} duplicates skipped')
    flash('. '.join(parts) + '.', 'success')
    return redirect(url_for('leads'))

@app.route('/leads/<int:lid>', methods=['GET','POST'])
def lead_detail(lid):
    c = db()
    lead = c.execute('SELECT * FROM leads WHERE id=?',(lid,)).fetchone()
    if not lead: flash('Lead not found','error'); return redirect(url_for('leads'))
    if request.method=='POST':
        action = request.form.get('action')
        if action=='update_lead':
            c.execute('''UPDATE leads SET name=?,company=?,email=?,phone=?,linkedin_url=?,instagram_url=?,
                niche=?,score=?,pipeline_stage=?,notes=?,updated_at=CURRENT_TIMESTAMP WHERE id=?''', (
                request.form.get('name'), request.form.get('company'),
                request.form.get('email'), request.form.get('phone'),
                request.form.get('linkedin_url'), request.form.get('instagram_url'),
                request.form.get('niche'), int(request.form.get('score',0)),
                request.form.get('pipeline_stage'), request.form.get('notes'), lid
            ))
            c.commit(); flash('Lead updated','success')
        elif action=='log_outreach':
            c.execute('''INSERT INTO outreach_log (lead_id,platform,action_type,pipeline_day,content_sent,reply_received,reply_type,notes)
                VALUES (?,?,?,?,?,?,?,?)''', (
                lid, request.form.get('platform'), request.form.get('action_type'),
                int(request.form.get('pipeline_day',0)), request.form.get('content_sent',''),
                request.form.get('reply_received',''), request.form.get('reply_type',''),
                request.form.get('log_notes','')
            ))
            day = int(request.form.get('pipeline_day',0))
            day_map = {1:'day1',2:'day2',3:'day3',4:'day4',5:'day5'}
            if day in day_map:
                c.execute('UPDATE leads SET pipeline_stage=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(day_map[day],lid))
            if request.form.get('reply_received','').strip():
                c.execute("UPDATE leads SET pipeline_stage='replied',updated_at=CURRENT_TIMESTAMP WHERE id=?",(lid,))
            c.commit(); flash('Action logged','success')
        return redirect(url_for('lead_detail',lid=lid))
    history = c.execute('SELECT * FROM outreach_log WHERE lead_id=? ORDER BY completed_at DESC',(lid,)).fetchall()
    tmpls = c.execute('SELECT * FROM templates ORDER BY category,name').fetchall()
    c.close()
    stages = ['new','day1','day2','day3','day4','day5','emailed','replied','call_booked','proposal','closed','lost']
    return render_template('lead_detail.html', lead=lead, history=history, templates=tmpls, stages=stages)

@app.route('/leads/<int:lid>/stage', methods=['POST'])
def update_stage(lid):
    c = db()
    c.execute('UPDATE leads SET pipeline_stage=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
        (request.form.get('stage'),lid))
    c.commit(); c.close()
    return jsonify({'ok':True})

@app.route('/leads/export-selected', methods=['POST'])
def export_selected_leads():
    ids = [int(i) for i in request.form.get('ids','').split(',') if i.strip().isdigit()]
    if not ids:
        flash('No leads selected','error')
        return redirect(url_for('leads'))
    c = db()
    placeholders = ','.join('?' * len(ids))
    rows = c.execute(f'SELECT name,email,company,phone,linkedin_url,pipeline_stage,google_rating,review_count,city FROM leads WHERE id IN ({placeholders})', ids).fetchall()
    c.close()
    import csv as csv_mod, io as io_mod
    out = io_mod.StringIO()
    w = csv_mod.writer(out)
    w.writerow(['first_name','email','company','phone','linkedin_url','stage','google_rating','review_count','city'])
    for r in rows:
        first = (r['name'] or '').split()[0] if r['name'] else ''
        w.writerow([first, r['email'] or '', r['company'] or '', r['phone'] or '',
                    r['linkedin_url'] or '', r['pipeline_stage'] or '',
                    r['google_rating'] or '', r['review_count'] or '', r['city'] or ''])
    from flask import make_response
    resp = make_response(out.getvalue())
    resp.headers['Content-Type'] = 'text/csv'
    resp.headers['Content-Disposition'] = f'attachment; filename=velaro_leads_{len(rows)}.csv'
    return resp

@app.route('/leads/bulk', methods=['POST'])
def bulk_leads():
    data = request.get_json()
    action = data.get('action')
    ids = [int(i) for i in data.get('ids', [])]
    if not ids: return jsonify({'ok': False, 'error': 'no ids'})
    c = db()
    if action == 'reject':
        c.executemany('UPDATE leads SET pipeline_stage="lost",updated_at=CURRENT_TIMESTAMP WHERE id=?',
            [(i,) for i in ids])
    elif action == 'stage':
        stage = data.get('stage', 'new')
        c.executemany('UPDATE leads SET pipeline_stage=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
            [(stage, i) for i in ids])
    elif action == 'delete':
        c.executemany('DELETE FROM outreach_log WHERE lead_id=?', [(i,) for i in ids])
        c.executemany('DELETE FROM leads WHERE id=?', [(i,) for i in ids])
    c.commit(); c.close()
    return jsonify({'ok': True, 'affected': len(ids)})

@app.route('/leads/<int:lid>/delete', methods=['POST'])
def delete_lead(lid):
    c = db()
    c.execute('DELETE FROM outreach_log WHERE lead_id=?',(lid,))
    c.execute('DELETE FROM leads WHERE id=?',(lid,))
    c.commit(); c.close()
    flash('Lead deleted','success')
    return redirect(url_for('leads'))

@app.route('/pipeline')
def pipeline():
    c = db()
    stages = ['new','day1','day2','day3','day4','day5','replied','call_booked','proposal','closed']
    two_days_ago = (datetime.now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    data = {}
    for s in stages:
        data[s] = c.execute('''SELECT l.*,MAX(o.completed_at) as last_action,
            CASE WHEN MAX(o.completed_at)<? OR MAX(o.completed_at) IS NULL THEN 1 ELSE 0 END as overdue
            FROM leads l LEFT JOIN outreach_log o ON l.id=o.lead_id
            WHERE l.pipeline_stage=? GROUP BY l.id ORDER BY l.updated_at DESC''',
            (two_days_ago,s)).fetchall()
    c.close()
    return render_template('pipeline.html', data=data, stages=stages)

@app.route('/followups')
def followups():
    c = db()
    two = (datetime.now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    five = (datetime.now()-timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
    overdue = c.execute('''SELECT l.*,MAX(o.completed_at) as last_action FROM leads l
        LEFT JOIN outreach_log o ON l.id=o.lead_id
        WHERE l.pipeline_stage IN ('day1','day2','day3','day4','day5')
        GROUP BY l.id HAVING last_action<? OR last_action IS NULL ORDER BY last_action''',(two,)).fetchall()
    unreplied = c.execute('''SELECT l.*,MAX(o.completed_at) as last_action FROM leads l
        LEFT JOIN outreach_log o ON l.id=o.lead_id WHERE l.pipeline_stage='replied'
        GROUP BY l.id HAVING last_action<? ORDER BY last_action''',(two,)).fetchall()
    stale = c.execute('''SELECT l.*,MAX(o.completed_at) as last_action FROM leads l
        LEFT JOIN outreach_log o ON l.id=o.lead_id WHERE l.pipeline_stage='proposal'
        GROUP BY l.id HAVING last_action<? ORDER BY last_action''',(five,)).fetchall()
    c.close()
    return render_template('followups.html', overdue=overdue, unreplied=unreplied, stale=stale)

@app.route('/templates')
def templates_page():
    c = db()
    cat = request.args.get('cat','')
    sql = 'SELECT * FROM templates' + (' WHERE category=?' if cat else '') + ' ORDER BY category,name'
    rows = c.execute(sql,(cat,) if cat else ()).fetchall()
    c.close()
    cats = ['li_comment','li_connect','li_dm','li_followup','ig_dm','ig_followup','email1','email2','email3','objection']
    return render_template('templates_page.html', templates=rows, cats=cats, cat=cat)

@app.route('/templates/add', methods=['POST'])
def add_template():
    c = db()
    c.execute('INSERT INTO templates (name,category,niche,content) VALUES (?,?,?,?)',
        (request.form.get('name'), request.form.get('category'),
         request.form.get('niche','all'), request.form.get('content')))
    c.commit(); c.close()
    flash('Template added','success')
    return redirect(url_for('templates_page'))

@app.route('/content', methods=['GET','POST'])
def content():
    c = db()
    if request.method=='POST':
        c.execute('INSERT INTO content_queue (platform,content,post_type,scheduled_date) VALUES (?,?,?,?)',
            (request.form.get('platform','linkedin'), request.form.get('content'),
             request.form.get('post_type'), request.form.get('scheduled_date')))
        c.commit(); flash('Post added to queue','success')
        return redirect(url_for('content'))
    platform = request.args.get('platform','')
    post_type = request.args.get('post_type','')
    conditions = []
    params = []
    if platform: conditions.append('platform=?'); params.append(platform)
    if post_type: conditions.append('post_type=?'); params.append(post_type)
    where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''
    sql = f'SELECT * FROM content_queue {where} ORDER BY posted ASC, scheduled_date ASC'
    posts = c.execute(sql, params).fetchall()
    c.close()
    return render_template('content.html', posts=posts, platform=platform, post_type=post_type)

@app.route('/content/<int:pid>/posted', methods=['POST'])
def mark_posted(pid):
    c = db()
    c.execute('UPDATE content_queue SET posted=1, posted_at=CURRENT_TIMESTAMP WHERE id=?',(pid,))
    c.commit(); c.close()
    return jsonify({'ok':True})

@app.route('/content/<int:pid>/delete', methods=['POST'])
def delete_post(pid):
    c = db()
    c.execute('DELETE FROM content_queue WHERE id=?',(pid,))
    c.commit(); c.close()
    flash('Post deleted','success')
    return redirect(url_for('content'))

@app.route('/export/leads')
def export_leads():
    c = db()
    rows = c.execute('SELECT * FROM leads ORDER BY created_at DESC').fetchall()
    c.close()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['id','name','company','email','phone','linkedin','instagram','niche','source','stage','score','notes','created'])
    for r in rows:
        writer.writerow([r['id'],r['name'],r['company'],r['email'],r['phone'],r['linkedin_url'],r['instagram_url'],r['niche'],r['source'],r['pipeline_stage'],r['score'],r['notes'],r['created_at']])
    from flask import Response
    return Response(output.getvalue(), mimetype='text/csv',
        headers={'Content-Disposition':'attachment;filename=velaro_leads.csv'})


@app.route('/planner')
def planner():
    c = db()
    today = datetime.now().strftime('%A, %B %d %Y')
    two_days_ago = (datetime.now()-timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')
    pipeline_count = c.execute("SELECT COUNT(*) FROM leads WHERE pipeline_stage IN ('day1','day2','day3','day4','day5')").fetchone()[0]
    overdue_count = c.execute('''SELECT COUNT(DISTINCT l.id) FROM leads l
        LEFT JOIN outreach_log o ON l.id=o.lead_id
        WHERE l.pipeline_stage IN ('day1','day2','day3','day4','day5','replied')
        GROUP BY l.id HAVING MAX(o.completed_at)<? OR MAX(o.completed_at) IS NULL''',(two_days_ago,)).fetchone()
    overdue_count = overdue_count[0] if overdue_count else 0
    emails_today = c.execute("SELECT COUNT(*) FROM outreach_log WHERE platform='email' AND DATE(completed_at)=DATE('now')").fetchone()[0]
    calls_week = c.execute("SELECT COUNT(*) FROM leads WHERE pipeline_stage='call_booked' AND updated_at > date('now','-7 days')").fetchone()[0]
    overdue_leads = c.execute('''SELECT l.* FROM leads l
        LEFT JOIN outreach_log o ON l.id=o.lead_id
        WHERE l.pipeline_stage IN ('day1','day2','day3','day4','day5','replied')
        GROUP BY l.id HAVING MAX(o.completed_at)<? OR MAX(o.completed_at) IS NULL
        ORDER BY l.updated_at ASC LIMIT 10''',(two_days_ago,)).fetchall()
    todays_post = c.execute(
        "SELECT * FROM content_queue WHERE platform='linkedin' AND scheduled_date=DATE('now') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    c.close()
    return render_template('planner.html', today=today, pipeline_count=pipeline_count,
        overdue_count=overdue_count, emails_today=emails_today, calls_week=calls_week,
        overdue_leads=overdue_leads, todays_post=todays_post)

@app.route('/planner/reset')
def planner_reset():
    flash('Planner reset — start fresh today','success')
    return redirect(url_for('planner'))

@app.route('/calendar')
def calendar():
    c = db()
    from datetime import date
    today = date.today()
    week_num = today.isocalendar()[1]
    posts = c.execute('SELECT *, strftime("%w", scheduled_date) as day_of_week FROM content_queue ORDER BY scheduled_date DESC').fetchall()
    weeks = list(range(max(1, week_num-1), week_num+8))
    c.close()
    return render_template('calendar.html', posts=posts, weeks=weeks, current_week=week_num)

@app.route('/agents')
def agents():
    return render_template('agents.html')


@app.route('/qualify')
def qualify():
    c = db()
    unscored = c.execute("SELECT COUNT(*) FROM leads WHERE score=0 OR score IS NULL").fetchone()[0]
    recent_scored = c.execute("SELECT * FROM leads WHERE score > 0 ORDER BY updated_at DESC LIMIT 8").fetchall()
    c.close()
    return render_template('qualify.html', unscored=unscored, recent_scored=recent_scored)

@app.route('/qualify/bulk')
def qualify_bulk():
    c = db()
    leads = c.execute("SELECT * FROM leads WHERE score=0 OR score IS NULL ORDER BY created_at DESC").fetchall()
    c.close()
    return render_template('qualify_bulk.html', leads=leads)

@app.route('/leads/add_ajax', methods=['POST'])
def add_lead_ajax():
    try:
        c = db()
        c.execute('''INSERT INTO leads (name,company,email,phone,linkedin_url,instagram_url,niche,source,score,pipeline_stage,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (
            request.form.get('name'), request.form.get('company',''),
            request.form.get('email',''), request.form.get('phone',''),
            request.form.get('linkedin_url',''), request.form.get('instagram_url',''),
            request.form.get('niche','law'), request.form.get('source','manual'),
            int(request.form.get('score',0)), request.form.get('pipeline_stage','new'),
            request.form.get('notes','')
        ))
        lid = c.lastrowid
        c.commit(); c.close()
        return jsonify({'ok': True, 'id': lid})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

@app.route('/leads/<int:lid>/score', methods=['POST'])
def update_score(lid):
    c = db()
    c.execute('UPDATE leads SET score=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',
        (int(request.form.get('score',0)), lid))
    c.commit(); c.close()
    return jsonify({'ok': True})


@app.route('/playbook')
def playbook():
    return render_template('playbook.html')

@app.route('/content/seed-week', methods=['POST'])
def seed_week_posts():
    """Seed 20 days of PI law carousel content."""
    from datetime import date, timedelta as td
    c = db()
    today = date.today()
    posts = [
        # Day 1 — carousel
        ('linkedin', '''Most PI firms don't know they're bleeding cases.
Here are 5 signs it's already happening:

1. Leads come in overnight and nobody touches them until 9am
2. The "intake system" is a shared inbox and an Excel sheet
3. Spending $5k+/month on ads but closing less than 20% of inquiries
4. Intake staff is handling calls, admin, and follow-ups simultaneously
5. Call the firm at 8pm — you get voicemail

3 or more of these? The firm is losing $30k–$80k a month through a gap they haven't measured yet.

I've seen firms recover that in a single month just by fixing number 1.

DM me "AUDIT" — I'll tell you exactly where yours is leaking.''', 'carousel', str(today)),

        # Day 2 — observation
        ('linkedin', '''Called a Houston PI firm at 7:43pm last week.
Voicemail. Never got a callback.

I wasn't a real lead — just testing.

But if I'd just been hit by a car on the way home from work, I'd have called the next firm on the list within 5 minutes.

That's what's happening to real leads every single night.

Firms running $8k–$15k/month on Google Ads and losing 40% of inquiries after 5pm aren't losing because of bad ads.

They're losing because nobody picks up.''', 'observation', str(today + td(days=1))),

        # Day 3 — client_story
        ('linkedin', '''A Florida PI firm had 180 leads last month.
They signed 22 of them.

That's a 12% close rate on $12k/month in ad spend.

We looked at where the other 88% went:

— 34% never got a response within 24 hours
— 28% called after hours and hit voicemail
— 19% submitted a web form and waited 6+ hours
— The rest: follow-up stopped after one unanswered call

None of this was about the ads.

We fixed the response time and the after-hours gap.

Month 2: 41 cases from the same 180 inquiries.

Same ad spend. Same leads. Different system.''', 'client_story', str(today + td(days=2))),

        # Day 4 — educational
        ('linkedin', '''At 10pm someone calls your firm about a car accident.
Here's what most PI firms let happen next — and what actually should.

What usually happens: voicemail. Generic message. Callback promised for tomorrow morning.

What should happen:

They speak to an intake agent immediately. The agent asks the right questions:
— What happened?
— Were you injured? Did you get treatment?
— Was the other driver insured?
— Have you spoken to any attorneys yet?

Case gets pre-qualified. If it's a fit — consultation booked directly to the attorney's calendar.

Attorney walks in Monday with 3 pre-qualified consults already scheduled from the weekend.

No chasing. No cold callbacks. No cases missed.

That's what "24/7 intake" actually means in practice.''', 'educational', str(today + td(days=3))),

        # Day 5 — personal
        ('linkedin', '''I called a PI firm at midnight last week.
Three rings. Voicemail. Generic message.

I wasn't a real lead. Just wanted to feel what an accident victim feels when nobody picks up.

Sat there thinking — this is happening right now. Someone just got rear-ended. They're shaken. They Google PI lawyers. Pick the first three. Call all of them.

Whoever picks up first gets the case.

The other two never know they lost it.

That's the problem. Not the tech side — the human cost. One missed intake = $15k–$150k gone. And most firms don't know it's happening.''', 'personal', str(today + td(days=4))),

        # Day 6 — build
        ('linkedin', '''Built an intake flow last week that turns a 10pm accident call into a booked consultation.
Here's exactly how it works:

1. Lead calls or submits form → response in under 60 seconds, any time of day
2. Qualification: injury confirmed, treatment sought, liability clear
3. If qualified → consultation booked directly to attorney calendar, no back-and-forth
4. If not qualified → referral to partner firm + case logged
5. No response after 2 attempts → 5-touch follow-up sequence starts automatically

The attorney sees a calendar full of pre-qualified consultations.

No leads sitting in a shared inbox. No "I'll call them back tomorrow."

Took about 2 weeks to build and test. Now it runs without anyone touching it.''', 'build', str(today + td(days=5))),

        # Day 7 — carousel
        ('linkedin', '''Ask a PI managing partner these 5 questions.
Most can't answer a single one cold.

1. What's your average response time to a new web inquiry?
(Industry average: 3.5 hours. Top firms: under 5 minutes.)

2. What % of your leads came in after 5pm last month?
(Usually 40–60% — and usually hitting voicemail.)

3. What's your lead-to-consultation conversion rate?
(Most firms only track sign rate, not this.)

4. What's your consultation show rate?
(Under 70% means there's a booking or confirmation problem.)

5. What's your cost per signed case — not per lead?
(This number changes every conversation about ad spend.)

If you don't know all 5, you're optimizing blind.

DM me "AUDIT" — I'll help you find them.''', 'carousel', str(today + td(days=6))),

        # Day 8 — observation
        ('linkedin', '''A PI attorney in Atlanta told me he has a great intake team.
Then I asked what happens when someone calls at 9pm.

He paused.

"They get a callback in the morning."

I asked what his average case value is.

"$45k–$60k."

So a lead worth $50k sits until 9am while two other firms have already called them back.

He hadn't thought about it that way before.

That's the conversation most PI firms haven't had with themselves yet.''', 'observation', str(today + td(days=7))),

        # Day 9 — client_story
        ('linkedin', '''A Texas PI firm had 47 leads from the previous month that never received a callback.
47.

They were spending $6k/month on Google Ads. The managing partner thought the leads were bad.

We ran an audit. Some of those leads were 3 weeks old. A few had moved to other firms. A few still picked up when we called.

We sent a simple 3-message reactivation sequence to the whole list.

Results:
— 6 booked consultations that week
— 2 became signed cases
— $80k+ in potential fees

From leads they'd already paid for and written off.

The leads weren't bad. They were abandoned.''', 'client_story', str(today + td(days=8))),

        # Day 10 — educational
        ('linkedin', '''The firm that responds in 60 seconds gets the case.
The one that calls back at 9am is competing against a relationship that's already 12 hours old.

Here's why the timing matters so much in PI specifically:

First 2 hours after an accident: high stress, urgency, ready to act. They want help now.
2–6 hours: anxiety settling. Talking to family. Still looking.
6–24 hours: skepticism rising. May have already spoken to someone.
24+ hours: insurance company has likely called. Loyalty forming elsewhere.

Speed in PI law isn't a nice-to-have feature.

It's the whole game.''', 'educational', str(today + td(days=9))),

        # Day 11 — personal
        ('linkedin', '''I spent months building intake systems before I understood what the real problem was.
It wasn't technology.

After talking to 30+ PI attorneys, the actual problem is simpler.

Nobody has ever shown them what they're losing.

The real number: missed calls per week × close rate × average case value = money leaving out the back door every month.

When you put that number in front of a managing partner, the conversation changes completely.

The tech is easy. Getting someone to see the problem clearly — that's the real work.''', 'personal', str(today + td(days=10))),

        # Day 12 — build
        ('linkedin', '''A firm had 340 leads marked "dead" in their CRM — all from the past 6 months.
We sent 3 messages. 4 became signed cases.

The sequence:

Message 1: "Hi [name], this is [firm]. We spoke a few months back about your case. Are you still dealing with this?"

Message 2 (3 days later, no reply): "Just checking in. If you've found representation, no worries. If not — happy to chat."

Message 3 (7 days later): "Closing the loop. If your situation changes, we're here."

From 340 old leads:
— 28 replies
— 11 consultations booked
— 4 signed cases

All from leads they'd already paid for and written off.''', 'build', str(today + td(days=11))),

        # Day 13 — carousel
        ('linkedin', '''7 intake mistakes killing PI case conversions.
None of them are about the ads.

1. Letting web forms sit overnight with no response
2. Asking legal questions before building basic rapport
3. Promising "someone will call you back today"
4. Sending calls to voicemail during lunch breaks
5. Treating borderline cases as hard no's instead of referrals
6. Stopping follow-up after one unanswered call
7. Booking consultations 5+ days out instead of 48 hours

Any single one kills otherwise qualified cases.

Most PI firms have all 7 running simultaneously without realizing it.

DM me "AUDIT" — I'll tell you which ones are costing you the most.''', 'carousel', str(today + td(days=12))),

        # Day 14 — observation
        ('linkedin', '''The fastest-growing PI firms don't have bigger ad budgets.
They treat intake like a revenue function, not an admin task.

The ones treating intake as admin: underpay the role, understaff it, measure nothing.

The ones treating it as revenue: track response time weekly, have scripts, cover every hour, measure conversion rate per intake rep.

Same ad spend. Completely different outcomes.

The difference isn't budget. It's how they think about what happens after the lead comes in.''', 'observation', str(today + td(days=13))),

        # Day 15 — client_story
        ('linkedin', '''A solo PI attorney in Georgia was signing 4 cases a month from 60+ inquiries.
Two months later: 11 cases. Same leads.

He was working 60+ hours a week. Good at trying cases. Drowning in everything else.

Intake was: answer calls when you can, follow up when you remember.

We built a simple intake and follow-up system. Nothing complicated.

Every inquiry responded to within 10 minutes. Qualified during first contact. Consultation booked same day where possible. 5-touch follow-up for non-responses.

Month 1: 9 cases.
Month 2: 11 cases.

Same attorney. Same leads. Just stopped losing cases to slow process.

He also got 8 hours a week back because pre-qualified consults are shorter.''', 'client_story', str(today + td(days=14))),

        # Day 16 — educational
        ('linkedin', '''Most PI firms stop following up after one unanswered call.
The Day 14 message is often the one that converts.

The sequence almost nobody runs:

Day 1: Immediate response + qualification attempt
Day 3: "Still looking for help with your case?"
Day 7: Something useful — what to do after an accident, what to expect from a PI case
Day 14: "Last check-in — if your situation has changed, we're here."
Day 30: Archive

Most firms stop at Day 1. Some reach Day 3.

Nobody makes it to Day 14.

But that message regularly converts leads who were "just researching" a month ago and are now ready to move.

PI leads have long decision cycles. Your follow-up needs to match.''', 'educational', str(today + td(days=15))),

        # Day 17 — personal
        ('linkedin', '''I picked PI law because the math is different here.
Missing one lead = $15k–$150k gone forever.

I got there by working backwards from a single question: who has the highest cost for a missed lead?

Med spa: $800 treatment.
Real estate: $15k commission, maybe.
PI law: $15k–$150k case. Gone to whoever picked up first.

When the stakes are that high, even a 10% improvement in intake conversion is worth tens of thousands a month.

That's why I focus here. The ROI case writes itself.''', 'personal', str(today + td(days=16))),

        # Day 18 — build
        ('linkedin', '''Consultation show rate went from 54% to 81%.
One change made the difference.

Old process:
Lead calls → intake asks questions → "an attorney will call you back to schedule" → 2 days of back-and-forth → consultation booked for next week

New process:
Lead calls → intake qualifies → "let me book you right now while I have you" → consultation on the calendar in real time → confirmation sent automatically

The difference is momentum.

Book them while they're engaged. Don't create a gap for doubt, comparison shopping, or an insurance company call to fill.''', 'build', str(today + td(days=17))),

        # Day 19 — carousel
        ('linkedin', '''5 ways to sign more PI cases without touching your ad spend.
None of these require more leads.

1. Cut response time to under 5 minutes for all new inquiries
(Most firms average 3+ hours — this alone moves the needle)

2. Cover after-hours calls so nothing goes to voicemail after 5pm
(40–60% of PI inquiries come in outside business hours)

3. Build a 5-touch follow-up sequence instead of stopping at one attempt
(Most firms stop at 1)

4. Book consultations during first contact — not promised for later
(Show rate jumps 20–30 points)

5. Reactivate the last 6 months of "dead" leads in your CRM
(Average recovery: 8–15% of the list)

None of these need a bigger budget.

DM me "AUDIT" — I'll tell you which one to start with.''', 'carousel', str(today + td(days=18))),

        # Day 20 — observation
        ('linkedin', '''A PI attorney told me he closes every consultation he gets.
He was reaching 12 out of 85 monthly inquiries.

I asked how many consultations he does per month: "About 12."

I asked how many inquiries come in: "Maybe 80–90."

So he's closing 100% of consults — and getting to 14% of his leads.

Not because the attorneys are bad. Not because the cases are weak.

Because 73 inquiries are getting lost somewhere between "we got a call" and "attorney sat down with them."

Closing every consult means nothing if you're only getting to 14% of your leads.

That gap is where most PI firms have their biggest untouched opportunity.''', 'observation', str(today + td(days=19))),
    ]
    inserted = 0
    for platform, content, post_type, sched_date in posts:
        existing = c.execute(
            'SELECT id FROM content_queue WHERE scheduled_date=? AND platform=?',
            (sched_date, platform)
        ).fetchone()
        if not existing:
            c.execute(
                'INSERT INTO content_queue (platform, content, post_type, scheduled_date) VALUES (?,?,?,?)',
                (platform, content, post_type, sched_date)
            )
            inserted += 1
    c.commit(); c.close()
    flash(f'✅ {inserted} LinkedIn posts added to content queue (20-day PI law plan)', 'success')
    return redirect(url_for('content'))


# ── Website Scraper ────────────────────────────────────────────────────────

@app.route('/scraper')
def scraper_page():
    return render_template('scraper.html', title='Website Scraper')


@app.route('/api/scrape')
def scrape_stream():
    from flask import Response, stream_with_context
    import json as _json
    from scraper import scrape_website

    urls_raw = request.args.get('urls', '')
    urls = [u.strip() for u in urls_raw.splitlines() if u.strip()]

    def generate():
        seen_emails = set()
        seen_name_keys = set()
        total = 0

        for url in urls:
            if not url.startswith('http'):
                url = 'https://' + url
            yield f"data: {_json.dumps({'type': 'status', 'msg': f'Crawling {url} …'})}\n\n"
            try:
                leads = scrape_website(url)
                new_leads = []
                for lead in leads:
                    email = lead.get('email', '').lower().strip()
                    import re as _re
                    name_key = _re.sub(r'\W', '', lead.get('name', '').lower()) + '|' + \
                               _re.sub(r'\W', '', lead.get('company', '').lower())
                    if email and email in seen_emails:
                        continue
                    if not email and name_key in seen_name_keys:
                        continue
                    if email:
                        seen_emails.add(email)
                    seen_name_keys.add(name_key)
                    new_leads.append(lead)
                total += len(new_leads)
                yield f"data: {_json.dumps({'type': 'result', 'url': url, 'leads': new_leads, 'count': len(new_leads)})}\n\n"
            except Exception as e:
                yield f"data: {_json.dumps({'type': 'error', 'url': url, 'msg': str(e)})}\n\n"

        yield f"data: {_json.dumps({'type': 'done', 'total': total})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )


@app.route('/scraper/export', methods=['POST'])
def scraper_export():
    import json as _json
    leads_json = request.form.get('leads', '[]')
    try:
        leads = _json.loads(leads_json)
    except Exception:
        flash('Invalid export data.', 'error')
        return redirect(url_for('scraper_page'))

    c = db()
    imported = skipped = 0
    for lead in leads:
        email = lead.get('email', '').strip()
        if email and c.execute('SELECT id FROM leads WHERE email=?', (email,)).fetchone():
            skipped += 1
            continue
        notes_parts = []
        if lead.get('title'):
            notes_parts.append(f"Title: {lead['title']}")
        if lead.get('practice_areas'):
            notes_parts.append(f"Practice areas: {lead['practice_areas']}")
        if lead.get('source_url'):
            notes_parts.append(f"Source: {lead['source_url']}")
        if lead.get('vcard') and lead.get('vcard_url'):
            notes_parts.append(f"vCard: {lead['vcard_url']}")
        c.execute(
            '''INSERT INTO leads (name, company, email, phone, linkedin_url, niche, source, notes, city, updated_at)
               VALUES (?, ?, ?, ?, ?, 'law', 'website_scraper', ?, ?, CURRENT_TIMESTAMP)''',
            (lead.get('name', ''), lead.get('company', ''), email,
             lead.get('phone', ''), lead.get('linkedin', ''),
             '\n'.join(notes_parts), lead.get('location', ''))
        )
        imported += 1
    c.commit()
    c.close()
    flash(f'Imported {imported} leads. {skipped} duplicates skipped.', 'success')
    return redirect(url_for('leads'))


@app.route('/api/leads/websites')
def leads_websites():
    """Return website URLs for given lead IDs (extracted from notes Source: field or linkedin_url fallback)."""
    import re as _re
    ids_raw = request.args.get('ids', '')
    ids = [i.strip() for i in ids_raw.split(',') if i.strip().isdigit()]
    if not ids:
        return jsonify({'urls': []})
    placeholders = ','.join('?' * len(ids))
    c = db()
    rows = c.execute(f'SELECT notes, linkedin_url FROM leads WHERE id IN ({placeholders})', ids).fetchall()
    c.close()
    urls = []
    for row in rows:
        notes = row['notes'] or ''
        m = _re.search(r'Source:\s*(https?://\S+)', notes)
        if m:
            urls.append(m.group(1).rstrip('/'))
        elif row['linkedin_url'] and row['linkedin_url'].startswith('http') and 'linkedin.com' not in row['linkedin_url']:
            # Legacy: website was incorrectly stored in linkedin_url before the fix
            urls.append(row['linkedin_url'].rstrip('/'))
    # Deduplicate preserving order
    seen = set()
    unique = [u for u in urls if not (u in seen or seen.add(u))]
    return jsonify({'urls': unique})


# ── LinkedIn Pipeline ──────────────────────────────────────────────────────

LI_STAGE_NEXT = {
    'day1': 'day2', 'day2': 'day3', 'day3': 'day4',
    'day4': 'day5', 'day5': 'day7', 'day7': 'day10', 'day10': 'done'
}
LI_STAGE_LABEL = {
    'day1': 'D1 · Like posts',
    'day2': 'D2 · Comment',
    'day3': 'D3 · Connect',
    'day4': 'D4 · Company page',
    'day5': 'D5 · Cold email',
    'day7': 'D7 · DM',
    'day10': 'D10 · Follow-up',
    'done':  'Done',
}
LI_DAILY_LIMITS = {'like': 50, 'comment': 20, 'connect': 15, 'dm': 25}
LI_STAGE_ACTION = {
    'day1': 'like', 'day2': 'comment', 'day3': 'connect',
    'day4': 'like', 'day5': 'dm', 'day7': 'dm', 'day10': 'dm'
}

@app.route('/linkedin')
def linkedin_pipeline():
    c = db()
    # Auto-enroll any lead with a LinkedIn URL that isn't in the pipeline yet
    c.execute('''
        INSERT OR IGNORE INTO linkedin_pipeline (lead_id)
        SELECT id FROM leads
        WHERE linkedin_url IS NOT NULL AND linkedin_url != ''
          AND linkedin_url LIKE '%linkedin.com%'
          AND pipeline_stage NOT IN ('closed','lost')
    ''')
    c.commit()
    rows = c.execute('''
        SELECT lp.*, l.name, l.company, l.linkedin_url, l.phone, l.notes,
               ROUND((JULIANDAY('now') - JULIANDAY(lp.stage_updated)) * 24) as hours_since,
               DATE(lp.added_at) as batch_date
        FROM linkedin_pipeline lp
        JOIN leads l ON l.id = lp.lead_id
        WHERE lp.stage != 'done'
        ORDER BY lp.added_at ASC
    ''').fetchall()

    today = c.execute('''
        SELECT stage, COUNT(*) as cnt FROM linkedin_actions
        WHERE DATE(done_at) = DATE('now') GROUP BY stage
    ''').fetchall()
    c.close()

    daily_counts = {}
    for r in today:
        action = LI_STAGE_ACTION.get(r['stage'], 'like')
        daily_counts[action] = daily_counts.get(action, 0) + r['cnt']

    leads = [dict(r) for r in rows]
    return render_template('linkedin.html',
        leads=leads, stage_label=LI_STAGE_LABEL,
        daily_counts=daily_counts, limits=LI_DAILY_LIMITS,
        now=datetime.now)

@app.route('/api/linkedin/add', methods=['POST'])
def li_add():
    ids = request.get_json().get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'error': 'No IDs'})
    c = db()
    added = 0
    for lid in ids:
        try:
            c.execute('INSERT OR IGNORE INTO linkedin_pipeline (lead_id) VALUES (?)', (lid,))
            added += c.execute('SELECT changes()').fetchone()[0]
        except Exception:
            pass
    c.commit(); c.close()
    return jsonify({'ok': True, 'added': added})

@app.route('/api/linkedin/action', methods=['POST'])
def li_action():
    data = request.get_json() or {}
    lead_id = data.get('lead_id')
    action  = data.get('action', 'update')  # 'done' | 'update' | 'remove'
    has_posts = data.get('has_posts')       # True/False/None
    connected = data.get('connected')

    c = db()
    row = c.execute('SELECT * FROM linkedin_pipeline WHERE lead_id=?', (lead_id,)).fetchone()
    if not row:
        c.close(); return jsonify({'ok': False, 'error': 'Not in pipeline'})

    if action == 'remove':
        c.execute('DELETE FROM linkedin_pipeline WHERE lead_id=?', (lead_id,))
        c.commit(); c.close()
        return jsonify({'ok': True, 'stage': 'removed'})

    # Update has_posts / connected flags if provided
    if has_posts is not None:
        c.execute('UPDATE linkedin_pipeline SET has_posts=? WHERE lead_id=?',
                  (1 if has_posts else 0, lead_id))
    if connected is not None:
        c.execute('UPDATE linkedin_pipeline SET connected=? WHERE lead_id=?',
                  (1 if connected else 0, lead_id))

    current = row['stage']
    if action == 'done':
        # No posts on D1 = didn't actually like anything, don't count against limits
        no_posts = (has_posts is False) or (row['has_posts'] == 0)
        skip_log = (current == 'day1' and no_posts)
        if not skip_log:
            c.execute('INSERT INTO linkedin_actions (lead_id, stage) VALUES (?,?)', (lead_id, current))
        # No posts: skip D2 (comment) — go D1→D3
        if current in ('day1', 'day2') and no_posts:
            next_stage = 'day3'
        else:
            next_stage = LI_STAGE_NEXT.get(current, 'done')
        c.execute('UPDATE linkedin_pipeline SET stage=?, stage_updated=CURRENT_TIMESTAMP WHERE lead_id=?',
                  (next_stage, lead_id))
    # 'skip' just logs without advancing — useful for "not ready yet"

    c.commit(); c.close()
    return jsonify({'ok': True, 'stage': LI_STAGE_NEXT.get(current, 'done') if action=='done' else current})

@app.route('/api/linkedin/voice-note', methods=['POST'])
def li_voice_note():
    data = request.get_json() or {}
    lead_id = data.get('lead_id')
    sent = data.get('sent', True)
    c = db()
    if sent:
        c.execute("UPDATE linkedin_pipeline SET voice_note_date=DATE('now'), connected=1 WHERE lead_id=?", (lead_id,))
    else:
        c.execute("UPDATE linkedin_pipeline SET voice_note_date=NULL WHERE lead_id=?", (lead_id,))
    c.commit(); c.close()
    return jsonify({'ok': True})

# ── Attendance Tracker ────────────────────────────────────────────────────────

@app.route('/attendance')
def attendance():
    c = db()
    today = datetime.now().strftime('%Y-%m-%d')

    # Active session (clocked in, not yet out)
    active = c.execute(
        "SELECT * FROM work_sessions WHERE clock_out IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()

    # All sessions for stats
    sessions = c.execute(
        "SELECT * FROM work_sessions WHERE clock_out IS NOT NULL ORDER BY date DESC"
    ).fetchall()

    # Today's total hours
    today_hours = c.execute(
        "SELECT COALESCE(SUM(hours),0) FROM work_sessions WHERE date=? AND clock_out IS NOT NULL", (today,)
    ).fetchone()[0]

    # This week
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime('%Y-%m-%d')
    week_hours = c.execute(
        "SELECT COALESCE(SUM(hours),0) FROM work_sessions WHERE date>=? AND clock_out IS NOT NULL", (week_start,)
    ).fetchone()[0]

    # This month
    month_start = datetime.now().strftime('%Y-%m-01')
    month_hours = c.execute(
        "SELECT COALESCE(SUM(hours),0) FROM work_sessions WHERE date>=? AND clock_out IS NOT NULL", (month_start,)
    ).fetchone()[0]
    month_days = c.execute(
        "SELECT COUNT(DISTINCT date) FROM work_sessions WHERE date>=? AND clock_out IS NOT NULL", (month_start,)
    ).fetchone()[0]

    # Total all-time hours
    total_hours = c.execute(
        "SELECT COALESCE(SUM(hours),0) FROM work_sessions WHERE clock_out IS NOT NULL"
    ).fetchone()[0]

    # Streak — exclude future dates
    today_date = datetime.now().date()
    all_dates = [r['date'] for r in c.execute(
        "SELECT DISTINCT date FROM work_sessions WHERE clock_out IS NOT NULL AND date <= ? ORDER BY date DESC",
        (str(today_date),)
    ).fetchall()]
    streak = 0
    check = today_date
    for d in all_dates:
        d_date = datetime.strptime(d, '%Y-%m-%d').date()
        if d_date == check or d_date == check - timedelta(days=1):
            streak += 1
            check = d_date - timedelta(days=1)
        else:
            break

    # Heatmap: last 84 days (12 weeks)
    heatmap = {}
    for r in c.execute(
        "SELECT date, SUM(hours) as h FROM work_sessions WHERE date >= date('now','-84 days') AND clock_out IS NOT NULL GROUP BY date"
    ).fetchall():
        heatmap[r['date']] = round(r['h'], 1)

    # Recent sessions for log table (last 30)
    recent = c.execute(
        "SELECT * FROM work_sessions ORDER BY date DESC, id DESC LIMIT 30"
    ).fetchall()

    c.close()
    return render_template('attendance.html',
        active=active, today=today, today_hours=round(today_hours,1),
        week_hours=round(week_hours,1), month_hours=round(month_hours,1),
        month_days=month_days, streak=streak, total_hours=round(total_hours,1),
        heatmap=heatmap, recent=recent,
        now=datetime.now, timedelta=timedelta)

@app.route('/api/attendance/clock', methods=['POST'])
def attendance_clock():
    c = db()
    today = datetime.now().strftime('%Y-%m-%d')
    now   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    active = c.execute(
        "SELECT * FROM work_sessions WHERE clock_out IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if active:
        # Clock out — calculate hours
        ci = datetime.strptime(active['clock_in'], '%Y-%m-%d %H:%M:%S')
        hours = round((datetime.now() - ci).total_seconds() / 3600, 2)
        c.execute("UPDATE work_sessions SET clock_out=?, hours=? WHERE id=?",
                  (now, hours, active['id']))
        c.commit(); c.close()
        return jsonify({'ok': True, 'action': 'out', 'hours': hours})
    else:
        # Clock in
        c.execute("INSERT INTO work_sessions (date, clock_in) VALUES (?,?)", (today, now))
        c.commit(); c.close()
        return jsonify({'ok': True, 'action': 'in', 'clock_in': now})

@app.route('/api/attendance/log', methods=['POST'])
def attendance_log():
    data  = request.get_json() or {}
    date  = data.get('date','').strip()
    hours = float(data.get('hours') or 0)
    notes = (data.get('notes') or '').strip()
    if not date or hours <= 0:
        return jsonify({'ok': False, 'error': 'Date and hours required'})
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    c = db()
    c.execute("INSERT INTO work_sessions (date, clock_in, clock_out, hours, notes) VALUES (?,?,?,?,?)",
              (date, now, now, hours, notes))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/attendance/delete/<int:sid>', methods=['POST'])
def attendance_delete(sid):
    c = db()
    c.execute("DELETE FROM work_sessions WHERE id=?", (sid,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/instagram')
def instagram_page():
    c = db()
    posted_rows = c.execute('SELECT day, posted_at FROM instagram_posted').fetchall()
    c.close()
    posted_days = {row['day']: row['posted_at'] for row in posted_rows}
    return render_template('instagram.html', title='Instagram Content',
                           pi_props=_PI_PROPS, pi_titles=_PI_TITLES, posted_days=posted_days)

@app.route('/instagram/posted/toggle/<int:day>', methods=['POST'])
def instagram_posted_toggle(day):
    c = db()
    row = c.execute('SELECT day FROM instagram_posted WHERE day=?', (day,)).fetchone()
    if row:
        c.execute('DELETE FROM instagram_posted WHERE day=?', (day,))
        posted = False
        posted_at = None
    else:
        posted_at = datetime.now().strftime('%Y-%m-%d %H:%M')
        c.execute('INSERT INTO instagram_posted (day, posted_at) VALUES (?,?)', (day, posted_at))
        posted = True
    c.commit(); c.close()
    return jsonify({'ok': True, 'day': day, 'posted': posted, 'posted_at': posted_at})

# ── Instagram Video Editor ────────────────────────────────────────────────────
_REMOTION_DIR    = '/Users/ayaanjameel/video-editor'
_REMOTION_VIDEOS = os.path.join(_REMOTION_DIR, 'public', 'videos')
_REMOTION_EDITED = os.path.join(_REMOTION_DIR, 'public', 'edited')
_REMOTION_PROPS  = os.path.join(_REMOTION_DIR, 'public', 'current_props.json')
_REMOTION_STORY_PROPS = os.path.join(_REMOTION_DIR, 'public', 'current_story_props.json')
_NPX             = '/Users/ayaanjameel/.nvm/versions/node/v24.11.1/bin/npx'
_reel_render_jobs  = {}  # job_id -> {status, output_name, log, day, started_at, props}
_reel_render_queue = []  # FIFO list of job_ids waiting for their turn
_reel_render_lock  = threading.Lock()

_PI_PROPS = {
    # ── PINNED CAROUSELS — pin all three to LinkedIn profile ─────────────────

    -2: {"format":"carousel",
        "hookStat":"0",
        "hookLabel":"handoffs. You deal directly with who builds your system.",
        "lowerThird":"Most agencies take your money and disappear. We stay in.",
        "results":[
            "We build every system ourselves — the voice AI, the automation, the qualification logic, the booking flow. No white-labelling. No templates handed off. Built for your firm specifically.",
            "We only work with PI law firms. Not med spas, not e-commerce, not 'any business with leads.' PI intake is all we do — which means we get very good at it.",
            "Every system is built from scratch for your firm — your practice areas, your case qualification criteria, your booking flow, your firm's name on every call. Nothing templated.",
            "We stay on monthly retainer after go-live because systems need tuning. Month 2 is better than Month 1. Month 6 is better than Month 2. We're accountable to results, not just the build.",
        ],
        "ctaText":"1–2 firms a month · DM 'AUDIT' →",
        "textPops":[]},

    -1: {"format":"carousel",
        "hookStat":"4",
        "hookLabel":"weeks to full AI intake. Here's exactly what we build.",
        "lowerThird":"Every PI firm we talk to has the same gap — calls after 5pm go nowhere.",
        "results":[
            "Week 1 — Voice AI: every call answered under your firm's name, 24/7. Case details captured automatically. Caller never reaches voicemail again.",
            "Week 2 — AI Qualification: accident type, injuries, liability, insurance — all captured and filtered before an attorney touches it. Only serious cases get through.",
            "Week 3 — Auto-booking + Follow-up: qualified leads book straight to the attorney calendar. Non-converters go into 30/60/90-day follow-up sequences automatically.",
            "Week 4 — Go live + optimise: full handoff, call recordings reviewed together, system tuned to your exact case types and rejection criteria.",
        ],
        "ctaText":"Texas · Florida · Georgia · DM 'AUDIT' →",
        "textPops":[]},

    0: {"format":"carousel",
        "hookStat":"1–2",
        "hookLabel":"PI firms a month. Here's exactly what we do — and why.",
        "lowerThird":"Most PI firms lose 3–5 cases a week. Not bad leads. Nobody picked up.",
        "results":[
            "Velaro builds AI intake exclusively for personal injury law firms. Not a chatbot. Not an answering service. A full system custom-built around your practice areas, qualification criteria, and booking flow.",
            "Every call answered under your firm's name, 24/7 · Every web form responded to in under 60 seconds · Leads pre-qualified before an attorney gets involved · Consultation booked straight to the calendar",
            "The firms we work with recover 3–5 cases per month they were losing to whoever answered first. At $20k–$100k per PI case — that's real revenue sitting in your missed-call log.",
            "We only take on 1–2 firms a month. Not for exclusivity — because building this properly takes time. We'd rather do it right for a few firms than fast for many.",
        ],
        "ctaText":"Texas · Florida · Georgia · DM 'AUDIT' →",
        "textPops":[]},
    # ── WEEK 1 ──────────────────────────────────────────────────────────────
    1:  {"format":"reel",     "hookStat":"78%",   "hookLabel":"hire the FIRST PI firm to respond",             "lowerThird":"If someone calls at 11pm... what happens?",             "results":["60-sec AI response, 24/7","Auto-qualifies every case","Consultation booked instantly"],                  "ctaText":"DM me 'intake' →",    "textPops":[{"timeMs":8000,"text":"Your firm: voicemail","icon":"❌"},{"timeMs":18000,"text":"Our system: 60 seconds","icon":"✅"},{"timeMs":35000,"text":"3x consultations, 30 days","icon":"📈"}], "script":"Someone gets in a car accident at 11pm. They call three firms.\n\nVoicemail. Voicemail. One firm's AI answers in 60 seconds — asks the right questions, books the consultation.\n\nBy morning they've signed. Your firm never got a chance.\n\nWe build that AI system for PI firms — every inquiry answered in under 60 seconds, 24/7.\n\nOne client: tripled consultations in 30 days. Same ad spend. Zero extra staff.\n\nFollow if you want to see how."},
    2:  {"format":"carousel", "hookStat":"5",     "hookLabel":"signs your PI firm loses cases every week",     "lowerThird":"Most PI firms don't know what they're missing",          "results":["After-hours calls go unanswered","Web forms replied to next morning","No intake pre-qualification"],  "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Sign 1: Voicemail after 6pm","icon":"❌"},{"timeMs":22000,"text":"Sign 3: 4+ hour response","icon":"⏱️"},{"timeMs":38000,"text":"Sign 5: No intake system","icon":"🤖"}]},
    3:  {"format":"reel",     "hookStat":"60s",   "hookLabel":"or leads hire whoever responded first",      "lowerThird":"Stop posting 'call us anytime' if this is your reality", "results":["Leads call 3 PI firms simultaneously","First response wins the case","After-hours = biggest gap"],     "ctaText":"DM me 'demo' →",      "textPops":[{"timeMs":7000,"text":"Accident at 11pm","icon":"🚗"},{"timeMs":18000,"text":"Called 3 firms simultaneously","icon":"📞"},{"timeMs":35000,"text":"Hired whoever answered first","icon":"✅"}], "script":"Because someone WILL call you at 8pm. And if it goes to voicemail, you just handed that case to whoever picks up.\n\nHere's what AI intake actually does, step by step.\n\nLead comes in — call, form, text. AI responds in 60 seconds. Asks the right questions: accident type, injuries, insurance, fault.\n\nIf the case qualifies, it books straight to the attorney's calendar. No human needed.\n\nAfter-hours call? Voice AI answers. Captures everything.\n\nOne firm: zero missed after-hours contacts. Month one."},
    4:  {"format":"story",    "hookStat":"11",    "hookLabel":"missed calls last Saturday alone",           "lowerThird":"The math PI attorneys don't want to see",              "results":["11 missed calls × $30k avg","= $330k in potential cases","Recovered: 0"],                            "ctaText":"Follow for the fix →", "textPops":[{"timeMs":8000,"text":"11 missed calls Saturday","icon":"📵"},{"timeMs":20000,"text":"$30k average case value","icon":"💰"},{"timeMs":38000,"text":"$330k walked out the door","icon":"🚪"}], "storyScript":"Pulled up a client's call log this weekend just to check something. 11 missed calls. One day. Saturday. That's something like $300k sitting in a missed-call log. Didn't even tell them yet, still processing it myself. If you want me to check yours, DM me 'audit' — I'll run the numbers, no cost."},
    5:  {"format":"carousel", "hookStat":"$50k",  "hookLabel":"lost monthly to slow intake response",       "lowerThird":"Your PI firm is probably losing this. Every month.",     "results":["40% of after-hours leads lost","Average response: 4+ hours","Competitor wins with 60s reply"],   "ctaText":"Save this →",          "textPops":[{"timeMs":7000,"text":"5 leads/week after-hours","icon":"📊"},{"timeMs":20000,"text":"3 go elsewhere by morning","icon":"❌"},{"timeMs":38000,"text":"$50k/month walking out","icon":"💸"}]},
    6:  {"format":"reel",     "hookStat":"+8",    "hookLabel":"consultations from after-hours alone",       "lowerThird":"Texas PI firm. Same ads. Different system.",            "results":["Response: 3-5hrs → 60 seconds","Zero after-hours misses","Month 1: +8 consultations"],          "ctaText":"Book a call →",        "textPops":[{"timeMs":5000,"text":"Before: Voicemail after 5pm","icon":"❌"},{"timeMs":22000,"text":"After: 60-sec AI, 24/7","icon":"✅"},{"timeMs":38000,"text":"+8 consultations, month 1","icon":"📈"}], "script":"Texas PI firm. Six attorneys. Good lead volume — conversion was flat.\n\nBefore: 3 to 5 hour response times during the day. Voicemail after 5pm, all weekend.\n\nAfter: every inquiry answered in 60 seconds, 24/7. Voice AI on every after-hours call.\n\nFirst month — eight additional consultations. All from after-hours leads that previously went to voicemail.\n\nThey didn't change the ads. Didn't hire staff. Changed what happened after someone reached out.\n\nThat's the whole game."},
    7:  {"format":"story",    "hookStat":"9pm",   "hookLabel":"PI attorney at kid's game. Lead called.",       "lowerThird":"AI replaces the gaps, not your team.",                 "results":["Call answered in 2 rings","Full case details captured","Monday 10am consult booked"],             "ctaText":"DM 'intake' →",        "textPops":[{"timeMs":10000,"text":"9pm Friday — accident call","icon":"📞"},{"timeMs":24000,"text":"AI answered in 2 rings","icon":"🤖"},{"timeMs":38000,"text":"Consult booked automatically","icon":"📅"}], "storyScript":"Had a funny conversation with an attorney client yesterday. He said he used to dread his kid's baseball games because his phone wouldn't stop. Now it just doesn't ring for him anymore — the AI's handling it. He still makes every game. That's honestly the whole point of building this. DM 'intake' if you want to see how it actually works."},
    # ── WEEK 2 ──────────────────────────────────────────────────────────────
    8:  {"format":"reel",     "hookStat":"$15k",  "hookLabel":"in PI ads — half the leads were ghosts",        "lowerThird":"I found where their money was going.",                 "results":["+3 consultations in week one","Response: 4hrs → 60 seconds","Zero extra ad spend"],            "ctaText":"DM me 'audit' →",      "textPops":[{"timeMs":7000,"text":"Response time: 4 hrs 23 min","icon":"⏱️"},{"timeMs":20000,"text":"Cut to 60 seconds with AI","icon":"⚡"},{"timeMs":34000,"text":"+3 bookings in week one","icon":"📈"}], "script":"Client was spending $15k a month on PI ads. Conversion felt low, so we went looking for where the leads actually went.\n\nHalf of them weren't bad leads. They were good leads that got a 4-hour-23-minute response time.\n\nBy the time someone called back, the lead had already Googled two more firms and signed with whichever one picked up first.\n\nWe cut response time to 60 seconds with AI intake — every call, every form, answered immediately, qualified automatically.\n\nWeek one: three more consultations booked. Same $15k ad spend. Zero extra dollars spent finding them.\n\nThe leads were never ghosts. They just got tired of waiting."},
    9:  {"format":"carousel", "hookStat":"0",     "hookLabel":"calls missed when AI handles intake",         "lowerThird":"The exact flow that books consultations while you sleep","results":["Web form → 60s AI response","AI qualifies the PI case","Consult auto-booked"],                          "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Step 1: Lead submits form","icon":"📝"},{"timeMs":20000,"text":"Step 2: AI responds in 60s","icon":"⚡"},{"timeMs":38000,"text":"Step 3: Consult auto-booked","icon":"📅"}]},
    10: {"format":"reel",     "hookStat":"2-3×",  "hookLabel":"PI conversion. Same ad budget.",                "lowerThird":"The ad worked. The intake killed the lead.",            "results":["Leads responded in 60s, not 6hrs","No leads lost after-hours","Same budget — better conversion"],"ctaText":"Fix your intake →",   "textPops":[{"timeMs":9000,"text":"Ad clicked at 9pm","icon":"🖱️"},{"timeMs":18000,"text":"Waited. Got nothing.","icon":"⏳"},{"timeMs":28000,"text":"Hired competitor by 9am","icon":"❌"}], "script":"Someone clicks your PI ad at 9pm. Fills out the form. Then waits.\n\nThe ad did its job. It got them in the door. What happens after that is a different system entirely, and most firms never look at it.\n\nWe tested this with a client — same ad, same budget, only the intake changed. Leads got a response in 60 seconds instead of 6 hours.\n\nNo leads lost after-hours. Same money going into ads. 2 to 3 times better conversion on the exact same traffic.\n\nIf your ads are working and your numbers still feel flat, stop blaming the ad. Look at what happens in the first hour after the click."},
    11: {"format":"story",    "hookStat":"90",    "hookLabel":"days of PI lead data. These numbers hurt.",   "lowerThird":"I pulled 90 days of data. These shouldn't exist.",      "results":["62% got no after-hours reply","Average callback: 4hr 12min","40% hired elsewhere by morning"],"ctaText":"Follow for the fix →", "textPops":[{"timeMs":8000,"text":"90 days of lead data","icon":"📊"},{"timeMs":20000,"text":"62% no after-hours reply","icon":"😱"},{"timeMs":38000,"text":"40% signed elsewhere by 9am","icon":"❌"}], "storyScript":"Pulled 90 days of one firm's lead data last night just to check something, and had to read it twice. 62% of after-hours leads never got a reply. Not estimating — that's the real number. Kind of stuck with me. Following along? I'll show you what we changed for them."},
    12: {"format":"carousel", "hookStat":"60s",   "hookLabel":"is all AI needs to qualify any PI lead",     "lowerThird":"Everything you think about AI for lawyers is wrong",    "results":["Captures accident details","Filters liability & injuries","Books the consultation"],            "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"What happened?","icon":"🚗"},{"timeMs":20000,"text":"Injuries? Fault? Insurance?","icon":"📋"},{"timeMs":38000,"text":"Consultation booked instantly","icon":"📅"}]},
    13: {"format":"reel",     "hookStat":"30",    "hookLabel":"days tracked. 3 numbers made no sense.",     "lowerThird":"30-day lead audit — the data was hard to look at.",     "results":["40% hired another PI firm by 9am","$12k/month ads — half wasted","+8 consultations recovered"],  "ctaText":"Run your audit →",    "textPops":[{"timeMs":7000,"text":"62% — no response until morning","icon":"😱"},{"timeMs":20000,"text":"40% signed elsewhere by 9am","icon":"❌"},{"timeMs":42000,"text":"+8 consultations recovered","icon":"✅"}], "script":"Tracked 30 days of one PI firm's lead data because something felt off, and three numbers came back that didn't make sense at first.\n\n40% of their leads had already hired another firm by 9am — before the office even opened.\n\nThey were spending $12k a month on ads, and roughly half of that was functionally being handed to whichever competitor responded faster.\n\nWe installed AI intake. Same 30-day window, same ad spend, eight additional consultations recovered that would have gone to someone else.\n\nThe leads were never the problem. The 9-hour gap between click and callback was.\n\nIf you want your own numbers run, that audit's free — just ask."},
    14: {"format":"story",    "hookStat":"9",     "hookLabel":"weekend PI leads. All handled. Zero staff.",     "lowerThird":"Monday morning — attorney opens the CRM.",             "results":["9 weekend inquiries captured","All pre-qualified automatically","3 consultations already booked"],"ctaText":"DM 'intake' →",       "textPops":[{"timeMs":8000,"text":"Friday 6pm — AI takes over","icon":"🤖"},{"timeMs":22000,"text":"9 leads qualify themselves","icon":"📋"},{"timeMs":38000,"text":"Monday: 3 consults ready","icon":"✅"}], "storyScript":"Checked a client's dashboard Monday morning out of curiosity. 9 leads came in over the weekend. Nobody touched a single one — all handled automatically, 3 already booked before the team even logged in. That's the kind of Monday I want every firm to have."},
    # ── WEEK 3 ──────────────────────────────────────────────────────────────
    15: {"format":"reel",     "hookStat":"0",     "hookLabel":"missed contacts after AI install",           "lowerThird":"PI firm down the street already has this. You don't.",    "results":["11pm calls — answered instantly","Weekend forms — 60-sec response","5 simultaneous — all handled"],"ctaText":"DM 'intake' →",       "textPops":[{"timeMs":12000,"text":"Doesn't replace your attorneys","icon":"⚖️"},{"timeMs":24000,"text":"Replaces the gaps they can't fill","icon":"🔧"},{"timeMs":38000,"text":"Firm down the street has this","icon":"⚠️"}], "script":"Zero missed contacts. That's the number after this firm installed AI intake — not a slow month, the actual count.\n\n11pm calls about an accident — answered instantly, not voicemail. Weekend web forms — 60-second response, not Monday morning. Five people calling at once — every single one handled, not three busy signals.\n\nThis isn't replacing the attorneys. It's just not letting anything slip through anymore.\n\nHere's the part that should actually worry you: the firm down the street from this client already has this running. Has had it for months.\n\nYou're not deciding whether to compete with AI intake. You're deciding how much longer you compete without it."},
    16: {"format":"carousel", "hookStat":"4+",    "hookLabel":"intake channels. All leaking. All fixed.",   "lowerThird":"PI firms lose leads between click and callback.",      "results":["Phone → AI answers 24/7","Web form → 60s response","After-hours → zero gap"],                "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Channel 1: Phone — missed","icon":"📞"},{"timeMs":20000,"text":"Channel 2: Form — no reply","icon":"📝"},{"timeMs":38000,"text":"All 4 channels: fixed","icon":"✅"}]},
    17: {"format":"reel",     "hookStat":"$45k",  "hookLabel":"lost to one voicemail. 15 hours too late.",  "lowerThird":"The voicemail wasn't the problem. The gap was.",       "results":["Called at 6:17pm Thursday","Callback: 9am Friday (15hrs)","PI lead signed at 6:30pm Thursday"],  "ctaText":"DM me 'intake' →",    "textPops":[{"timeMs":8000,"text":"6:17pm — slip-and-fall call","icon":"📞"},{"timeMs":20000,"text":"Voicemail. Callback: 9am.","icon":"⏰"},{"timeMs":35000,"text":"Lead hired competitor by 8am","icon":"❌"}], "script":"A potential client called at 6:17pm on a Thursday about a serious slip-and-fall. Left a voicemail.\n\nSomeone called back Friday morning. 15 hours later.\n\nThe lead had already hired another firm by 8am. Called them at 6:30pm, they had a 24/7 line, answered in 2 rings, booked a Friday morning consult.\n\nThe case settled for $45,000. Zero of that went to this firm.\n\nThe voicemail wasn't the problem. The 15-hour gap was.\n\nWe installed AI intake. Now every call gets a text response in under 60 seconds — even if no one can pick up. The case details are captured. The consultation is booked.\n\nThat case doesn't walk out anymore. Follow to see exactly how."},
    18: {"format":"story",    "hookStat":"2",     "hookLabel":"firms. Same city. Same budget. Different results.","lowerThird":"Two PI firms. Same everything. Except one thing.", "results":["Firm A: 8 consultations/month","Firm B: 26 consultations/month","Difference: AI intake speed"],"ctaText":"Follow for more →",   "textPops":[{"timeMs":8000,"text":"Same city, same budget","icon":"⚖️"},{"timeMs":20000,"text":"Firm A: 8 consults/month","icon":"📉"},{"timeMs":35000,"text":"Firm B: 26 consults/month","icon":"📈"}], "storyScript":"Was comparing two firms today — same city, basically the same ad budget. One's pulling 8 consultations a month. The other's at 26. Same money in, wildly different results out. Wanna guess what the actual difference was? Follow along, I'll get into it."},
    19: {"format":"carousel", "hookStat":"TX+FL", "hookLabel":"PI firms building an edge you can't see yet","lowerThird":"Texas and Florida are moving first. Are you?",          "results":["TX: highest PI volume in US","FL: competitive = need the edge","GA: still open window"],       "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Texas: first movers winning","icon":"🤠"},{"timeMs":22000,"text":"Florida: speed = survival","icon":"🌴"},{"timeMs":38000,"text":"Georgia: still open","icon":"🍑"}]},
    20: {"format":"reel",     "hookStat":"8/10",  "hookLabel":"PI attorneys said voicemail after 6pm",      "lowerThird":"I asked 10 attorneys. Here's what it costs.",          "results":["5 calls/week after-hours","2–3 lost to whoever answered","$60k–$90k/month walking out"],     "ctaText":"DM 'intake' →",        "textPops":[{"timeMs":8000,"text":"8 out of 10: voicemail","icon":"📵"},{"timeMs":20000,"text":"$60k–$90k/month lost","icon":"💸"},{"timeMs":35000,"text":"2 firms had AI. Both winning.","icon":"🏆"}], "script":"Not judging — most firms don't have a choice. After 6pm, nobody's there.\n\nBut here's what that decision costs at scale.\n\nIf your firm gets 5 after-hours calls a week and you're on voicemail, you're probably speaking to maybe 2 of them the next day. At least 2–3 have already hired someone.\n\nAt a $30k average case value, that's $60k–$90k per month in cases going to whoever answered.\n\nNot the best firm. Just the one that picked up.\n\nThe 2 firms out of 10 that had something different in place — both had AI handling the after-hours line. One had it for 3 months. Their consultation numbers had nearly doubled.\n\nVoicemail is the most expensive thing in your intake stack. Follow to see what replaces it."},
    21: {"format":"story",    "hookStat":"9pm",   "hookLabel":"form submitted. What happened next.",        "lowerThird":"Before the system vs after. The gap was embarrassing.", "results":["BEFORE: reply next morning","AFTER: text in 47 seconds","PI case pre-qualified instantly"],         "ctaText":"Follow for more →",    "textPops":[{"timeMs":8000,"text":"Before: Reply next morning","icon":"❌"},{"timeMs":22000,"text":"After: Text in 47 seconds","icon":"⚡"},{"timeMs":38000,"text":"Case qualified before 10pm","icon":"✅"}], "storyScript":"Tested something last night for fun — filled out a law firm's contact form at 9pm just to see what happens. Normal firm: nothing till morning. A client using our system: text back in 47 seconds. Tiny test, but that gap is basically the whole business model."},
    # ── WEEK 4 ──────────────────────────────────────────────────────────────
    22: {"format":"reel",     "hookStat":"$20k",  "hookLabel":"in PI ads. Voicemail at 8pm.",               "lowerThird":"Ad worked perfectly. Intake lost the case.",           "results":["Lead clicked at 8:43pm","Filled form. Waited.","Signed elsewhere at 9:30pm"],                   "ctaText":"Fix your intake →",    "textPops":[{"timeMs":7000,"text":"$20k/month in ads","icon":"💰"},{"timeMs":18000,"text":"Lead clicks 8:43pm","icon":"🖱️"},{"timeMs":30000,"text":"Signs competitor by 9:30pm","icon":"❌"}], "script":"You spend $20k on ads. Someone clicks at 8:43pm, fills the contact form, and waits.\n\nYour office closes at 6. Nobody sees it until 9am.\n\nBut here's what happens on their end: they're stressed, probably still shaken from the accident. They submit to 3 firms. Yours, and two others.\n\nOne of those other firms has AI intake. They get a text in 47 seconds.\n\nBy the time your team arrives at 9am, that lead signed at 9:30pm last night.\n\n$20,000 in ads worked perfectly. The intake lost the case.\n\nThis is why ad spend without intake optimization is just paying for your competitor's clients.\n\nWe fix the intake side. Follow to see how the math changes."},
    23: {"format":"carousel", "hookStat":"3×",    "hookLabel":"PI case intake. Same ad spend. AI intake.",     "lowerThird":"The AI Revenue Engine — how it actually works.",        "results":["Meta ads bring leads in","AI responds in 60 seconds","Cases pre-qualified + booked"],         "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Step 1: Lead clicks ad","icon":"🖱️"},{"timeMs":22000,"text":"Step 2: AI responds in 60s","icon":"⚡"},{"timeMs":38000,"text":"Step 3: Consultation booked","icon":"📅"}]},
    24: {"format":"reel",     "hookStat":"$55k",  "hookLabel":"receptionist cost. Still goes home at 6pm.", "lowerThird":"Why hiring more staff won't fix your PI intake gap.",                "results":["Receptionist: $40k–$55k/year","Still offline after 6pm","AI: fraction of cost, 24/7"],        "ctaText":"DM me 'intake' →",    "textPops":[{"timeMs":8000,"text":"Receptionist: $55k/year","icon":"💸"},{"timeMs":20000,"text":"9pm accident call → voicemail","icon":"❌"},{"timeMs":35000,"text":"AI: 24/7, fraction of cost","icon":"🤖"}], "script":"A receptionist costs $40k-$55k a year. And they still go home at 6pm.\n\nThe firm that beats you on after-hours isn't spending more on staff. They have a system that doesn't clock out.\n\nHere's what I mean. You hire an intake coordinator — great during the day. But someone calls at 9pm on a Saturday about a serious car accident. The intake coordinator is at dinner. The call goes to voicemail. That case goes to whoever picked up.\n\nThe problem isn't staffing. The problem is that humans sleep and businesses don't.\n\nAI intake costs a fraction of a receptionist and handles 11pm calls, 3am web forms, and simultaneous inquiries without breaking a sweat.\n\nThe math isn't even close. Follow for the breakdown."},
    25: {"format":"story",    "hookStat":"340",   "hookLabel":"dead leads reactivated. 4 booked. $0 ads.",  "lowerThird":"Every PI firm has a graveyard in their CRM.",          "results":["340 dead leads, 6+ months old","AI reactivation: 2 weeks","4 responded. 4 booked consults."], "ctaText":"Follow for the system →","textPops":[{"timeMs":8000,"text":"340 dead leads in CRM","icon":"📊"},{"timeMs":22000,"text":"AI follow-up: 2 weeks","icon":"🤖"},{"timeMs":38000,"text":"4 consultations. $0 spend.","icon":"✅"}], "storyScript":"Was going through a client's CRM today and found something kind of wild — 340 leads just sitting there, untouched for 6+ months. Ran a quiet reactivation sequence, nothing pushy. 4 responded. All 4 booked. Zero ad spend. Your CRM probably has a graveyard like that too — worth a look."},
    26: {"format":"carousel", "hookStat":"7",     "hookLabel":"questions AI asks every PI lead. 60 seconds.","lowerThird":"The 7 questions that qualify every PI case.",          "results":["What happened + when?","Injuries? Fault? Insurance?","Availability → consult booked"],        "ctaText":"DM 'questions' →",     "textPops":[{"timeMs":7000,"text":"Q1: What happened?","icon":"🚗"},{"timeMs":22000,"text":"Q4: Fault + insurance?","icon":"⚖️"},{"timeMs":38000,"text":"Q7: Book the consult","icon":"📅"}]},
    27: {"format":"reel",     "hookStat":"3:47",  "hookLabel":"AM. Call came in. PI case was ready Monday.",   "lowerThird":"A call came in at 3:47am. Here's what happened.",      "results":["Voice AI answered in 2 rings","Full intake captured","Monday 10am consult confirmed"],        "ctaText":"DM me 'intake' →",    "textPops":[{"timeMs":8000,"text":"3:47am — accident call","icon":"🌙"},{"timeMs":20000,"text":"AI answered in 2 rings","icon":"🤖"},{"timeMs":35000,"text":"Case + consult ready Monday","icon":"✅"}], "script":"3:47am. Sunday. Someone called about a multi-vehicle accident they'd just been in.\n\nBefore we built the system — that call goes to voicemail. By 9am Monday, that person has made 3 more calls and probably retained whoever answered first.\n\nInstead, the voice AI picked up on the second ring. Asked about the accident, injuries, other parties involved. Told them a consultation was booked for Monday 10am. Sent a text confirmation.\n\nMonday morning the attorney opens the CRM to find:\n— Caller name and contact\n— Accident type: multi-vehicle, highway\n— Injuries: cervical strain, ER visit\n— Insurance: other party insured\n— Consultation: 10am today, confirmed\n\nSix hours of sleep. One new qualified case ready to go.\n\nThis is what \"no lead falls through\" actually looks like in practice."},
    28: {"format":"story",    "hookStat":"9→26",  "hookLabel":"consultations/month. Same firm. Same ads.",  "lowerThird":"30-day before vs after. Same PI firm.",               "results":["BEFORE: 9 consults, 4hr response","AFTER: 26 consults, 52s response","Same ads. Different intake."],"ctaText":"DM 'results' →",     "textPops":[{"timeMs":8000,"text":"Before: 9 consults/month","icon":"📉"},{"timeMs":22000,"text":"After: 26 consults/month","icon":"📈"},{"timeMs":38000,"text":"Same ads. Different system.","icon":"⚖️"}], "storyScript":"Pulled the 30-day before/after on a client today and honestly had to screenshot it. 9 consultations a month, then 26. Same firm, same ads, same everything — except response time. Still can't quite believe how big that gap turned out to be."},
    # ── DAYS 29-30 ──────────────────────────────────────────────────────────
    29: {"format":"carousel", "hookStat":"60s",   "hookLabel":"to qualify any PI lead. The exact framework.","lowerThird":"How to qualify a PI lead in 60 seconds.",             "results":["Injury → SOL → Fault","Insurance → Case type match","Score: book / review / decline"],        "ctaText":"DM 'qualify' →",       "textPops":[{"timeMs":7000,"text":"Step 1: Verify injury","icon":"🏥"},{"timeMs":22000,"text":"Step 3: Fault + insurance","icon":"⚖️"},{"timeMs":38000,"text":"Score 4-5: Book immediately","icon":"✅"}]},
    30: {"format":"reel",     "hookStat":"30",    "hookLabel":"days. One thing to say directly.",           "lowerThird":"If your PI firm is still on voicemail after-hours — read this.", "results":["First movers: 2–3 month edge","4 weeks to build + go live","Running 24/7 from day one"],      "ctaText":"DM me 'intake' →",    "textPops":[{"timeMs":8000,"text":"Market is shifting","icon":"📈"},{"timeMs":20000,"text":"First movers: 2-3 month edge","icon":"🏆"},{"timeMs":35000,"text":"4 weeks to build. 24/7 live.","icon":"🚀"}], "script":"30 days of posting about this. One thing I want to say directly.\n\nIf you're a PI attorney still using voicemail for after-hours calls — I'm not here to say you're doing it wrong. Most firms are. It's the default.\n\nBut the market is shifting. Slowly, then all at once.\n\nThe firms that move on this first get 2-3 months of exclusive advantage before it becomes table stakes. Every week you wait is a week your intake gap costs you cases to whoever responded faster.\n\nThe good news: it takes 4 weeks to build and go live. The intake system runs 24/7 from day one.\n\nIf you've been watching this content and wondering whether it applies to your firm — it does. DM me \"intake\" and I'll do a free audit of your current response time and show you where the gaps are.\n\nNo pitch. Just the data. You decide what to do with it."},

    # ── WEEK 5 ──────────────────────────────────────────────────────────────
    31: {"format":"reel",     "hookStat":"$94k",  "hookLabel":"is what the ROI calculator said before they even signed",   "lowerThird":"I ran their numbers before they were a client.",       "results":["Inputs: 6 missed calls/week, $32k avg case","Recoverable at 30% close rate: ~$94k/year","Setup cost: covered in under 6 weeks"], "ctaText":"Run your numbers →",   "textPops":[{"timeMs":8000,"text":"Just their real numbers","icon":"🔢"},{"timeMs":20000,"text":"~$94k/year recoverable","icon":"💰"},{"timeMs":35000,"text":"Payback: under 6 weeks","icon":"⏱️"}], "script":"Before anyone signs anything, I run their actual numbers through a calculator — no guessing, just their real call volume and case value.\n\nOne firm: about 6 missed calls a week, $32k average case value, and being honest about how many of those leads they'd realistically close.\n\nAt a conservative 30% close rate on recovered leads, that's roughly $94,000 a year just sitting in missed calls — not hypothetical, just what their own numbers say.\n\nSetup cost on a system like this pays for itself in under six weeks once it's live, and the math doesn't even need optimistic assumptions to work.\n\nI'm not asking firms to trust me on faith. I'm asking them to look at their own numbers first. If you want yours run the same way, that part's free — DM me \"intake.\""},
    32: {"format":"carousel", "hookStat":"5",     "hookLabel":"questions to ask before you spend another dollar on ads",   "lowerThird":"Most firms fix the wrong thing first.",                 "results":["1. What's our actual response time?","2. How many after-hours leads do we lose?","3. Could fixing intake beat raising ad spend?"], "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Don't raise spend yet","icon":"🛑"},{"timeMs":20000,"text":"Fix intake first","icon":"🔧"},{"timeMs":35000,"text":"Then scale the ads","icon":"📈"}]},
    33: {"format":"reel",     "hookStat":"14/20", "hookLabel":"PI firms didn't answer on the first call",                  "lowerThird":"I called 20 PI firms last week pretending to be a lead.", "results":["14/20: no answer, straight to voicemail","4/20: answered, asked zero questions","2/20: answered well, booked a callback"], "ctaText":"Where would you rank →", "textPops":[{"timeMs":7000,"text":"Called 20 PI firms","icon":"📞"},{"timeMs":20000,"text":"14 went to voicemail","icon":"❌"},{"timeMs":35000,"text":"Only 2 did it right","icon":"✅"}], "script":"Spent an afternoon calling 20 PI law firms, pretending to be someone who just got rear-ended.\n\n14 out of 20 went straight to voicemail. No callback within the hour for half of them.\n\n4 answered but didn't ask a single qualifying question — just took a name and number.\n\nOnly 2 out of 20 answered properly, asked about the accident, and got me a real time to talk to someone.\n\nThese aren't bad firms. Good attorneys, real case results on their websites. The phone is just the weakest part of the business, and almost nobody's looking at it.\n\nIf I called your firm right now, which one of these would you be?"},
    34: {"format":"story",    "hookStat":"4hrs",  "hookLabel":"to find a bug nobody will ever notice",                     "lowerThird":"Behind the scenes — the boring part of this job.",     "results":["Voice AI kept hanging up early","4 hours to find one bad timeout setting","5 minutes to fix it"], "ctaText":"Follow for more →",     "textPops":[{"timeMs":8000,"text":"Voice AI kept hanging up","icon":"📵"},{"timeMs":20000,"text":"4 hours to find the bug","icon":"🔍"},{"timeMs":35000,"text":"Fixed in 5 minutes","icon":"✅"}], "storyScript":"Spent the morning debugging why a client's voice AI kept hanging up after the second question — turned out to be one bad timeout setting, took four hours to find, five minutes to fix. That's most of this job, honestly. Nobody sees the four hours, they just see the system answering calls at 2am like it's nothing. Small win, but I'll take it."},
    35: {"format":"carousel", "hookStat":"$2,400","hookLabel":"the real cost of one missed after-hours call",             "lowerThird":"Most firms have never actually done this math.",        "results":["Avg PI case value: $30k","Close rate on a contacted lead: ~8%","Expected value per missed call: ~$2,400"], "ctaText":"Run your numbers →",  "textPops":[{"timeMs":8000,"text":"$30k average case value","icon":"💰"},{"timeMs":20000,"text":"8% typical close rate","icon":"📊"},{"timeMs":35000,"text":"~$2,400 per missed call","icon":"🧮"}]},
    36: {"format":"reel",     "hookStat":"$150k", "hookLabel":"sitting in one Atlanta firm's missed-call log",            "lowerThird":"Atlanta firm, 9 attorneys. One bad weekend exposed it.", "results":["Saturday: 7 calls, 2 answered","Sunday: 5 calls, 1 answered","Estimated $150k in cases unanswered"], "ctaText":"DM me 'intake' →",   "textPops":[{"timeMs":7000,"text":"Busy weekend, office closed","icon":"🎆"},{"timeMs":20000,"text":"9 calls, 3 answered total","icon":"📵"},{"timeMs":35000,"text":"$150k sitting unanswered","icon":"💸"}], "script":"Atlanta PI firm, nine attorneys, asked me to look at their numbers after a busy weekend.\n\nSaturday: seven new inquiries. Two answered live. The other five hit voicemail.\n\nSunday: five more calls. One answered.\n\nThat's nine missed contacts in a single weekend, during exactly the kind of weekend that generates car accident cases — more traffic, more people out, more drinking.\n\nAt their average case value, that's somewhere around $150,000 sitting in a missed-call log from two days.\n\nThey didn't lose those cases to a competitor's better ads. They lost them to a closed office on a Saturday.\n\nWe installed AI intake the following week. Next busy weekend, every single one of those calls gets answered."},
    37: {"format":"story",    "hookStat":"2 days","hookLabel":"an attorney thought his AI was \"broken\"",                "lowerThird":"It wasn't broken. It just had nothing to forward.",    "results":["AI hadn't rung him in 2 days","He assumed something was wrong","Nothing was — it was just working"], "ctaText":"Follow for more →",     "textPops":[{"timeMs":8000,"text":"\"My AI is broken\"","icon":"😅"},{"timeMs":20000,"text":"Hadn't missed anything in 2 days","icon":"🤖"},{"timeMs":35000,"text":"That was the whole point","icon":"✅"}], "storyScript":"Funniest client call I've had this month — an attorney called me at 11pm panicking that his AI was broken because it hadn't rung him in two days. Nothing was broken. It just hadn't missed anything to forward to him. Took him a minute to realize that was the whole point. He laughed, I laughed, good call."},

    # ── WEEK 6 ──────────────────────────────────────────────────────────────
    38: {"format":"reel",     "hookStat":"5",     "hookLabel":"people called this firm at the exact same time",            "lowerThird":"Five accidents. Five calls. Same two minutes.",         "results":["Old system: 1 line, 4 voicemails","New system: 5 calls answered simultaneously","All 5 pre-qualified before lunch"], "ctaText":"DM me 'demo' →",      "textPops":[{"timeMs":7000,"text":"5 calls. Same 2 minutes.","icon":"📞"},{"timeMs":20000,"text":"Old system: 4 voicemails","icon":"❌"},{"timeMs":35000,"text":"New system: all 5 answered","icon":"✅"}], "script":"Tuesday afternoon, a multi-car pileup on the highway. Five different people from that one accident called the same PI firm within about two minutes of each other.\n\nWith their old setup — one phone line, one receptionist — that's one call answered and four voicemails.\n\nWith AI intake, every single one gets answered at the same time. No queue, no busy signal, no \"please hold.\"\n\nBy the time the attorney got back from lunch, all five were pre-qualified, case details captured, two already had consultations booked.\n\nThis is the scenario nobody plans for — not the slow Tuesday, the chaotic one. That's exactly when the old system breaks and this one doesn't."},
    39: {"format":"carousel", "hookStat":"3",     "hookLabel":"signs your weekends are costing you cases",                 "lowerThird":"Check your call log from last Saturday. Be honest.",    "results":["Office closed Sat/Sun — calls go to voicemail","Web form replies wait until Monday morning","No idea how many weekend leads you lost"], "ctaText":"Save this →",          "textPops":[{"timeMs":8000,"text":"Saturday: office closed","icon":"📵"},{"timeMs":20000,"text":"Monday: leads already gone","icon":"🚶"},{"timeMs":35000,"text":"You don't even know the number","icon":"❓"}]},
    40: {"format":"reel",     "hookStat":"31%",   "hookLabel":"of booked consultations never showed up. Until this changed.", "lowerThird":"Booking the consult isn't the finish line.",         "results":["Before: 31% no-show rate on consults","AI sends confirmation + reminder messages","After: no-show rate dropped to 9%"], "ctaText":"DM me 'intake' →",   "textPops":[{"timeMs":8000,"text":"31% just didn't show up","icon":"📵"},{"timeMs":20000,"text":"Confirmation + reminders sent","icon":"💬"},{"timeMs":35000,"text":"No-show rate: 9%","icon":"✅"}], "script":"Booking the consultation feels like the win. For one firm, it wasn't — 31% of booked consultations were just no-shows. People got busy, forgot, or had already talked to someone else by the time the appointment came around.\n\nThe AI intake system doesn't stop at booking. It sends a confirmation right after, then a reminder the morning of, then one an hour before — same way a dentist's office reminds you, just automatic.\n\nNo-show rate dropped from 31% to 9% within the first month. Same booked volume, way more of those consultations actually happening.\n\nBooking a consult and someone actually walking through it are two completely different numbers. Most firms only track the first one."},
    41: {"format":"story",    "hookStat":"30s",   "hookLabel":"test you can run on your own site right now",               "lowerThird":"Most attorneys have never actually done this.",        "results":["Submit a test lead on your own form","Time how long until anything happens","If it's hours — that's the whole problem"], "ctaText":"Try it now →",         "textPops":[{"timeMs":8000,"text":"Submit a test lead","icon":"📝"},{"timeMs":20000,"text":"Time the response","icon":"⏱️"},{"timeMs":35000,"text":"That gap is the problem","icon":"🔍"}], "storyScript":"Quick thing you can check on your own site in 30 seconds — open your contact form on your phone right now and actually submit a test lead. Time how long it takes for anything to happen. Most attorneys have never done this. If the answer is \"nothing happens for hours,\" that's the whole problem, right there, no system needed to tell you that."},
    42: {"format":"carousel", "hookStat":"Before / After","hookLabel":"what actually changes when you install AI intake","lowerThird":"Side by side. No fluff, just the difference.",          "results":["Before: 3-5hr response → After: 60 seconds","Before: voicemail after 6pm → After: answered 24/7","Before: guessing lead quality → After: pre-qualified"], "ctaText":"Save this →",  "textPops":[{"timeMs":8000,"text":"Before: 3-5hr response","icon":"🐢"},{"timeMs":20000,"text":"After: 60 seconds","icon":"⚡"},{"timeMs":35000,"text":"Pre-qualified, every time","icon":"✅"}]},
    43: {"format":"reel",     "hookStat":"73%",   "hookLabel":"of accident leads under 40 would rather text than call",   "lowerThird":"Your intake is built for phone calls. Your leads aren't.", "results":["Most PI intake: phone-only","Most leads under 40: prefer text first","AI intake answers both, same 60 seconds"], "ctaText":"DM me 'intake' →",  "textPops":[{"timeMs":7000,"text":"They don't want to call","icon":"📵"},{"timeMs":20000,"text":"They want to text","icon":"💬"},{"timeMs":35000,"text":"AI answers either way","icon":"✅"}], "script":"Most PI firms built their entire intake around one assumption — that someone in an accident is going to pick up the phone and call.\n\nA lot of younger leads just won't. They'll text. They'll fill out a form at 1am and expect a reply, not a ring.\n\nIf your only path in is a phone line, you're invisible to a huge chunk of leads who are sitting there with a case, just not calling you about it.\n\nAI intake handles both the same way — call comes in, answered in seconds. Text comes in, answered in seconds. Web form, same thing.\n\nIt's not about replacing the phone. It's about not assuming everyone wants to use it the same way you do."},
    44: {"format":"story",    "hookStat":"Night 1","hookLabel":"a client's first real call broke the system",          "lowerThird":"Honest one today — how this actually gets better.",     "results":["Didn't test what happens on hang-up","Found out live, on a real call","Fixed that night, never happened again"], "ctaText":"Follow for more →",   "textPops":[{"timeMs":8000,"text":"Found a bug, live","icon":"😬"},{"timeMs":20000,"text":"Fixed it that night","icon":"🔧"},{"timeMs":35000,"text":"That's how it gets better","icon":"✅"}], "storyScript":"Honest one today — I built the first version of this for a client without testing what happens when someone hangs up mid-sentence. Found out live, on their first real call. Fixed it that night, never happened again, but that's basically how every version of this gets better — something breaks on a real call, then it doesn't break again. Not glamorous, just how it actually goes."},

    # ── WEEK 7 ──────────────────────────────────────────────────────────────
    45: {"format":"reel",     "hookStat":"$38k",  "hookLabel":"case that almost went to a competitor over a weekend",     "lowerThird":"Slip-and-fall. Saturday afternoon. Nobody picked up.",  "results":["Called Saturday 2pm, no answer","AI answered in 4 seconds instead","Consult booked Monday 9am, case retained"], "ctaText":"Book a call →",        "textPops":[{"timeMs":7000,"text":"Saturday slip-and-fall","icon":"🦴"},{"timeMs":20000,"text":"AI answered in 4 seconds","icon":"📞"},{"timeMs":35000,"text":"$38k case retained","icon":"💰"}], "script":"Saturday afternoon, someone slips at a grocery store, hurts their back badly enough to need an ER visit. They call a PI firm from the parking lot.\n\nWith their old setup, that call goes unanswered until Monday. Two days is plenty of time to call three more firms.\n\nThis firm had AI intake running. Answered in four seconds, asked about the injury, the location, witnesses, got the basics down before the person even left the parking lot.\n\nMonday morning, consultation already booked for 9am. Case retained. Settled later for $38,000.\n\nThe difference between getting that case and losing it wasn't the attorney's skill or the firm's reputation. It was four seconds versus two days."},
    46: {"format":"carousel", "hookStat":"$50k",  "hookLabel":"in lost cases looks smaller than you'd think",               "lowerThird":"It's never one big case. It's five small gaps.",        "results":["3 after-hours calls a week, unanswered","2 web forms a week, answered next morning","Add it up over a month: ~$50k walking out"], "ctaText":"Save this →",  "textPops":[{"timeMs":8000,"text":"Never one big loss","icon":"🔍"},{"timeMs":20000,"text":"It's small gaps, weekly","icon":"📉"},{"timeMs":35000,"text":"Adds up to $50k/month","icon":"💸"}]},
    47: {"format":"reel",     "hookStat":"$612",  "hookLabel":"is what one missed call actually costs, on average",       "lowerThird":"Most firms know cost-per-lead. Almost none know this.", "results":["Cost to generate one lead: ~$150-300","Value lost if not contacted: ~$2,400+","Net loss per missed call: hundreds, not zero"], "ctaText":"Run your audit →",  "textPops":[{"timeMs":7000,"text":"You know cost-per-lead","icon":"📊"},{"timeMs":20000,"text":"Do you know cost-per-miss?","icon":"❓"},{"timeMs":35000,"text":"It's hundreds, not zero","icon":"🧮"}], "script":"Every PI firm running ads knows their cost-per-lead. Most have it memorized — $150, $250, whatever it is for them.\n\nAlmost none of them have calculated the cost of a missed call, and it's not zero. You already paid to generate that lead. If nobody answers, that ad spend didn't just underperform — it was wasted entirely.\n\nRun the math: you spent real money to get the phone to ring, then a missed call doesn't just cost you the case, it costs you the acquisition spend on top of it.\n\nMost firms treat missed calls as a soft, vague problem. It's not vague. It's a specific number, and it's bigger than people expect once they actually calculate it."},
    48: {"format":"story",    "hookStat":"4am",   "hookLabel":"and 11pm. Two calls. Both handled. One Tuesday.",           "lowerThird":"Still doesn't get old, watching this work.",            "results":["4am call: handled, pre-qualified","11pm call: handled, pre-qualified","Attorney saw both before he woke up"], "ctaText":"Follow for more →",     "textPops":[{"timeMs":8000,"text":"4am call, handled","icon":"🌙"},{"timeMs":20000,"text":"11pm call, handled","icon":"📞"},{"timeMs":35000,"text":"Both before he woke up","icon":"✅"}], "storyScript":"Pulled up a client's dashboard this morning just to check on something and got distracted looking at the call log instead — 4am, 11pm, 2 different calls on a Tuesday, both handled, both pre-qualified before the attorney even woke up. I look at this stuff every day and it still doesn't get old seeing it actually work while someone's asleep."},
    49: {"format":"carousel", "hookStat":"$0",    "hookLabel":"extra staff needed to handle 2-3x more intake volume",     "lowerThird":"Most firms scale intake by hiring. There's a cheaper way.", "results":["Hiring an intake coordinator: $40k-$55k/yr","AI intake: a fraction of that, runs 24/7","Handles 5 simultaneous calls either way"], "ctaText":"DM me 'intake' →", "textPops":[{"timeMs":8000,"text":"Hiring costs $40k-$55k/yr","icon":"💵"},{"timeMs":20000,"text":"AI costs a fraction","icon":"💡"},{"timeMs":35000,"text":"Handles more volume either way","icon":"📈"}]},
    50: {"format":"reel",     "hookStat":"24hrs", "hookLabel":"is how fast the insurance company already moved on your lead", "lowerThird":"The other side isn't waiting until Monday.",          "results":["Insurance adjusters call within hours","Opposing counsel responds same day","Your intake: still on Monday's to-do list"], "ctaText":"DM me 'intake' →",  "textPops":[{"timeMs":7000,"text":"Insurance moves in hours","icon":"⏱️"},{"timeMs":20000,"text":"They don't wait for Monday","icon":"📅"},{"timeMs":35000,"text":"Your intake shouldn't either","icon":"⚡"}], "script":"Here's something that should bother you more than it does. The insurance company on the other side of your case doesn't wait until Monday to respond. Their adjusters are calling claimants within hours of an accident being reported.\n\nThey're fast because speed works in their favor — get there first, frame the conversation, sometimes get a statement before the person even has a lawyer.\n\nMeanwhile a lot of PI firms still treat their own intake like a 9-to-5 job. Someone calls Friday night, the file doesn't get touched until Monday morning.\n\nYou're in a race you didn't realize you signed up for, against people who already treat speed as the whole strategy. AI intake puts your firm on the same clock as theirs — answered in seconds, not days."},
    51: {"format":"story",    "hookStat":"2 years","hookLabel":"is how long one attorney said he'd been meaning to fix this", "lowerThird":"Not judging. Most firms are in this exact spot.",      "results":["DM: \"been meaning to fix this for 2 years\"","The math doesn't change while you wait","It takes about 4 weeks once you start"], "ctaText":"Follow for more →",  "textPops":[{"timeMs":8000,"text":"\"Two years,\" he said","icon":"😅"},{"timeMs":20000,"text":"Most firms are right there","icon":"🤝"},{"timeMs":35000,"text":"Starting is the hard part","icon":"🚀"}], "storyScript":"Got a DM yesterday from an attorney who said he'd been meaning to fix his after-hours setup for two years — two years. Not judging, genuinely, most firms are in that exact spot, it's just never the most urgent thing until a case slips through. Told him the same thing I'll say here — it takes about 4 weeks once you actually start, the hard part is just starting."},

    # ── WEEK 8 ──────────────────────────────────────────────────────────────
    52: {"format":"reel",     "hookStat":"3×",    "hookLabel":"more accident calls come in over a long weekend",           "lowerThird":"More traffic. More accidents. Same closed office.",     "results":["Long weekends: call volume jumps 2-3x","Most firm offices: still closed Sat/Sun","AI intake: doesn't know what a holiday is"], "ctaText":"DM me 'intake' →",  "textPops":[{"timeMs":7000,"text":"Long weekend traffic spikes","icon":"🚗"},{"timeMs":20000,"text":"2-3x more accident calls","icon":"📈"},{"timeMs":35000,"text":"Office still closed though","icon":"🔒"}], "script":"Long holiday weekends are statistically some of the worst for accidents — more cars on the road, more travel, more fatigue. Call volume for PI firms spikes accordingly, sometimes two to three times a normal weekend.\n\nAnd it's exactly when most firm offices are closed for the longest stretch of the month.\n\nThat combination — highest demand, lowest coverage — is the single worst intake gap in this entire business, and it happens like clockwork every few months.\n\nAI intake doesn't know it's a holiday. Same 60-second response at 2am on a Sunday of a long weekend as it gives on a random Tuesday afternoon.\n\nIf you only fix one gap in your intake this year, this is the one with the highest concentration of cases sitting behind it."},
    53: {"format":"carousel", "hookStat":"4",     "hookLabel":"things we check before taking on a new firm",               "lowerThird":"We turn down more firms than we take. Here's the filter.", "results":["Named partners, real case results","Already running ads (so leads exist)","Currently losing leads to slow response"], "ctaText":"DM 'intake' to check →", "textPops":[{"timeMs":8000,"text":"We don't take every firm","icon":"🚫"},{"timeMs":20000,"text":"This is the actual filter","icon":"🔍"},{"timeMs":35000,"text":"DM to see if you qualify","icon":"✅"}]},
    54: {"format":"reel",     "hookStat":"Referred","hookLabel":"by a past client. Still went to voicemail.",            "lowerThird":"Your best lead source has the same gap as your ads.",   "results":["Past client referred a friend at 8pm","Call went to voicemail, same as any ad lead","Friend signed with a different firm instead"], "ctaText":"DM me 'intake' →", "textPops":[{"timeMs":7000,"text":"Referred by a happy client","icon":"🤝"},{"timeMs":20000,"text":"Still hit voicemail at 8pm","icon":"❌"},{"timeMs":35000,"text":"Went to a different firm","icon":"🚶"}], "script":"Referrals get treated like the safe leads — the ones you don't have to worry about, because someone already vouched for you.\n\nThey still call after hours. They still get voicemail like anyone else.\n\nOne firm found out a past client referred a friend after an accident at 8pm on a Friday. The friend called, got voicemail, and ended up signing with a different firm by Monday.\n\nThe attorney never even knew the referral happened. Found out two weeks later in a totally unrelated conversation.\n\nYour ad leads get all the attention because you're paying for them directly. Your referral leads are walking through the exact same broken door, you just don't see it happen."},
    55: {"format":"story",    "hookStat":"3hrs",  "hookLabel":"to fix a bug nobody else will ever notice",                  "lowerThird":"Friday thought — most weeks aren't glamorous.",        "results":["Most weeks: small timing bugs","Looks exactly the same to everyone else","Just keeping it from breaking at 2am"], "ctaText":"Follow for more →",     "textPops":[{"timeMs":8000,"text":"3 hours on one bug","icon":"🛠️"},{"timeMs":20000,"text":"Looks the same to everyone","icon":"👀"},{"timeMs":35000,"text":"Just keeping it from breaking","icon":"🔒"}], "storyScript":"Friday thought — this week was mostly fixing small timing bugs nobody will ever notice except me, the kind of thing where you spend three hours and the result looks exactly the same to everyone else. That's most weeks here, honestly. The flashy stuff is rare. It's mostly just making sure the thing doesn't break at 2am when nobody's watching."},
    56: {"format":"carousel", "hookStat":"4 weeks","hookLabel":"from first call to fully live. Here's the actual timeline.", "lowerThird":"No mystery box. Here's the real build timeline.",       "results":["Week 1: voice AI + CRM setup","Week 2: qualification logic built","Week 3-4: testing, sequences, go live"], "ctaText":"DM me 'intake' →",   "textPops":[{"timeMs":8000,"text":"Week 1: voice AI + CRM","icon":"🛠️"},{"timeMs":20000,"text":"Week 2: qualification logic","icon":"🧠"},{"timeMs":35000,"text":"Week 4: live, running 24/7","icon":"🚀"}]},
    57: {"format":"reel",     "hookStat":"6hrs",  "hookLabel":"a week back. Here's what attorneys actually do with it",   "lowerThird":"Nobody asks this question, but it matters.",           "results":["No more screening obvious non-cases","No more chasing voicemail callbacks","More time on cases already worth taking"], "ctaText":"DM me 'intake' →",  "textPops":[{"timeMs":7000,"text":"Less time screening junk leads","icon":"🗑️"},{"timeMs":20000,"text":"Less time chasing callbacks","icon":"📵"},{"timeMs":35000,"text":"More time on real cases","icon":"⚖️"}], "script":"Everybody talks about AI intake in terms of cases recovered. Nobody really talks about what attorneys do with the time it gives back.\n\nOne managing partner told me it wasn't really the missed cases that bothered him most — it was how much of his week went to screening calls that were never going to be real cases, or chasing people who'd already moved on by the time he called back.\n\nOnce intake is automated, that work just doesn't land on his desk anymore. Pre-qualified leads show up with the details already captured, the questions already asked.\n\nHe estimated it gave him back close to six hours a week. Not in cases — in actual time, the kind you can spend on the cases already worth taking.\n\nThe ROI conversation is usually about dollars. Sometimes it's just about getting a Tuesday afternoon back."},
    58: {"format":"story",    "hookStat":"4",     "hookLabel":"consultations booked overnight. One screenshot, no caption.",  "lowerThird":"Still get a little hit of satisfaction every time.",   "results":["Client texted a dashboard screenshot","4 consultations booked overnight","No caption needed — the numbers said it"], "ctaText":"DM me to see it →",   "textPops":[{"timeMs":8000,"text":"Just a screenshot, no caption","icon":"📱"},{"timeMs":20000,"text":"4 consults, overnight","icon":"📅"},{"timeMs":35000,"text":"Still doesn't get old","icon":"😊"}], "storyScript":"Client texted me a screenshot last night, no caption, just their dashboard showing four consultations booked overnight while they were asleep. Didn't even need to say anything, the screenshot said it. Still get a little hit of satisfaction every time that happens, not gonna pretend I don't. If you want to see what that dashboard actually looks like, DM me and I'll just show you."},
    59: {"format":"carousel", "hookStat":"3",     "hookLabel":"things firms say right before they say yes",                "lowerThird":"If you've thought any of these, you're not alone.",    "results":["\"We already have a receptionist\"","\"We tried a chatbot, it didn't work\"","\"This sounds expensive\" — usually isn't"], "ctaText":"DM me 'intake' →", "textPops":[{"timeMs":8000,"text":"\"We have a receptionist\"","icon":"🙋"},{"timeMs":20000,"text":"\"We tried a chatbot\"","icon":"🤖"},{"timeMs":35000,"text":"\"Sounds expensive\" — let's check","icon":"💵"}]},
    60: {"format":"reel",     "hookStat":"60",    "hookLabel":"days of this. Here's where I'm at.",                       "lowerThird":"Two months of posting this. The urgency has changed.", "results":["We only take 1-2 firms a month","Most firms still on voicemail after 6pm","4 weeks to build, running 24/7 from day one"], "ctaText":"DM me 'intake' →",   "textPops":[{"timeMs":8000,"text":"60 days of this content","icon":"📅"},{"timeMs":20000,"text":"We take 1-2 firms a month","icon":"🔒"},{"timeMs":35000,"text":"4 weeks to build and live","icon":"🚀"}], "script":"60 days of posting about the same problem, from a dozen different angles. Voicemail, missed calls, lost referrals, weekend spikes, the math behind all of it.\n\nHere's the part I haven't said directly enough: we only take on one or two firms a month. Not because of hype — because the setup, the qualification logic, the testing, all of it is custom per firm, and that takes real time to do right.\n\nIf you've been reading these for two months and telling yourself you'll get to it eventually — that's fine, genuinely, but the math doesn't change while you wait. Every week on voicemail is still a week of cases going to whoever answered first.\n\nFour weeks to build. Runs 24/7 from the day it goes live.\n\nIf this is the month it's actually a priority, DM me \"intake\" and I'll tell you straight whether it makes sense for your firm before you spend anything."},
}

_PI_TITLES = {
    -2:"⭐ About Us (PIN THIS)",
    -1:"⭐ What We Do (PIN THIS)",
    0:"⭐ Authority — Who We Are (PIN THIS)",
    1:"The 60-Second Rule",           2:"5 Signs You're Losing Cases",    3:"Stop Posting 'Call Anytime'",
    4:"11 Missed Calls",              5:"$50k Lost Monthly",              6:"8 Extra Consultations",
    7:"Kid's Game Story",             8:"Half Their Leads Were Ghosts",   9:"Perfect Intake Flow",
    10:"Ads Work, Intake Fails",      11:"90 Days of Data",               12:"What AI Actually Does",
    13:"30 Days Tracked",             14:"Monday Morning CRM",            15:"Honest Take on AI",
    16:"4 Channels Fixed",            17:"$45k Voicemail",                18:"Two Firms Same Budget",
    19:"TX+FL Moving First",          20:"10 Attorneys Surveyed",         21:"9pm Form Submit Test",
    22:"$20k Ads + Voicemail",        23:"AI Revenue Engine",             24:"Receptionist Won't Fix It",
    25:"340 Dead Leads",              26:"7 Questions Framework",         27:"3:47am Call",
    28:"9 to 26 Consultations",       29:"Qualify in 60 Seconds",         30:"Day 30 — Final Post",
    31:"The ROI Calculator",          32:"5 Questions Before You Spend",  33:"Mystery-Shopped 20 Firms",
    34:"4 Hours, One Bug",            35:"Cost of One Missed Call",       36:"Atlanta Weekend Surge",
    37:"\"My AI Is Broken\"",         38:"5 Calls At Once",               39:"3 Weekend Warning Signs",
    40:"31% No-Show Rate",            41:"Test Your Own Site",            42:"Before / After",
    43:"Text vs. Call Preference",    44:"The Bug That Got Through",      45:"$38k Slip-and-Fall",
    46:"What $50k Looks Like",        47:"Cost-Per-Missed-Call",          48:"4am and 11pm",
    49:"$0 Extra Staff",              50:"Racing the Insurance Co.",      51:"Two Years of Meaning To",
    52:"Long Weekend Spike",          53:"Our Client Filter",             54:"Referrals Get Missed Too",
    55:"Friday Wind-Down",            56:"The 4-Week Build",              57:"6 Hours Back a Week",
    58:"4 Consults, No Caption",      59:"3 Things Before Yes",           60:"Day 60 — Where I'm At",
}
_PI_DAYS = ["MON","TUE","WED","THU","FRI","SAT","SUN"]

def _pick_solution_graphic(props):
    """Pick which 'solution' beat graphic fits this day's script: ai_24_7 if the
    copy explicitly talks about round-the-clock coverage, intake_flow if it walks
    through the steps explicitly — either literally ("step by step") or by
    narrating the route in order (a text/SMS step, then a consultation getting
    booked) — else the lead-qualification dashboard. Checked against the full
    script (not just the short props fields), since that's the only place this
    phrasing actually appears."""
    haystack = ' '.join([
        props.get('hookLabel', '') or '',
        props.get('lowerThird', '') or '',
        ' '.join(props.get('results', []) or []),
        props.get('script', '') or '',
    ]).lower()
    # intake_flow's signals are checked first because they're specific narrative
    # patterns (the route walked in order); '24/7' is just one phrase and can show
    # up incidentally (e.g. a script quoting a *competitor's* "24/7 line"), so it's
    # the weakest signal and goes last to avoid hijacking an intake_flow script.
    if 'step by step' in haystack or 'step-by-step' in haystack:
        return 'intake_flow'
    if 'text' in haystack and 'consultation' in haystack and 'book' in haystack:
        return 'intake_flow'
    if '24/7' in haystack or '24-7' in haystack:
        return 'ai_24_7'
    return 'ai_intake_live'

def _find_phrase_timestamp(captions, keywords, after_ms=0):
    """Search the real Whisper word-level transcript for the first point where all
    keywords appear within a short rolling window of consecutive spoken words.
    textPops are pre-authored at fixed timestamps and only linearly rescaled to the
    actual clip duration — that assumes constant speaking pace, which real speech
    never has, so the rescaled timestamp can land well before or after the moment
    those words are actually said. Anchoring on the literal transcript is exact.
    after_ms restricts the search to words spoken from that point on — several
    scripts mention "AI" once in the problem beat (a rival firm's AI) before the
    real reveal, so an unscoped search can lock onto the wrong mention."""
    if not captions:
        return None
    words = [(c['text'].lower().strip('.,!?’\'"():;').strip(), c['startMs']) for c in captions]
    keywords = [k.lower() for k in keywords]
    span = 6
    for i in range(len(words)):
        if words[i][1] < after_ms:
            continue
        window = words[i:i + span]
        match_times = []
        for kw in keywords:
            # Exact word match, not substring — "voice" must not match inside
            # "voicemail" (a real collision: Day 17 recaps "the voicemail wasn't
            # the problem... we installed AI intake" in one breath, which would
            # otherwise satisfy a ['voice','ai'] search on the wrong sentence).
            matches = [t for w, t in window if kw == w]
            if not matches:
                break
            match_times.append(min(matches))
        else:
            # Anchor on the matched word itself, not the start of the lookahead
            # window — a keyword found 5 words into the window otherwise anchors
            # on whatever unrelated word started the window, landing the graphic
            # seconds before the phrase is actually spoken.
            return min(match_times)
    return None

def _compute_broll_cuts(duration, text_pops, props, captions=None):
    """Place B2B motion-graphic cutaways at the actual narrative beats — preferring
    the real moment those beats are spoken (from the Whisper transcript) over the
    linearly-rescaled textPops timestamps, which only approximate it. missed_call
    anchors on 'voicemail'/'missed call' (the problem beat); the solution graphic
    (ai_24_7 or ai_intake_live, picked by _pick_solution_graphic) anchors on
    whichever real reveal phrasing the day's script actually uses — 'build' +
    'system' (Day 1), 'AI' + 'intake' (most others), or 'voice' + 'AI' — searched
    only after the problem beat so an earlier "their AI" mention can't be mistaken
    for our own reveal. intake_flow (the call->SMS->pre-qualified->booked route)
    anchors on 'step by' and only appears on scripts that literally walk through
    the steps, searched after the solution beat."""
    pops = sorted(text_pops or [], key=lambda p: p['timeMs'])
    CLIP_LEN = 5.0  # match the rendered HyperFrames clips' real length exactly —
                    # a shorter window made the fade-out start mid-animation,
                    # cutting the clip's own ending (e.g. a badge settling) short

    def window(anchor_ms):
        # Every other overlay (captions, text pops, results, CTA) self-hides via
        # isInGraphic while a cutaway is active, so the next narrative beat's
        # timestamp is never a real constraint — only the video's own length is.
        # Capping on it previously truncated the window below CLIP_LEN, fading
        # the graphic out mid-animation (e.g. before the last checkmark/stat lands).
        start = max(0.0, anchor_ms / 1000 - 0.3)
        end = min(start + CLIP_LEN, duration)
        if end - start < 2.5:
            return None
        return round(start, 2), round(end, 2)

    cuts = []

    # Not every script frames the problem beat as a literal "voicemail" —
    # some describe it as a slow ad-response gap, a lost lead, or a no-show
    # callback instead. Try the real phrasings actually used before giving up.
    # No blind fallback to a rescaled-pop guess anymore: a missed_call graphic
    # showing up over unrelated narration (e.g. "we spent $15k on ads...") was
    # worse than not showing one at all, so an unanchored beat is just skipped.
    problem_ms = (
        _find_phrase_timestamp(captions, ['voicemail'])
        or _find_phrase_timestamp(captions, ['missed', 'call'])
        or _find_phrase_timestamp(captions, ['googled'])
        or _find_phrase_timestamp(captions, ['hired', 'another'])
        or _find_phrase_timestamp(captions, ['fills', 'form'])
    )
    if problem_ms is not None:
        w = window(problem_ms)
        if w:
            cuts.append({"start": w[0], "end": w[1], "keyword": "missed_call", "videoFile": "", "graphic": "missed_call"})

    # Each solution graphic depicts a different concept, so it must anchor on
    # the phrase that actually matches what it shows — ai_intake_live is the
    # qualification dashboard (fires only on "AI intake" literally being said),
    # intake_flow is the call->SMS->pre-qualified->booked route (fires only on
    # "step by step", since that's the one script pattern that actually walks
    # through it), ai_24_7 is the round-the-clock graphic (fires on "build
    # system"/"voice AI"). They're mutually exclusive — only one plays per
    # video — and there's no cross-fallback and no blind pop-timing guess:
    # skip rather than mis-place it.
    after = (problem_ms or 0) + 500
    solution_graphic = _pick_solution_graphic(props)
    if solution_graphic == 'ai_intake_live':
        solution_ms = _find_phrase_timestamp(captions, ['ai', 'intake'], after_ms=after)
    elif solution_graphic == 'intake_flow':
        # Not every intake_flow script literally says "step by step" — some
        # narrate the route in-line instead ("voice AI picked up... consultation
        # was booked... sent a text confirmation"). Try the literal phrase first,
        # then the start of that narration ("voice AI"), then the SMS-step
        # mention itself ("text") as the last resort anchor.
        solution_ms = (
            _find_phrase_timestamp(captions, ['step', 'by'], after_ms=after)
            or _find_phrase_timestamp(captions, ['voice', 'ai'], after_ms=after)
            or _find_phrase_timestamp(captions, ['text'], after_ms=after)
        )
    else:
        solution_ms = (
            _find_phrase_timestamp(captions, ['build', 'system'], after_ms=after)
            or _find_phrase_timestamp(captions, ['voice', 'ai'], after_ms=after)
        )
    if solution_ms is not None:
        w = window(solution_ms)
        if w:
            cuts.append({"start": w[0], "end": w[1], "keyword": solution_graphic, "videoFile": "", "graphic": solution_graphic})

    # Cuts no longer share a ceiling, so if two narrative beats are spoken close
    # together their full-length windows could overlap and stack two graphics on
    # screen at once. Trim the earlier cut's end back to the later cut's start
    # (never the reverse — the later anchor is the literal spoken moment).
    cuts.sort(key=lambda c: c['start'])
    for i in range(len(cuts) - 1):
        if cuts[i]['end'] > cuts[i + 1]['start']:
            cuts[i]['end'] = round(cuts[i + 1]['start'], 2)
    cuts = [c for c in cuts if c['end'] - c['start'] >= 2.5]

    return cuts

def _advance_reel_queue():
    """Start the next queued reel render, if none is currently rendering."""
    with _reel_render_lock:
        is_active = any(j.get('status') == 'rendering' for j in _reel_render_jobs.values())
        if is_active or not _reel_render_queue:
            return
        jid = _reel_render_queue.pop(0)
        job = _reel_render_jobs[jid]
        job['status'] = 'rendering'
        job['log'] = 'Starting render...'
        job['started_at'] = datetime.now().strftime('%H:%M:%S')
    prefix = 'pi_story' if job.get('is_story') else 'pi_day'
    output_name = f"{prefix}{job['day']}_{jid}.mp4"
    threading.Thread(target=_do_render, args=(jid, output_name), daemon=True).start()

def _do_render(job_id, output_name):
    try:
        # ── Step 1: props were built and stashed on the job when it was queued ──
        props = _reel_render_jobs[job_id]['props']
        is_story = _reel_render_jobs[job_id].get('is_story', False)

        video_abs = os.path.join(_REMOTION_DIR, 'public', props.get('videoSrc', ''))

        # ── Step 2: ffprobe — get exact video duration ───────────────────────
        _reel_render_jobs[job_id]['log'] = '⏱ Detecting video duration...'
        try:
            ffout = subprocess.check_output([
                'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', video_abs
            ], text=True)
            streams = json.loads(ffout).get('streams', [])
            duration = max((float(s.get('duration', 0)) for s in streams), default=0.0)
        except Exception:
            duration = 0.0

        if duration > 1:
            orig_secs = props.get('totalSeconds', 60) or 60
            scale = duration / orig_secs
            # Rescale textPop timestamps proportionally to actual video length
            if abs(scale - 1.0) > 0.03:
                props['textPops'] = [
                    {**p, 'timeMs': int(p['timeMs'] * scale)}
                    for p in props.get('textPops', [])
                ]
            props['totalSeconds'] = round(duration, 3)
            _reel_render_jobs[job_id]['log'] = f'⏱ Duration: {duration:.1f}s  (scaled overlays ×{scale:.2f})'

        # ── Step 3: Whisper — word-level captions ────────────────────────────
        _reel_render_jobs[job_id]['log'] += '\n🎙 Transcribing audio with Whisper...'
        try:
            import whisper as _whisper
            model = _whisper.load_model('base')
            result = model.transcribe(video_abs, word_timestamps=True, language='en',
                                      fp16=False, verbose=False)
            captions = []
            for seg in result.get('segments', []):
                for w in seg.get('words', []):
                    word = w.get('word', '').strip()
                    if word:
                        captions.append({
                            'text': word,
                            'startMs': int(w['start'] * 1000),
                            'endMs':   int(w['end']   * 1000),
                            'confidence': round(float(w.get('probability', 1.0)), 3),
                        })
            props['captions'] = captions
            props['showCaptions'] = True
            _reel_render_jobs[job_id]['log'] += f'\n✅ {len(captions)} words transcribed'
        except Exception as exc:
            _reel_render_jobs[job_id]['log'] += f'\n⚠️ Whisper skipped: {exc}'
            props['captions'] = []
            props['showCaptions'] = False

        # Stories are just the clip + captions — no dashboard motion graphics, so
        # skip the broll-cut anchoring pass entirely (it doesn't apply).
        if not is_story:
            # Now that we have the real transcript, anchor motion graphics on the actual
            # moment those beats are spoken rather than the linearly-rescaled textPops guess.
            props['brollCuts'] = _compute_broll_cuts(props.get('totalSeconds', 0) or 0, props.get('textPops', []), props, props.get('captions', []))
            if props['brollCuts']:
                _reel_render_jobs[job_id]['log'] += f"\n🎬 Motion graphics: {', '.join(c['graphic'] for c in props['brollCuts'])}"

        # ── Step 4: write props for Remotion — safe now, this job owns the shared
        #            props file exclusively until it finishes (queue-serialized) ──
        props_path = _REMOTION_STORY_PROPS if is_story else _REMOTION_PROPS
        with open(props_path, 'w') as f:
            json.dump(props, f, indent=2)

        # Cancel may have been requested during ffprobe/Whisper, before any subprocess existed to terminate.
        if _reel_render_jobs[job_id].get('cancel_requested'):
            _reel_render_jobs[job_id]['status'] = 'cancelled'
            return

        _reel_render_jobs[job_id]['log'] += '\n🎬 Starting Remotion render...'

        # ── Step 5: render ───────────────────────────────────────────────────
        composition_id = 'Story' if is_story else 'Reel'
        proc = subprocess.Popen(
            [_NPX, 'remotion', 'render', composition_id, f'public/edited/{output_name}', '--codec=h264'],
            cwd=_REMOTION_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        _reel_render_jobs[job_id]['proc'] = proc
        lines = [_reel_render_jobs[job_id]['log']]
        for line in proc.stdout:
            lines.append(line.rstrip())
            _reel_render_jobs[job_id]['log'] = '\n'.join(lines[-20:])
        proc.wait()
        if _reel_render_jobs[job_id].get('cancel_requested'):
            _reel_render_jobs[job_id]['status'] = 'cancelled'
        elif proc.returncode == 0:
            _reel_render_jobs[job_id].update({'status': 'done', 'output_name': output_name})
        else:
            _reel_render_jobs[job_id]['status'] = 'error'
    except Exception as e:
        import traceback
        if not _reel_render_jobs[job_id].get('cancel_requested'):
            _reel_render_jobs[job_id].update({'status': 'error', 'log': traceback.format_exc()})
    finally:
        _reel_render_jobs[job_id].pop('proc', None)
        _advance_reel_queue()

def _render_day_editor(allowed_format, heading, subheading):
    edited = []
    if os.path.exists(_REMOTION_EDITED):
        for f in sorted(os.listdir(_REMOTION_EDITED), reverse=True):
            if f.endswith('.mp4'):
                size_mb = round(os.path.getsize(os.path.join(_REMOTION_EDITED, f)) / 1024 / 1024, 1)
                edited.append({'name': f, 'size': size_mb})
    days_for_grid = [d for d in range(1, max(_PI_PROPS) + 1) if _PI_PROPS[d]['format'] == allowed_format]
    return render_template('instagram_editor.html', title=heading,
                           heading=heading, subheading=subheading,
                           allowed_format=allowed_format, days_for_grid=days_for_grid,
                           default_day=days_for_grid[0],
                           edited=edited, props=_PI_PROPS, titles=_PI_TITLES,
                           days=_PI_DAYS, jobs=_reel_render_jobs)

@app.route('/instagram/editor')
def instagram_editor():
    return _render_day_editor('reel', 'Video Editor',
                               'Upload HeyGen MP4 → Whisper transcribes → motion graphics render automatically')

@app.route('/instagram/stories')
def instagram_stories():
    return _render_day_editor('story', 'Story Editor',
                               'Upload a quick talk-to-camera clip → Whisper transcribes → captions render automatically')

@app.route('/instagram/editor/render', methods=['POST'])
def instagram_editor_render():
    day = int(request.form.get('day', 1))
    video = request.files.get('video')
    if not video or not video.filename:
        return jsonify({'ok': False, 'error': 'No video file uploaded'})

    is_story = _PI_PROPS[day]['format'] == 'story'

    jid = str(uuid.uuid4())[:8]
    os.makedirs(_REMOTION_VIDEOS, exist_ok=True)
    video_filename = f'{jid}_upload.mp4'
    video.save(os.path.join(_REMOTION_VIDEOS, video_filename))

    if is_story:
        # Stories are just the clip + captions — none of the Reel dashboard
        # fields (hookStat, lowerThird, results, ctaText, textPops) apply.
        props = {
            'videoSrc': f'videos/{video_filename}',
            'videoFit': request.form.get('videoFit', 'cover'),
            'accentColor': '#818cf8',
            'captions': [],
            'totalSeconds': 20,      # ffprobe overwrites this in _do_render
            'showCaptions': False,   # Whisper overwrites this in _do_render
        }
    else:
        props = {
            **_PI_PROPS[day],
            'videoSrc': f'videos/{video_filename}',
            'framesDir': '', 'totalFrames': 0,
            'captions': [], 'brollCuts': [],
            'videoFit': request.form.get('videoFit', 'cover'),
            'account': 'US',
            'totalSeconds': 60,      # ffprobe overwrites this in _do_render
            'showCaptions': False,   # Whisper overwrites this in _do_render
        }
        for field in ['hookStat', 'hookLabel', 'lowerThird', 'ctaText']:
            v = request.form.get(field, '').strip()
            if v:
                props[field] = v
        results_raw = request.form.get('results', '').strip()
        if results_raw:
            props['results'] = [r.strip() for r in results_raw.split('\n') if r.strip()]

    # Queue it — props are stashed on the job and only written to the shared
    # props file once it's actually this job's turn to render (see _do_render).
    _reel_render_jobs[jid] = {
        'status': 'queued', 'output_name': None, 'log': 'Waiting in queue...',
        'day': day, 'title': _PI_TITLES[day], 'queued_at': datetime.now().strftime('%H:%M:%S'),
        'is_story': is_story,
        'props': props,
    }
    _reel_render_queue.append(jid)
    _advance_reel_queue()
    position = _reel_render_queue.index(jid) + 1 if jid in _reel_render_queue else 0
    return jsonify({'ok': True, 'job_id': jid, 'queue_position': position})

@app.route('/instagram/editor/status/<jid>')
def instagram_editor_status(jid):
    job = _reel_render_jobs.get(jid)
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'})
    resp = {k: v for k, v in job.items() if k not in ('props', 'proc')}
    if job.get('status') == 'queued':
        resp['queue_position'] = _reel_render_queue.index(jid) + 1 if jid in _reel_render_queue else 0
    return jsonify({'ok': True, **resp})

@app.route('/instagram/editor/cancel/<jid>', methods=['POST'])
def instagram_editor_cancel(jid):
    job = _reel_render_jobs.get(jid)
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'})

    if job.get('status') == 'queued':
        if jid in _reel_render_queue:
            _reel_render_queue.remove(jid)
        job['status'] = 'cancelled'
        job['log'] = 'Cancelled while queued.'
    elif job.get('status') == 'rendering':
        job['cancel_requested'] = True
        proc = job.get('proc')
        if proc:
            proc.terminate()
        job['log'] += '\nCancelling...'
    else:
        return jsonify({'ok': False, 'error': f"Job already {job.get('status')}"})

    return jsonify({'ok': True})

@app.route('/instagram/editor/download/<filename>')
def instagram_editor_download(filename):
    if not filename.endswith('.mp4') or '/' in filename or '..' in filename:
        return 'Invalid', 400
    path = os.path.join(_REMOTION_EDITED, filename)
    if not os.path.exists(path):
        return 'Not found', 404
    return send_file(path, as_attachment=True, download_name=filename)

# ── Instagram Carousel Generator ────────────────────────────────────────────
_REMOTION_CAROUSELS = os.path.join(_REMOTION_DIR, 'public', 'carousels')
_CAROUSEL_PROPS      = os.path.join(_REMOTION_DIR, 'public', 'current_carousel_props.json')
_carousel_jobs        = {}  # job_id -> {status, day, title, slide_count, log}

# ── Instagram Story Card Generator — same text-card mechanic as the Carousel
#    Generator above, just at the Story aspect ratio. For story days with no
#    recorded HeyGen clip (e.g. out of avatar credits), this needs none —
#    it's the same hookStat/lowerThird/results/ctaText fields every day already
#    has, rendered as a tap-through sequence instead of requiring footage.
_REMOTION_STORY_CARDS = os.path.join(_REMOTION_DIR, 'public', 'story_cards')
_STORY_CARD_PROPS     = os.path.join(_REMOTION_DIR, 'public', 'current_story_carousel_props.json')
_story_card_jobs      = {}  # job_id -> {status, day, title, slide_count, log}

def _build_carousel_slides(props):
    """Turn one day's _PI_PROPS entry into the slide list CarouselSlide composition expects."""
    results = props.get('results', [])
    slides = [
        {"kind": "hook", "stat": props.get('hookStat', ''), "label": props.get('hookLabel', '')},
        {"kind": "problem", "text": props.get('lowerThird', '')},
    ]
    for i, r in enumerate(results):
        slides.append({"kind": "result", "text": r, "index": i, "total": len(results)})
    slides.append({"kind": "cta", "text": props.get('ctaText', '')})
    return slides

def _do_slide_render(job_id, slides, theme, jobs, out_base_dir, props_path, composition_id):
    """Shared by the Carousel Generator and the Story Card Generator — both render
    a sequence of static text-card slides via Remotion, just at different aspect
    ratios (CarouselSlide=1080x1350, StoryCard=1080x1920) and output dirs."""
    try:
        out_dir = os.path.join(out_base_dir, job_id)
        os.makedirs(out_dir, exist_ok=True)

        with open(props_path, 'w') as f:
            json.dump({"slides": slides, "account": "US", "theme": theme}, f, indent=2)

        n = len(slides)
        proc = subprocess.Popen(
            [_NPX, 'remotion', 'render', composition_id, out_dir,
             '--sequence', f'--frames=0-{n - 1}', '--image-format=png',
             '--image-sequence-pattern=slide_[frame].[ext]'],
            cwd=_REMOTION_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
        )
        lines = ['Rendering slides...']
        for line in proc.stdout:
            lines.append(line.rstrip())
            jobs[job_id]['log'] = '\n'.join(lines[-20:])
        proc.wait()
        if proc.returncode == 0:
            jobs[job_id].update({'status': 'done', 'slide_count': n})
        else:
            jobs[job_id]['status'] = 'error'
    except Exception:
        import traceback
        jobs[job_id].update({'status': 'error', 'log': traceback.format_exc()})

def _do_carousel_render(job_id, slides, theme='dark'):
    _do_slide_render(job_id, slides, theme, _carousel_jobs, _REMOTION_CAROUSELS, _CAROUSEL_PROPS, 'CarouselSlide')

@app.route('/instagram/carousel')
def instagram_carousel():
    carousel_days = {d: p for d, p in _PI_PROPS.items() if p.get('format') == 'carousel'}
    done_jobs = [(jid, j) for jid, j in _carousel_jobs.items() if j.get('status') == 'done']
    last_job_id, last_job = done_jobs[-1] if done_jobs else (None, None)
    return render_template('instagram_carousel.html', title='Carousel Generator',
                           props=carousel_days, titles=_PI_TITLES, jobs=_carousel_jobs,
                           last_job_id=last_job_id, last_job=last_job)

@app.route('/instagram/carousel/generate', methods=['POST'])
def instagram_carousel_generate():
    active = [j for j in _carousel_jobs.values() if j.get('status') == 'rendering']
    if active:
        return jsonify({'ok': False, 'error': 'A carousel render is already in progress. Please wait.'})

    day = int(request.form.get('day', 0))
    if day not in _PI_PROPS:
        return jsonify({'ok': False, 'error': 'Unknown day'})

    props = dict(_PI_PROPS[day])
    for field in ['hookStat', 'hookLabel', 'lowerThird', 'ctaText']:
        v = request.form.get(field, '').strip()
        if v:
            props[field] = v
    results_raw = request.form.get('results', '').strip()
    if results_raw:
        props['results'] = [r.strip() for r in results_raw.split('\n') if r.strip()]

    theme = request.form.get('theme', 'dark').strip()
    if theme not in ('dark', 'light'):
        theme = 'dark'

    slides = _build_carousel_slides(props)
    jid = str(uuid.uuid4())[:8]
    _carousel_jobs[jid] = {
        'status': 'rendering', 'day': day, 'title': _PI_TITLES.get(day, f'Day {day}'),
        'slide_count': len(slides), 'log': 'Starting render...', 'theme': theme,
        'started_at': datetime.now().strftime('%H:%M:%S'),
    }
    threading.Thread(target=_do_carousel_render, args=(jid, slides, theme), daemon=True).start()
    return jsonify({'ok': True, 'job_id': jid})

@app.route('/instagram/carousel/status/<jid>')
def instagram_carousel_status(jid):
    job = _carousel_jobs.get(jid)
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'})
    return jsonify({'ok': True, **job})

@app.route('/instagram/carousel/download/<jid>')
def instagram_carousel_download(jid):
    if '/' in jid or '..' in jid:
        return 'Invalid', 400
    job = _carousel_jobs.get(jid)
    out_dir = os.path.join(_REMOTION_CAROUSELS, jid)
    if not job or job.get('status') != 'done' or not os.path.isdir(out_dir):
        return 'Not found', 404

    pngs = sorted(f for f in os.listdir(out_dir) if f.lower().endswith('.png'))

    pdf_bytes = None
    if pngs:
        from PIL import Image
        pages = [Image.open(os.path.join(out_dir, f)).convert('RGB') for f in pngs]
        pdf_buf = io.BytesIO()
        pages[0].save(pdf_buf, format='PDF', save_all=True, append_images=pages[1:])
        pdf_bytes = pdf_buf.getvalue()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in pngs:
            zf.write(os.path.join(out_dir, fname), arcname=os.path.join('slides', fname))
        if pdf_bytes:
            zf.writestr('carousel.pdf', pdf_bytes)
    buf.seek(0)
    title = job.get('title', '').replace(' ', '_')
    day = job.get('day')
    name_parts = [f'day{day}' if day else None, 'carousel', title, jid]
    download_name = '_'.join(p for p in name_parts if p) + '.zip'
    return send_file(buf, as_attachment=True, download_name=download_name, mimetype='application/zip')

@app.route('/instagram/carousel/slide/<jid>/<fname>')
def instagram_carousel_slide(jid, fname):
    if '/' in jid or '..' in jid or '/' in fname or '..' in fname:
        return 'Invalid', 400
    return send_file(os.path.join(_REMOTION_CAROUSELS, jid, fname), mimetype='image/png')

@app.route('/instagram/story-cards')
def instagram_story_cards():
    story_days = {d: p for d, p in _PI_PROPS.items() if p.get('format') == 'story'}
    return render_template('instagram_story_cards.html', title='Story Card Generator',
                           props=story_days, titles=_PI_TITLES, jobs=_story_card_jobs)

@app.route('/instagram/story-cards/generate', methods=['POST'])
def instagram_story_cards_generate():
    active = [j for j in _story_card_jobs.values() if j.get('status') == 'rendering']
    if active:
        return jsonify({'ok': False, 'error': 'A story card render is already in progress. Please wait.'})

    day = int(request.form.get('day', 0))
    if day not in _PI_PROPS:
        return jsonify({'ok': False, 'error': 'Unknown day'})

    props = dict(_PI_PROPS[day])
    for field in ['hookStat', 'hookLabel', 'lowerThird', 'ctaText']:
        v = request.form.get(field, '').strip()
        if v:
            props[field] = v
    results_raw = request.form.get('results', '').strip()
    if results_raw:
        props['results'] = [r.strip() for r in results_raw.split('\n') if r.strip()]

    theme = request.form.get('theme', 'dark').strip()
    if theme not in ('dark', 'light'):
        theme = 'dark'

    slides = _build_carousel_slides(props)
    jid = str(uuid.uuid4())[:8]
    _story_card_jobs[jid] = {
        'status': 'rendering', 'day': day, 'title': _PI_TITLES.get(day, f'Day {day}'),
        'slide_count': len(slides), 'log': 'Starting render...', 'theme': theme,
        'started_at': datetime.now().strftime('%H:%M:%S'),
    }
    threading.Thread(target=_do_slide_render, args=(
        jid, slides, theme, _story_card_jobs, _REMOTION_STORY_CARDS, _STORY_CARD_PROPS, 'StoryCard'
    ), daemon=True).start()
    return jsonify({'ok': True, 'job_id': jid})

@app.route('/instagram/story-cards/status/<jid>')
def instagram_story_cards_status(jid):
    job = _story_card_jobs.get(jid)
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'})
    return jsonify({'ok': True, **job})

@app.route('/instagram/story-cards/download/<jid>')
def instagram_story_cards_download(jid):
    if '/' in jid or '..' in jid:
        return 'Invalid', 400
    job = _story_card_jobs.get(jid)
    out_dir = os.path.join(_REMOTION_STORY_CARDS, jid)
    if not job or job.get('status') != 'done' or not os.path.isdir(out_dir):
        return 'Not found', 404

    pngs = sorted(f for f in os.listdir(out_dir) if f.lower().endswith('.png'))

    pdf_bytes = None
    if pngs:
        from PIL import Image
        pages = [Image.open(os.path.join(out_dir, f)).convert('RGB') for f in pngs]
        pdf_buf = io.BytesIO()
        pages[0].save(pdf_buf, format='PDF', save_all=True, append_images=pages[1:])
        pdf_bytes = pdf_buf.getvalue()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in pngs:
            zf.write(os.path.join(out_dir, fname), arcname=os.path.join('frames', fname))
        if pdf_bytes:
            zf.writestr('story_cards.pdf', pdf_bytes)
    buf.seek(0)
    title = job.get('title', '').replace(' ', '_')
    day = job.get('day')
    name_parts = [f'day{day}' if day else None, 'storycards', title, jid]
    download_name = '_'.join(p for p in name_parts if p) + '.zip'
    return send_file(buf, as_attachment=True, download_name=download_name, mimetype='application/zip')

@app.route('/instagram/remotion-package')
def instagram_remotion_package():
    REEL_TEMPLATE = '''import React from "react";
import {
  AbsoluteFill, useCurrentFrame, interpolate,
  Video, staticFile, spring, Sequence
} from "remotion";

const FPS = 30;

const fade = (f: number, s: number, e: number, os: number, oe: number) =>
  interpolate(f, [s*FPS, e*FPS, os*FPS, oe*FPS], [0,1,1,0], {extrapolateLeft:"clamp",extrapolateRight:"clamp"});

const fadeIn = (f: number, s: number, e: number) =>
  interpolate(f, [s*FPS, e*FPS], [0,1], {extrapolateLeft:"clamp",extrapolateRight:"clamp"});

type Overlay = {
  type: "stat"|"split"|"bigtext"|"beforeafter";
  from: number; to: number;
  [key: string]: any;
};
export type ReelConfig = {
  title: string; videoFile: string; hookText: string; overlays: Overlay[];
};

const StatCard: React.FC<{f:number;from:number;to:number;number:string;label:string;sublabel:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  const sc = spring({frame:p.f-p.from*FPS,fps:FPS,config:{damping:12}});
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center"}}>
      <div style={{transform:`scale(${sc})`,background:"white",borderRadius:24,padding:"32px 56px",textAlign:"center",boxShadow:"0 20px 60px rgba(0,0,0,.3)"}}>
        <div style={{fontSize:80,fontWeight:900,color:"#ef4444",lineHeight:1}}>{p.number}</div>
        <div style={{fontSize:26,color:"#6b7280",marginTop:8}}>{p.label}</div>
        <div style={{fontSize:34,fontWeight:800,color:"#111827",marginTop:4}}>{p.sublabel}</div>
      </div>
    </AbsoluteFill>
  );
};

const SplitCard: React.FC<{f:number;from:number;to:number;leftEmoji:string;leftText:string;rightEmoji:string;rightText:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center",flexDirection:"row",padding:60,gap:24}}>
      {[{e:p.leftEmoji,t:p.leftText,bg:"#fee2e2",tc:"#991b1b"},{e:p.rightEmoji,t:p.rightText,bg:"#d1fae5",tc:"#065f46"}].map((s,i)=>(
        <div key={i} style={{flex:1,background:s.bg,borderRadius:20,padding:"32px 24px",textAlign:"center"}}>
          <div style={{fontSize:56}}>{s.e}</div>
          <div style={{fontSize:30,fontWeight:700,color:s.tc,marginTop:12}}>{s.t}</div>
        </div>
      ))}
    </AbsoluteFill>
  );
};

const BigText: React.FC<{f:number;from:number;to:number;text:string;sub:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center"}}>
      <div style={{textAlign:"center"}}>
        <div style={{fontSize:108,fontWeight:900,color:"white",lineHeight:1,textShadow:"2px 2px 12px rgba(0,0,0,.9)"}}>{p.text}</div>
        <div style={{fontSize:34,color:"white",marginTop:16,textShadow:"1px 1px 6px rgba(0,0,0,.8)"}}>{p.sub}</div>
      </div>
    </AbsoluteFill>
  );
};

const BeforeAfter: React.FC<{f:number;from:number;to:number;before:string;after:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  const showAfter = p.f > (p.from+1.2)*FPS;
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center"}}>
      <div style={{textAlign:"center"}}>
        <div style={{fontSize:48,fontWeight:900,color:"#ef4444",textDecoration:"line-through",opacity:showAfter?.5:1}}>{p.before}</div>
        {showAfter && <div style={{fontSize:60,fontWeight:900,color:"#10b981",marginTop:20}}>→ {p.after}</div>}
      </div>
    </AbsoluteFill>
  );
};

const EndCard: React.FC<{f:number}> = ({f}) => {
  const op = fadeIn(f,52,53);
  return (
    <AbsoluteFill style={{opacity:op,background:"rgba(0,0,0,.88)",justifyContent:"center",alignItems:"center"}}>
      <div style={{textAlign:"center"}}>
        <div style={{fontSize:52,fontWeight:900,color:"white",letterSpacing:6}}>VELARO</div>
        <div style={{fontSize:26,color:"#a78bfa",marginTop:10}}>AI Intake Systems for PI Law</div>
        <div style={{width:60,height:2,background:"#7c3aed",margin:"24px auto"}} />
        <div style={{fontSize:30,color:"white",fontWeight:600}}>Follow for more →</div>
      </div>
    </AbsoluteFill>
  );
};

export const ReelTemplate: React.FC<{config: ReelConfig}> = ({config}) => {
  const f = useCurrentFrame();
  const hookOp = fade(f,0,.5,3.5,4.5);
  return (
    <AbsoluteFill style={{width:1080,height:1920,backgroundColor:"#000",overflow:"hidden",fontFamily:"Inter,system-ui,sans-serif"}}>
      <Video src={staticFile(config.videoFile)} style={{width:"100%",height:"100%",objectFit:"cover"}} />
      <AbsoluteFill style={{background:"linear-gradient(to bottom,rgba(0,0,0,.2) 0%,transparent 35%,transparent 60%,rgba(0,0,0,.65) 100%)"}} />
      <AbsoluteFill style={{opacity:hookOp,alignItems:"flex-end",justifyContent:"flex-start",padding:60,paddingBottom:280}}>
        <div style={{color:"white",fontSize:44,fontWeight:900,lineHeight:1.2,textShadow:"2px 2px 8px rgba(0,0,0,.9)",maxWidth:960}}>{config.hookText}</div>
      </AbsoluteFill>
      {config.overlays.map((o,i)=>{
        const {type,from,to,...d}=o;
        if(type==="stat") return <StatCard key={i} f={f} from={from} to={to} {...d as any}/>;
        if(type==="split") return <SplitCard key={i} f={f} from={from} to={to} {...d as any}/>;
        if(type==="bigtext") return <BigText key={i} f={f} from={from} to={to} {...d as any}/>;
        if(type==="beforeafter") return <BeforeAfter key={i} f={f} from={from} to={to} {...d as any}/>;
        return null;
      })}
      <EndCard f={f} />
    </AbsoluteFill>
  );
};
'''

    REEL_CONFIGS = '''import {ReelConfig} from "./ReelTemplate";

export const REEL_CONFIGS: ReelConfig[] = [
  {
    title: "Day1_60SecondRule",
    videoFile: "reel1.mp4",
    hookText: "If someone calls your firm at 11pm... what happens to that inquiry?",
    overlays: [
      {type:"stat", from:8, to:14, number:"78%", label:"of leads hire the", sublabel:"FIRST firm to respond"},
      {type:"split", from:22, to:30, leftEmoji:"❌", leftText:"Voicemail", rightEmoji:"✅", rightText:"60-sec AI"},
      {type:"bigtext", from:40, to:48, text:"3x", sub:"consultations in 30 days"},
    ]
  },
  {
    title: "Day2_HalfLeadsGhosts",
    videoFile: "reel2.mp4",
    hookText: "A PI firm spending $15k/month on ads. Half their leads ghosted.",
    overlays: [
      {type:"beforeafter", from:5, to:14, before:"4 HRS 23 MIN", after:"60 SECONDS"},
      {type:"stat", from:28, to:36, number:"+3", label:"consultations booked", sublabel:"in week one"},
    ]
  },
  {
    title: "Day3_WhatAIDoes",
    videoFile: "reel3.mp4",
    hookText: "AI for law firms gets thrown around. Here\\'s what it actually does.",
    overlays: [
      {type:"stat", from:6, to:13, number:"60s", label:"response time", sublabel:"to every single inquiry"},
      {type:"stat", from:40, to:48, number:"0", label:"missed after-hours contacts", sublabel:"after install"},
    ]
  },
  {
    title: "Day4_NotTheAds",
    videoFile: "reel4.mp4",
    hookText: "Our ads aren\\'t working. I hear this constantly. It\\'s almost never the ads.",
    overlays: [
      {type:"bigtext", from:22, to:30, text:"THE AD WORKED.", sub:"The intake failed."},
      {type:"stat", from:38, to:46, number:"2–3x", label:"conversion rate", sublabel:"same budget, better system"},
    ]
  },
  {
    title: "Day5_30DaysOfData",
    videoFile: "reel5.mp4",
    hookText: "We ran a 30-day audit on a PI firm\\'s intake. The numbers were hard to look at.",
    overlays: [
      {type:"stat", from:6, to:13, number:"62%", label:"of after-hours inquiries", sublabel:"got no response until morning"},
      {type:"stat", from:18, to:25, number:"40%", label:"had already hired", sublabel:"another firm by 9am"},
      {type:"bigtext", from:42, to:50, text:"+8", sub:"consultations in month one"},
    ]
  },
  {
    title: "Day6_8ExtraConsultations",
    videoFile: "reel6.mp4",
    hookText: "Same ad budget. Same attorneys. 8 more consultations in month one.",
    overlays: [
      {type:"bigtext", from:5, to:12, text:"+8", sub:"consultations from after-hours alone"},
      {type:"beforeafter", from:22, to:32, before:"3-5 HR RESPONSE + VOICEMAIL", after:"60 SECONDS, 24/7"},
    ]
  },
  {
    title: "Day7_HonestTake",
    videoFile: "reel7.mp4",
    hookText: "Honest take: AI won\\'t replace your intake team. Here\\'s what it actually replaces.",
    overlays: [
      {type:"stat", from:28, to:36, number:"0", label:"missed contacts", sublabel:"after-hours · weekends · simultaneous calls"},
    ]
  },
];
'''

    ROOT_TSX = '''import {Composition} from "remotion";
import {ReelTemplate} from "./compositions/ReelTemplate";
import {REEL_CONFIGS} from "./compositions/reelConfigs";

export const RemotionRoot = () => (
  <>
    {REEL_CONFIGS.map((config, i) => (
      <Composition
        key={i}
        id={config.title}
        component={() => <ReelTemplate config={config} />}
        durationInFrames={60 * 30}
        fps={30}
        width={1080}
        height={1920}
        defaultProps={{config}}
      />
    ))}
  </>
);
'''

    PKG_JSON = '''{
  "name": "velaro-reels",
  "version": "1.0.0",
  "scripts": {
    "start": "remotion studio",
    "build": "remotion render"
  },
  "dependencies": {
    "react": "^18.0.0",
    "react-dom": "^18.0.0",
    "remotion": "^4.0.0",
    "@remotion/cli": "^4.0.0"
  },
  "devDependencies": {
    "@types/react": "^18.0.0",
    "typescript": "^5.0.0"
  }
}
'''

    REMOTION_CONFIG = '''import {Config} from "@remotion/cli/config";
Config.setVideoImageFormat("jpeg");
Config.setOverwriteOutput(true);
'''

    README = '''# Velaro Reels — Remotion Project

## Setup (one time)
1. npm install
2. Put your HeyGen MP4 files in /public/ named: reel1.mp4, reel2.mp4, ... reel7.mp4

## Preview in browser
npm run start
→ Opens Remotion Studio, click any composition to preview

## Render a single reel
npx remotion render Day1_60SecondRule out/day1.mp4 --codec=h264

## Render all 7 reels
for i in 1 2 3 4 5 6 7; do
  npx remotion render $(ls src/compositions/reelConfigs.ts | head -1) out/day${i}.mp4
done

## Or render all in one command
npx remotion render RemotionRoot --sequence --codec=h264 --output=out/

## Requirements
- Node.js 18+
- npm install (first time only)

## Output format
1080x1920 (9:16), 30fps, 60 seconds each

## Workflow
1. Generate avatar video in HeyGen → download as MP4
2. Rename to reel1.mp4, reel2.mp4, etc → put in /public/
3. npm run start → preview in browser
4. Render when happy → upload to Instagram as Reel
'''

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('package.json', PKG_JSON)
        zf.writestr('remotion.config.ts', REMOTION_CONFIG)
        zf.writestr('src/Root.tsx', ROOT_TSX)
        zf.writestr('src/compositions/ReelTemplate.tsx', REEL_TEMPLATE)
        zf.writestr('src/compositions/reelConfigs.ts', REEL_CONFIGS)
        zf.writestr('public/PUT_HEYGEN_MP4_FILES_HERE.txt', 'Name your HeyGen exports: reel1.mp4, reel2.mp4, reel3.mp4, reel4.mp4, reel5.mp4, reel6.mp4, reel7.mp4')
        zf.writestr('README.md', README)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='velaro-remotion.zip', mimetype='application/zip')

@app.route('/instagram/remotion-compositions')
def instagram_remotion_compositions():
    REEL_TEMPLATE = '''import React from "react";
import {
  AbsoluteFill, useCurrentFrame, interpolate,
  Video, staticFile, spring
} from "remotion";

const FPS = 30;

const fade = (f: number, s: number, e: number, os: number, oe: number) =>
  interpolate(f, [s*FPS, e*FPS, os*FPS, oe*FPS], [0,1,1,0], {extrapolateLeft:"clamp",extrapolateRight:"clamp"});

const fadeIn = (f: number, s: number, e: number) =>
  interpolate(f, [s*FPS, e*FPS], [0,1], {extrapolateLeft:"clamp",extrapolateRight:"clamp"});

export type ReelConfig = {
  title: string;
  videoFile: string;
  hookText: string;
  captionLines: string[];   // shown at bottom, 4s each, auto-timed
  overlays: Array<{type:"stat"|"split"|"bigtext"|"beforeafter"; from:number; to:number; [key:string]:any}>;
};

// ── Caption bar ──────────────────────────────────────────────────────────────
const CaptionBar: React.FC<{f:number; lines:string[]}> = ({f, lines}) => {
  const lineIdx = Math.min(Math.floor(f / (FPS * 4)), lines.length - 1);
  const line = lines[lineIdx];
  if (!line) return null;
  return (
    <AbsoluteFill style={{justifyContent:"flex-end",alignItems:"center",paddingBottom:90}}>
      <div style={{
        background:"rgba(0,0,0,.72)",
        color:"white",
        fontSize:28,
        fontWeight:600,
        lineHeight:1.35,
        textAlign:"center",
        maxWidth:900,
        padding:"14px 28px",
        borderRadius:12,
        letterSpacing:.3,
      }}>{line}</div>
    </AbsoluteFill>
  );
};

// ── Overlay components ───────────────────────────────────────────────────────
const StatCard: React.FC<{f:number;from:number;to:number;number:string;label:string;sublabel:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  const sc = spring({frame:p.f-p.from*FPS,fps:FPS,config:{damping:12}});
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center"}}>
      <div style={{transform:`scale(${sc})`,background:"white",borderRadius:24,padding:"32px 56px",textAlign:"center",boxShadow:"0 20px 60px rgba(0,0,0,.3)"}}>
        <div style={{fontSize:80,fontWeight:900,color:"#ef4444",lineHeight:1}}>{p.number}</div>
        <div style={{fontSize:26,color:"#6b7280",marginTop:8}}>{p.label}</div>
        <div style={{fontSize:34,fontWeight:800,color:"#111827",marginTop:4}}>{p.sublabel}</div>
      </div>
    </AbsoluteFill>
  );
};

const SplitCard: React.FC<{f:number;from:number;to:number;leftEmoji:string;leftText:string;rightEmoji:string;rightText:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center",flexDirection:"row",padding:60,gap:24}}>
      {[{e:p.leftEmoji,t:p.leftText,bg:"#fee2e2",tc:"#991b1b"},{e:p.rightEmoji,t:p.rightText,bg:"#d1fae5",tc:"#065f46"}].map((s,i)=>(
        <div key={i} style={{flex:1,background:s.bg,borderRadius:20,padding:"32px 24px",textAlign:"center"}}>
          <div style={{fontSize:56}}>{s.e}</div>
          <div style={{fontSize:30,fontWeight:700,color:s.tc,marginTop:12}}>{s.t}</div>
        </div>
      ))}
    </AbsoluteFill>
  );
};

const BigText: React.FC<{f:number;from:number;to:number;text:string;sub:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center"}}>
      <div style={{textAlign:"center"}}>
        <div style={{fontSize:108,fontWeight:900,color:"white",lineHeight:1,textShadow:"2px 2px 12px rgba(0,0,0,.9)"}}>{p.text}</div>
        <div style={{fontSize:34,color:"white",marginTop:16,textShadow:"1px 1px 6px rgba(0,0,0,.8)"}}>{p.sub}</div>
      </div>
    </AbsoluteFill>
  );
};

const BeforeAfter: React.FC<{f:number;from:number;to:number;before:string;after:string}> = (p) => {
  const op = fade(p.f,p.from,p.from+.5,p.to-.5,p.to);
  const showAfter = p.f > (p.from+1.2)*FPS;
  return (
    <AbsoluteFill style={{opacity:op,justifyContent:"center",alignItems:"center"}}>
      <div style={{textAlign:"center"}}>
        <div style={{fontSize:48,fontWeight:900,color:"#ef4444",textDecoration:"line-through",opacity:showAfter?.5:1}}>{p.before}</div>
        {showAfter && <div style={{fontSize:60,fontWeight:900,color:"#10b981",marginTop:20}}>→ {p.after}</div>}
      </div>
    </AbsoluteFill>
  );
};

const EndCard: React.FC<{f:number}> = ({f}) => {
  const op = fadeIn(f,52,53);
  return (
    <AbsoluteFill style={{opacity:op,background:"rgba(0,0,0,.88)",justifyContent:"center",alignItems:"center"}}>
      <div style={{textAlign:"center"}}>
        <div style={{fontSize:52,fontWeight:900,color:"white",letterSpacing:6}}>VELARO</div>
        <div style={{fontSize:26,color:"#a78bfa",marginTop:10}}>AI Intake Systems for PI Law</div>
        <div style={{width:60,height:2,background:"#7c3aed",margin:"24px auto"}} />
        <div style={{fontSize:30,color:"white",fontWeight:600}}>Follow for more →</div>
      </div>
    </AbsoluteFill>
  );
};

// ── Main composition ─────────────────────────────────────────────────────────
export const ReelTemplate: React.FC<{config: ReelConfig}> = ({config}) => {
  const f = useCurrentFrame();
  const hookOp = fade(f,0,.5,3.5,4.5);
  return (
    <AbsoluteFill style={{width:1080,height:1920,backgroundColor:"#000",overflow:"hidden",fontFamily:"Inter,system-ui,sans-serif"}}>
      <Video src={staticFile(config.videoFile)} style={{width:"100%",height:"100%",objectFit:"cover"}} />
      <AbsoluteFill style={{background:"linear-gradient(to bottom,rgba(0,0,0,.2) 0%,transparent 35%,transparent 55%,rgba(0,0,0,.7) 100%)"}} />

      {/* Hook text */}
      <AbsoluteFill style={{opacity:hookOp,alignItems:"flex-end",justifyContent:"flex-start",padding:60,paddingBottom:280}}>
        <div style={{color:"white",fontSize:44,fontWeight:900,lineHeight:1.2,textShadow:"2px 2px 8px rgba(0,0,0,.9)",maxWidth:960}}>{config.hookText}</div>
      </AbsoluteFill>

      {/* Motion graphic overlays */}
      {config.overlays.map((o,i)=>{
        const {type,from,to,...d}=o;
        if(type==="stat") return <StatCard key={i} f={f} from={from} to={to} {...d as any}/>;
        if(type==="split") return <SplitCard key={i} f={f} from={from} to={to} {...d as any}/>;
        if(type==="bigtext") return <BigText key={i} f={f} from={from} to={to} {...d as any}/>;
        if(type==="beforeafter") return <BeforeAfter key={i} f={f} from={from} to={to} {...d as any}/>;
        return null;
      })}

      {/* Auto captions — change every 4 seconds */}
      <CaptionBar f={f} lines={config.captionLines} />

      <EndCard f={f} />
    </AbsoluteFill>
  );
};
'''

    REEL_CONFIGS = '''import {ReelConfig} from "./ReelTemplate";

export const REEL_CONFIGS: ReelConfig[] = [
  {
    title: "Day1_60SecondRule",
    videoFile: "reel1.mp4",
    hookText: "If someone calls your firm at 11pm... what happens?",
    captionLines: [
      "Here\\'s the truth most PI attorneys don\\'t want to admit.",
      "That call goes to voicemail. The next morning...",
      "...that person already hired the firm that called back.",
      "78% hire the first attorney who responds. The first.",
      "We built AI that answers every inquiry in 60 seconds.",
      "24 hours a day. Zero extra staff.",
      "One firm tripled consultations in 30 days. Same ad spend.",
      "I\\'m AJ. This is what we build for PI firms. Follow for more.",
    ],
    overlays: [
      {type:"stat", from:8, to:14, number:"78%", label:"of leads hire the", sublabel:"FIRST firm to respond"},
      {type:"split", from:22, to:30, leftEmoji:"❌", leftText:"Voicemail", rightEmoji:"✅", rightText:"60-sec AI"},
      {type:"bigtext", from:40, to:48, text:"3x", sub:"consultations in 30 days"},
    ]
  },
  {
    title: "Day2_HalfLeadsGhosts",
    videoFile: "reel2.mp4",
    hookText: "A PI firm spending $15k/month on ads. Half their leads ghosted.",
    captionLines: [
      "They weren\\'t bad leads. They were just slow.",
      "We audited their intake — avg response time: 4 hrs 23 min.",
      "By then, the lead had already booked with a competitor.",
      "We cut response time to 60 seconds with AI intake.",
      "Week one: 3 consultations booked automatically, after hours.",
      "Cases they would have completely lost.",
      "This is what we build at Velaro for PI firms.",
      "Follow and I\\'ll show you exactly how it works.",
    ],
    overlays: [
      {type:"beforeafter", from:5, to:14, before:"4 HRS 23 MIN", after:"60 SECONDS"},
      {type:"stat", from:28, to:36, number:"+3", label:"consultations booked", sublabel:"in week one"},
    ]
  },
  {
    title: "Day3_WhatAIDoes",
    videoFile: "reel3.mp4",
    hookText: "AI for law firms. Here\\'s what it actually does — step by step.",
    captionLines: [
      "Someone gets hurt. They find your firm. They fill out a form.",
      "Normally — nothing happens until Monday morning.",
      "With our system: AI responds in 60 seconds.",
      "Asks the right questions. Accident type. Injuries. Insurance.",
      "If the case qualifies — books the consultation automatically.",
      "No human needed. After-hours call? Voice AI answers.",
      "One firm went from missing 40% of after-hours leads to zero.",
      "Not magic. Just systems that don\\'t sleep.",
    ],
    overlays: [
      {type:"stat", from:6, to:13, number:"60s", label:"response time", sublabel:"to every inquiry"},
      {type:"stat", from:40, to:48, number:"0", label:"missed after-hours contacts", sublabel:"after install"},
    ]
  },
  {
    title: "Day4_NotTheAds",
    videoFile: "reel4.mp4",
    hookText: "\\"Our ads aren\\'t working.\\" It\\'s almost never the ads.",
    captionLines: [
      "I talk to PI attorneys every week who want to pause their ads.",
      "But when we dig into the intake data — the leads were fine.",
      "Someone clicked the ad at 9pm, filled a form, and waited.",
      "By morning they\\'d booked with another firm.",
      "The ad worked perfectly. The intake failed.",
      "We fix the intake side — 60-second AI response, 24/7.",
      "Same ad budget → 2 to 3x better conversion.",
      "Don\\'t pause your ads. Fix what happens after the click.",
    ],
    overlays: [
      {type:"bigtext", from:22, to:30, text:"THE AD WORKED.", sub:"The intake failed."},
      {type:"stat", from:38, to:46, number:"2–3x", label:"conversion rate", sublabel:"same budget, better system"},
    ]
  },
  {
    title: "Day5_30DaysOfData",
    videoFile: "reel5.mp4",
    hookText: "We ran a 30-day audit on a PI firm\\'s intake. Hard to look at.",
    captionLines: [
      "62% of after-hours inquiries — no response until morning.",
      "Of those, 40% had already signed with another firm by 9am.",
      "This firm was running $12,000 a month in ads.",
      "Losing half their leads to something fixable.",
      "We built them AI intake. 60-second responses, after hours.",
      "Every missed call called back automatically.",
      "Month one: zero missed after-hours contacts.",
      "Eight additional consultations that would have been lost.",
    ],
    overlays: [
      {type:"stat", from:6, to:13, number:"62%", label:"of after-hours inquiries", sublabel:"no response until morning"},
      {type:"stat", from:18, to:25, number:"40%", label:"had already hired", sublabel:"another firm by 9am"},
      {type:"bigtext", from:42, to:50, text:"+8", sub:"consultations in month one"},
    ]
  },
  {
    title: "Day6_8ExtraConsultations",
    videoFile: "reel6.mp4",
    hookText: "Same budget. Same attorneys. 8 more consultations. What changed?",
    captionLines: [
      "A PI firm in Texas. Two partners, six attorneys.",
      "Good lead volume. Conversion was flat.",
      "Response times: 3 to 5 hours during the day.",
      "Voicemail after 5pm and all weekend.",
      "We built their AI intake system over four weeks.",
      "Every inquiry — call, form, chat — answered in 60 seconds.",
      "First month: 8 additional consultations from after-hours alone.",
      "Same ads. Same staff. Different system.",
    ],
    overlays: [
      {type:"bigtext", from:5, to:12, text:"+8", sub:"consultations, month one"},
      {type:"beforeafter", from:22, to:32, before:"VOICEMAIL AFTER 5PM", after:"60-SEC AI, 24/7"},
    ]
  },
  {
    title: "Day7_HonestTake",
    videoFile: "reel7.mp4",
    hookText: "Honest take: AI won\\'t replace your intake team.",
    captionLines: [
      "I want to be direct about this.",
      "AI won\\'t replace the relationship between attorney and client.",
      "What AI replaces: the 11pm voicemail that never got called back.",
      "The weekend form that sat in an inbox until Monday.",
      "The 5 simultaneous inquiries when 3 couldn\\'t get through.",
      "Your team handles warm conversations.",
      "AI makes sure nothing falls through before Monday morning.",
      "The firm down the street installed this 6 months ago. Are you?",
    ],
    overlays: [
      {type:"stat", from:28, to:36, number:"0", label:"missed contacts", sublabel:"after-hours · weekends · simultaneous calls"},
    ]
  },
];
'''

    ROOT_ADDITION = '''// ── Add this to your existing Root.tsx ────────────────────────────────────────
// 1. Import at the top of your Root.tsx:
//    import { ReelTemplate } from "./compositions/ReelTemplate";
//    import { REEL_CONFIGS } from "./compositions/reelConfigs";
//
// 2. Add inside your RemotionRoot component:
//    {REEL_CONFIGS.map((config, i) => (
//      <Composition
//        key={i}
//        id={config.title}
//        component={() => <ReelTemplate config={config} />}
//        durationInFrames={60 * 30}
//        fps={30}
//        width={1080}
//        height={1920}
//        defaultProps={{ config }}
//      />
//    ))}
//
// 3. Put your HeyGen MP4s in /public/ named reel1.mp4 ... reel7.mp4
//
// 4. Render:
//    npx remotion render Day1_60SecondRule out/day1.mp4 --codec=h264
//
// 5. To edit captions: open reelConfigs.ts → edit captionLines array for each day
//    Each line shows for 4 seconds. Add/remove lines to adjust timing.
'''

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('compositions/ReelTemplate.tsx', REEL_TEMPLATE)
        zf.writestr('compositions/reelConfigs.ts', REEL_CONFIGS)
        zf.writestr('HOW_TO_ADD_TO_EXISTING_PROJECT.ts', ROOT_ADDITION)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='velaro-compositions.zip', mimetype='application/zip')

@app.route('/action-plan')
def action_plan():
    c = db()
    # Ensure start_date exists
    row = c.execute("SELECT value FROM settings WHERE key='start_date'").fetchone()
    if not row:
        today_str = datetime.now().strftime('%Y-%m-%d')
        c.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('start_date',?)", (today_str,))
        c.commit()
        start_date = today_str
    else:
        start_date = row[0]
    from datetime import date as _date
    try:
        sd = _date.fromisoformat(start_date)
        day_num = (date.today() - sd).days + 1
    except Exception:
        day_num = 1
    tasks = c.execute('SELECT * FROM action_plan_tasks ORDER BY phase, id').fetchall()
    c.close()
    phases = {
        0: {'label': 'Phase 0 — Fix the Foundation', 'days': 'Days 1–3'},
        1: {'label': 'Phase 1 — LinkedIn Ramp',       'days': 'Days 4–14'},
        2: {'label': 'Phase 2 — Scale + Discovery Calls', 'days': 'Days 15–45'},
        3: {'label': 'Phase 3 — Close + Deliver',     'days': 'Days 46–70'},
        4: {'label': 'Phase 4 — Scale + Re-warm Email','days': 'Days 71–90'},
    }
    grouped = {}
    for ph in phases:
        ph_tasks = [t for t in tasks if t['phase'] == ph]
        done = sum(1 for t in ph_tasks if t['is_done'])
        grouped[ph] = {'tasks': ph_tasks, 'done': done, 'total': len(ph_tasks)}
    return render_template('action_plan.html', grouped=grouped, phases=phases, day_num=min(day_num,90), start_date=start_date)

@app.route('/api/action-plan/toggle', methods=['POST'])
def toggle_action_task():
    data = request.get_json()
    tid = int(data.get('id', 0))
    is_done = int(data.get('is_done', 0))
    c = db()
    c.execute("UPDATE action_plan_tasks SET is_done=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (is_done, tid))
    c.commit()
    c.close()
    return jsonify({'ok': True})

@app.route('/roi-calculator')
def roi_calculator():
    return render_template('roi_calculator.html', title='ROI Calculator')

@app.route('/scripts')
def scripts():
    return render_template('scripts.html', title='Scripts')

# ── LinkedIn Manual Log ─────────────────────────────────────────────────────

@app.route('/li-log')
def li_log():
    c = db()
    today = datetime.now().strftime('%Y-%m-%d')
    logs = c.execute('''
        SELECT * FROM li_manual_log
        ORDER BY replied ASC, follow_up_date ASC, sent_at DESC
    ''').fetchall()
    # Stats
    total = c.execute('SELECT COUNT(*) FROM li_manual_log').fetchone()[0]
    replied = c.execute('SELECT COUNT(*) FROM li_manual_log WHERE replied=1').fetchone()[0]
    due_today = c.execute(
        "SELECT COUNT(*) FROM li_manual_log WHERE replied=0 AND follow_up_date <= ?", (today,)
    ).fetchone()[0]
    sent_today = c.execute(
        "SELECT COUNT(*) FROM li_manual_log WHERE DATE(sent_at)=DATE('now')"
    ).fetchone()[0]
    c.close()
    return render_template('li_log.html', logs=logs, today=today,
                           total=total, replied=replied, due_today=due_today, sent_today=sent_today)

@app.route('/api/li-log/add', methods=['POST'])
def li_log_add():
    d = request.get_json() or {}
    name     = d.get('name','').strip()
    firm     = d.get('firm','').strip()
    username = d.get('username','').strip()
    action   = d.get('action','dm')
    notes    = d.get('notes','').strip()
    sent_at  = d.get('sent_at', datetime.now().strftime('%Y-%m-%d'))
    # Auto follow-up dates based on action type
    fu_days  = {'voice_note': 3, 'dm': 3, 'connect': 3, 'video': 7, 'comment': 2}.get(action, 3)
    from datetime import date as _date
    fu_date  = (datetime.strptime(sent_at,'%Y-%m-%d').date() + timedelta(days=fu_days)).strftime('%Y-%m-%d')
    c = db()
    c.execute('''INSERT INTO li_manual_log (name,firm,username,action,notes,sent_at,follow_up_date)
                 VALUES (?,?,?,?,?,?,?)''', (name,firm,username,action,notes,sent_at,fu_date))
    c.commit()
    new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.close()
    return jsonify({'ok': True, 'id': new_id, 'follow_up_date': fu_date})

@app.route('/api/li-log/reply', methods=['POST'])
def li_log_reply():
    d = request.get_json() or {}
    lid = int(d.get('id',0))
    replied = int(d.get('replied',1))
    notes   = d.get('notes','')
    c = db()
    c.execute('UPDATE li_manual_log SET replied=?, reply_notes=? WHERE id=?', (replied, notes, lid))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/li-log/delete', methods=['POST'])
def li_log_delete():
    lid = int((request.get_json() or {}).get('id', 0))
    c = db()
    c.execute('DELETE FROM li_manual_log WHERE id=?', (lid,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/li-log/snooze', methods=['POST'])
def li_log_snooze():
    d = request.get_json() or {}
    lid  = int(d.get('id', 0))
    days = int(d.get('days', 3))
    c = db()
    c.execute("UPDATE li_manual_log SET follow_up_date=DATE(follow_up_date, '+' || ? || ' days') WHERE id=?",
              (days, lid))
    c.commit()
    new_date = c.execute('SELECT follow_up_date FROM li_manual_log WHERE id=?', (lid,)).fetchone()[0]
    c.close()
    return jsonify({'ok': True, 'new_date': new_date})

# ── Cold Email Manual Log ───────────────────────────────────────────────────

@app.route('/email-log')
def email_log():
    c = db()
    today = datetime.now().strftime('%Y-%m-%d')
    logs = c.execute('''
        SELECT * FROM email_manual_log
        ORDER BY replied ASC, follow_up_date ASC, sent_at DESC
    ''').fetchall()
    total = c.execute('SELECT COUNT(*) FROM email_manual_log').fetchone()[0]
    replied = c.execute('SELECT COUNT(*) FROM email_manual_log WHERE replied=1').fetchone()[0]
    due_today = c.execute(
        "SELECT COUNT(*) FROM email_manual_log WHERE replied=0 AND follow_up_date <= ?", (today,)
    ).fetchone()[0]
    sent_today = c.execute(
        "SELECT COUNT(*) FROM email_manual_log WHERE DATE(sent_at)=DATE('now')"
    ).fetchone()[0]
    c.close()
    return render_template('email_log.html', logs=logs, today=today,
                           total=total, replied=replied, due_today=due_today, sent_today=sent_today)

@app.route('/api/email-log/add', methods=['POST'])
def email_log_add():
    d = request.get_json() or {}
    name    = d.get('name','').strip()
    firm    = d.get('firm','').strip()
    email   = d.get('email','').strip()
    stage   = d.get('stage','email1')
    notes   = d.get('notes','').strip()
    sent_at = d.get('sent_at', datetime.now().strftime('%Y-%m-%d'))
    # Email 1 -> Email 2 follow-up at 5 days, Email 2 -> Email 3 (breakup) at 5 more,
    # Email 3 is the breakup — no further scheduled follow-up.
    fu_days = {'email1': 5, 'email2': 5, 'email3': None}.get(stage, 5)
    fu_date = None
    if fu_days is not None:
        fu_date = (datetime.strptime(sent_at,'%Y-%m-%d').date() + timedelta(days=fu_days)).strftime('%Y-%m-%d')
    c = db()
    c.execute('''INSERT INTO email_manual_log (name,firm,email,stage,notes,sent_at,follow_up_date)
                 VALUES (?,?,?,?,?,?,?)''', (name,firm,email,stage,notes,sent_at,fu_date))
    c.commit()
    new_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.close()
    return jsonify({'ok': True, 'id': new_id, 'follow_up_date': fu_date})

@app.route('/api/email-log/reply', methods=['POST'])
def email_log_reply():
    d = request.get_json() or {}
    eid = int(d.get('id',0))
    replied = int(d.get('replied',1))
    notes   = d.get('notes','')
    c = db()
    c.execute('UPDATE email_manual_log SET replied=?, reply_notes=? WHERE id=?', (replied, notes, eid))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/email-log/delete', methods=['POST'])
def email_log_delete():
    eid = int((request.get_json() or {}).get('id', 0))
    c = db()
    c.execute('DELETE FROM email_manual_log WHERE id=?', (eid,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/email-log/snooze', methods=['POST'])
def email_log_snooze():
    d = request.get_json() or {}
    eid  = int(d.get('id', 0))
    days = int(d.get('days', 3))
    c = db()
    c.execute("UPDATE email_manual_log SET follow_up_date=DATE(follow_up_date, '+' || ? || ' days') WHERE id=?",
              (days, eid))
    c.commit()
    new_date = c.execute('SELECT follow_up_date FROM email_manual_log WHERE id=?', (eid,)).fetchone()[0]
    c.close()
    return jsonify({'ok': True, 'new_date': new_date})

# ── Post Presets ────────────────────────────────────────────────────────────

@app.route('/post-presets')
def post_presets():
    return render_template('post_presets.html', title='Post Presets')

# ── LinkedIn Session Tracker ────────────────────────────────────────────────

LI_SESSION_LIMITS = {
    'like':    {'label': 'Likes',        'limit': 50,  'color': 'indigo'},
    'comment': {'label': 'Comments',     'limit': 20,  'color': 'violet'},
    'connect': {'label': 'Connections',  'limit': 15,  'color': 'blue'},
    'dm':      {'label': 'DMs',          'limit': 25,  'color': 'sky'},
    'view':    {'label': 'Profile Views','limit': 70,  'color': 'teal'},
    'voice':   {'label': 'Voice Notes',  'limit': 20,  'color': 'purple'},
}

@app.route('/li-session')
def li_session():
    c = db()
    today = datetime.now().strftime('%Y-%m-%d')
    rows = c.execute(
        "SELECT action, count FROM li_session_log WHERE date=?", (today,)
    ).fetchall()
    c.close()
    counts = {r['action']: r['count'] for r in rows}
    return render_template('li_session.html', counts=counts,
                           limits=LI_SESSION_LIMITS, today=today)

@app.route('/api/session/inc', methods=['POST'])
def session_inc():
    d      = request.get_json() or {}
    action = d.get('action','')
    delta  = int(d.get('delta', 1))
    if action not in LI_SESSION_LIMITS:
        return jsonify({'ok': False, 'error': 'unknown action'})
    c = db()
    c.execute('''
        INSERT INTO li_session_log (date, action, count) VALUES (DATE('now'), ?, MAX(0,?))
        ON CONFLICT(date, action) DO UPDATE SET count = MAX(0, count + ?)
    ''', (action, delta, delta))
    c.commit()
    new_count = c.execute(
        "SELECT count FROM li_session_log WHERE date=DATE('now') AND action=?", (action,)
    ).fetchone()[0]
    c.close()
    return jsonify({'ok': True, 'count': new_count})

@app.route('/api/session/reset', methods=['POST'])
def session_reset():
    c = db()
    c.execute("DELETE FROM li_session_log WHERE date=DATE('now')")
    c.commit(); c.close()
    return jsonify({'ok': True})

# ── Video Editor (Loom recording helper) ───────────────────────────────────

@app.route('/video-editor')
def video_editor():
    return render_template('video_editor.html', title='Video Editor')

@app.route('/demo')
def demo():
    firm = request.args.get('firm', 'Rodriguez Law Group')
    return render_template('demo.html', firm=firm)

# ── Video Studio (Remotion full editor) ────────────────────────────────────

REMOTION_DIR = os.path.join(os.path.dirname(__file__), 'remotion')
UPLOAD_DIR   = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

_render_jobs = {}  # job_id -> {'status','progress','output','error'}

@app.route('/video-studio')
def video_studio():
    return render_template('video_studio.html', title='Video Studio')

@app.route('/api/studio/upload', methods=['POST'])
def studio_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext not in {'mp4', 'mov', 'avi', 'webm', 'mkv'}:
        return jsonify({'error': 'Must be mp4/mov/avi/webm/mkv'}), 400
    vid_id = str(uuid.uuid4())[:8]
    fname  = f'{vid_id}.{ext}'
    fpath  = os.path.join(UPLOAD_DIR, fname)
    f.save(fpath)
    # probe duration with ffprobe
    duration = 60.0
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', fpath],
            capture_output=True, text=True, timeout=15
        )
        info = json.loads(r.stdout)
        duration = float(info['format'].get('duration', 60))
    except Exception:
        pass
    return jsonify({
        'id': vid_id,
        'filename': fname,
        'url': f'http://localhost:5001/static/uploads/{fname}',
        'duration': round(duration, 1),
    })

@app.route('/api/studio/cut', methods=['POST'])
def studio_cut():
    data     = request.get_json()
    input_url = data.get('input', '')
    segments  = data.get('segments', [])
    if not input_url or not segments:
        return jsonify({'error': 'Missing input or segments'}), 400

    uploads_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
    filename    = input_url.split('/')[-1]
    input_path  = os.path.join(uploads_dir, filename)
    if not os.path.exists(input_path):
        return jsonify({'error': 'Source file not found'}), 404

    out_name = f'cut_{uuid.uuid4().hex[:8]}_{filename}'
    out_path = os.path.join(uploads_dir, out_name)

    if len(segments) == 1:
        # Single segment — simple trim
        s = segments[0]
        dur = s['end'] - s['start']
        result = subprocess.run([
            'ffmpeg', '-y', '-ss', str(s['start']), '-i', input_path,
            '-t', str(dur), '-c', 'copy', out_path
        ], capture_output=True, text=True)
    else:
        # Multiple segments — use concat filter
        n = len(segments)
        filter_parts = []
        for i, s in enumerate(segments):
            filter_parts.append(
                f"[0:v]trim=start={s['start']}:end={s['end']},setpts=PTS-STARTPTS[v{i}];"
                f"[0:a]atrim=start={s['start']}:end={s['end']},asetpts=PTS-STARTPTS[a{i}]"
            )
        inputs  = ''.join(f'[v{i}][a{i}]' for i in range(n))
        concat  = f"{inputs}concat=n={n}:v=1:a=1[outv][outa]"
        filter_str = ';'.join(filter_parts) + ';' + concat
        result = subprocess.run([
            'ffmpeg', '-y', '-i', input_path,
            '-filter_complex', filter_str,
            '-map', '[outv]', '-map', '[outa]',
            out_path
        ], capture_output=True, text=True)

    if result.returncode != 0:
        return jsonify({'error': result.stderr[-500:]}), 500
    return jsonify({'output': f'http://localhost:5001/static/uploads/{out_name}'})

@app.route('/api/studio/trim', methods=['POST'])
def studio_trim():
    data  = request.get_json()
    input_url = data.get('input', '')
    start = float(data.get('start', 0))
    end   = float(data.get('end', 0))
    if not input_url or end <= start:
        return jsonify({'error': 'Invalid trim params'}), 400
    # Resolve URL to local path
    uploads_dir = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
    filename    = input_url.split('/')[-1]
    input_path  = os.path.join(uploads_dir, filename)
    if not os.path.exists(input_path):
        return jsonify({'error': 'Source file not found'}), 404
    out_name  = f'trim_{uuid.uuid4().hex[:8]}_{filename}'
    out_path  = os.path.join(uploads_dir, out_name)
    duration  = end - start
    result = subprocess.run([
        'ffmpeg', '-y',
        '-ss', str(start),
        '-i', input_path,
        '-t', str(duration),
        '-c', 'copy',
        out_path
    ], capture_output=True, text=True)
    if result.returncode != 0:
        return jsonify({'error': result.stderr[-400:]}), 500
    return jsonify({'output': f'http://localhost:5001/static/uploads/{out_name}'})

@app.route('/api/studio/remotion-ready')
def remotion_ready():
    import urllib.request
    try:
        urllib.request.urlopen('http://localhost:3001', timeout=2)
        return jsonify({'ready': True})
    except Exception:
        return jsonify({'ready': False})

@app.route('/api/studio/save-props', methods=['POST'])
def studio_save_props():
    data = request.get_json()
    # Auto-detect face cam duration if not already set
    face_src = data.get('faceSrc', '')
    if face_src and not data.get('faceDuration'):
        local_path = None
        if face_src.startswith('http://localhost:5001/static/'):
            rel = face_src.replace('http://localhost:5001/static/', '')
            local_path = os.path.join(os.path.dirname(__file__), 'static', rel)
        elif face_src.startswith('/static/'):
            local_path = os.path.join(os.path.dirname(__file__), face_src.lstrip('/'))
        if local_path and os.path.exists(local_path):
            try:
                r = subprocess.run(
                    ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', local_path],
                    capture_output=True, text=True, timeout=10
                )
                probe = json.loads(r.stdout)
                for s in probe.get('streams', []):
                    if s.get('codec_type') == 'video':
                        data['faceDuration'] = round(float(s['duration']), 2)
                        break
            except Exception:
                pass
    base = os.path.dirname(__file__)
    props_path = os.path.join(base, 'remotion', 'src', 'last-props.json')
    os.makedirs(os.path.dirname(props_path), exist_ok=True)
    with open(props_path, 'w') as f:
        json.dump(data, f, indent=2)
    # Also write to static for fallback
    static_path = os.path.join(base, 'static', 'last-props.json')
    with open(static_path, 'w') as f:
        json.dump(data, f, indent=2)
    # Restart Remotion Studio with --props so the videos load in the editor
    def restart():
        import time; time.sleep(0.3)
        _start_remotion(props_file='./src/last-props.json')
    threading.Thread(target=restart, daemon=True).start()
    return jsonify({'ok': True, 'restarting': True})

@app.route('/api/studio/render', methods=['POST'])
def studio_render():
    d = request.get_json() or {}
    composition = d.get('composition', 'FaceEdit')
    props       = d.get('props', {})
    out_name    = f"{str(uuid.uuid4())[:8]}_{composition}.mp4"
    out_path    = os.path.join(UPLOAD_DIR, out_name)
    job_id      = str(uuid.uuid4())[:8]

    # Disable sounds if MP3 files are not present in remotion/public/sounds/
    sounds_dir = os.path.join(REMOTION_DIR, 'public', 'sounds')
    has_sounds = any(f.endswith('.mp3') for f in os.listdir(sounds_dir)) if os.path.isdir(sounds_dir) else False
    if not has_sounds:
        props['enableSounds'] = False

    _render_jobs[job_id] = {'status': 'running', 'progress': 0, 'output': None, 'error': None}

    def run_render():
        try:
            cmd = [
                'npx', 'remotion', 'render',
                composition,
                out_path,
                '--props', json.dumps(props),
                '--log', 'error',
            ]
            result = subprocess.run(
                cmd, cwd=REMOTION_DIR,
                capture_output=True, text=True, timeout=600
            )
            if result.returncode == 0:
                _render_jobs[job_id]['status']   = 'done'
                _render_jobs[job_id]['output']   = f'http://localhost:5001/static/uploads/{out_name}'
            else:
                _render_jobs[job_id]['status'] = 'error'
                _render_jobs[job_id]['error']  = result.stderr[-1000:]
        except Exception as e:
            _render_jobs[job_id]['status'] = 'error'
            _render_jobs[job_id]['error']  = str(e)

    threading.Thread(target=run_render, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/studio/render/<job_id>')
def studio_render_status(job_id):
    job = _render_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(job)

# ── VAPI Calls ───────────────────────────────────────────────────────────────

STATE_TZ = {
    "TX": "America/Chicago", "FL": "America/New_York", "GA": "America/New_York",
    "CA": "America/Los_Angeles", "NY": "America/New_York", "IL": "America/Chicago",
    "OH": "America/New_York", "PA": "America/New_York", "AZ": "America/Phoenix",
    "CO": "America/Denver", "NC": "America/New_York", "VA": "America/New_York",
    "WA": "America/Los_Angeles", "TN": "America/Chicago", "MO": "America/Chicago",
    "AL": "America/Chicago", "SC": "America/New_York", "LA": "America/Chicago",
    "KY": "America/New_York", "OR": "America/Los_Angeles",
}

def init_vapi_tables(c):
    c.executescript("""
        CREATE TABLE IF NOT EXISTS vapi_leads (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            firm_name      TEXT NOT NULL,
            phone          TEXT,
            direct_number  TEXT,
            city           TEXT,
            state          TEXT,
            timezone       TEXT,
            lead_type      TEXT DEFAULT 'COLD',
            google_reviews INTEGER DEFAULT 0,
            score          INTEGER DEFAULT 5,
            notes          TEXT,
            do_not_call    INTEGER DEFAULT 0,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS vapi_calls (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            lead_name        TEXT, firm_name TEXT, phone_used TEXT,
            call_type        TEXT, state TEXT, lead_type TEXT,
            outcome          TEXT, email_collected TEXT, phone_collected TEXT,
            best_day TEXT, best_time TEXT, callback_date TEXT,
            called_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            attempt_number   INTEGER DEFAULT 1,
            next_action TEXT, next_action_date TEXT, notes TEXT, vapi_call_id TEXT
        );
        CREATE TABLE IF NOT EXISTS vapi_emails (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outcome TEXT, lead_name TEXT, firm_name TEXT,
            to_email TEXT, subject TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, success INTEGER
        );
    """)
    # settings table for runtime config (warm-up mode etc.)
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    # migrate: add missing columns
    lead_cols = {r[1] for r in c.execute("PRAGMA table_info(vapi_leads)").fetchall()}
    if 'score' not in lead_cols:
        c.execute("ALTER TABLE vapi_leads ADD COLUMN score INTEGER DEFAULT 5")
    if 'no_answer_dead' not in lead_cols:
        c.execute("ALTER TABLE vapi_leads ADD COLUMN no_answer_dead INTEGER DEFAULT 0")
    call_cols = {r[1] for r in c.execute("PRAGMA table_info(vapi_calls)").fetchall()}
    if 'ab_variant' not in call_cols:
        c.execute("ALTER TABLE vapi_calls ADD COLUMN ab_variant TEXT DEFAULT 'A-Pain'")
    c.commit()

@app.route('/calls')
def calls_dashboard():
    c = db()
    init_vapi_tables(c)

    # highest score first — leads that keep missing drop to the bottom
    leads = c.execute("""
        SELECT * FROM vapi_leads
        WHERE do_not_call = 0 AND (no_answer_dead IS NULL OR no_answer_dead = 0)
        ORDER BY score DESC, created_at DESC
    """).fetchall()

    no_answer_leads = c.execute("""
        SELECT * FROM vapi_leads
        WHERE no_answer_dead = 1
        ORDER BY created_at DESC
    """).fetchall()

    # stats
    calls_today  = c.execute("SELECT COUNT(*) FROM vapi_calls WHERE DATE(datetime(called_at, '+330 minutes'))=DATE(datetime('now', '+330 minutes'))").fetchone()[0]
    hot_total    = c.execute("SELECT COUNT(*) FROM vapi_calls WHERE outcome='HOT'").fetchone()[0]
    warm_total   = c.execute("SELECT COUNT(*) FROM vapi_calls WHERE outcome='WARM'").fetchone()[0]
    vm_total     = c.execute("SELECT COUNT(*) FROM vapi_calls WHERE outcome='VOICEMAIL'").fetchone()[0]
    ni_total     = c.execute("SELECT COUNT(*) FROM vapi_calls WHERE outcome='NOT_INTERESTED'").fetchone()[0]
    total_calls  = c.execute("SELECT COUNT(*) FROM vapi_calls").fetchone()[0]
    queue_total      = c.execute("SELECT COUNT(*) FROM vapi_leads WHERE do_not_call=0 AND (no_answer_dead IS NULL OR no_answer_dead=0)").fetchone()[0]
    dnc_total        = c.execute("SELECT COUNT(*) FROM vapi_leads WHERE do_not_call=1").fetchone()[0]
    no_answer_total  = c.execute("SELECT COUNT(*) FROM vapi_leads WHERE no_answer_dead=1").fetchone()[0]

    # A/B variant performance
    ab_stats_raw = c.execute("""
        SELECT ab_variant,
               COUNT(*) as calls,
               SUM(CASE WHEN outcome='HOT'  THEN 1 ELSE 0 END) as hot,
               SUM(CASE WHEN outcome='WARM' THEN 1 ELSE 0 END) as warm,
               SUM(CASE WHEN outcome IN ('HOT','WARM') THEN 1 ELSE 0 END) as converted
        FROM vapi_calls
        WHERE ab_variant IS NOT NULL
        GROUP BY ab_variant
        ORDER BY converted DESC
    """).fetchall()
    ab_stats = [dict(r) for r in ab_stats_raw]
    for row in ab_stats:
        row['rate'] = round(row['converted'] / row['calls'] * 100) if row['calls'] else 0

    recent_calls = c.execute(
        "SELECT * FROM vapi_calls ORDER BY called_at DESC LIMIT 30"
    ).fetchall()

    hot_leads = c.execute(
        "SELECT * FROM vapi_calls WHERE outcome='HOT' ORDER BY called_at DESC"
    ).fetchall()

    callbacks = c.execute(
        "SELECT * FROM vapi_calls WHERE outcome='CALLBACK' ORDER BY callback_date ASC"
    ).fetchall()

    # daily limit (warm-up mode)
    row_lim = c.execute("SELECT value FROM settings WHERE key='daily_call_limit'").fetchone()
    daily_limit = int(row_lim[0]) if row_lim else 16

    c.close()
    return render_template('calls.html',
        leads=leads, recent_calls=recent_calls,
        hot_leads=hot_leads, callbacks=callbacks,
        calls_today=calls_today, hot_total=hot_total,
        warm_total=warm_total, vm_total=vm_total,
        ni_total=ni_total, total_calls=total_calls,
        ab_stats=ab_stats, state_tz=STATE_TZ,
        daily_limit=daily_limit,
        queue_total=queue_total, dnc_total=dnc_total,
        no_answer_leads=no_answer_leads, no_answer_total=no_answer_total)

@app.route('/api/calls/set-daily-limit', methods=['POST'])
def set_daily_limit():
    limit = request.json.get('limit')
    try:
        limit = int(limit)
        if limit < 1 or limit > 200:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'Invalid limit'})
    c = db()
    init_vapi_tables(c)
    c.execute("INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('daily_call_limit', ?, CURRENT_TIMESTAMP)", (str(limit),))
    c.commit(); c.close()
    return jsonify({'ok': True, 'limit': limit})

@app.route('/calls/leads/add', methods=['POST'])
def calls_add_lead():
    c = db()
    init_vapi_tables(c)
    state = (request.form.get('state') or '').upper().strip()
    tz = request.form.get('timezone') or STATE_TZ.get(state, 'America/Chicago')
    c.execute("""
        INSERT INTO vapi_leads (name,firm_name,phone,direct_number,city,state,timezone,lead_type,google_reviews,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        request.form.get('name','').strip(),
        request.form.get('firm_name','').strip(),
        request.form.get('phone','').strip(),
        request.form.get('direct_number','').strip(),
        request.form.get('city','').strip(),
        state,
        tz,
        (request.form.get('lead_type') or 'COLD').upper(),
        int(request.form.get('google_reviews') or 0),
        request.form.get('notes','').strip(),
    ))
    c.commit(); c.close()
    flash('Lead added to call queue', 'success')
    return redirect(url_for('calls_dashboard'))

@app.route('/calls/leads/<int:lid>/delete', methods=['POST'])
def calls_delete_lead(lid):
    c = db()
    c.execute("DELETE FROM vapi_leads WHERE id=?", (lid,))
    c.commit(); c.close()
    flash('Lead removed', 'success')
    return redirect(url_for('calls_dashboard'))

@app.route('/calls/leads/<int:lid>/edit', methods=['GET', 'POST'])
def calls_edit_lead(lid):
    c = db()
    if request.method == 'GET':
        lead = c.execute("SELECT * FROM vapi_leads WHERE id=?", (lid,)).fetchone()
        c.close()
        if not lead:
            return jsonify({'error': 'Not found'}), 404
        return jsonify(dict(lead))
    # POST — update
    state = (request.form.get('state') or '').upper().strip()
    tz = request.form.get('timezone') or STATE_TZ.get(state, 'America/Chicago')
    c.execute("""
        UPDATE vapi_leads SET
            name=?, firm_name=?, phone=?, direct_number=?,
            city=?, state=?, timezone=?, lead_type=?, google_reviews=?, notes=?
        WHERE id=?
    """, (
        request.form.get('name','').strip(),
        request.form.get('firm_name','').strip(),
        request.form.get('phone','').strip(),
        request.form.get('direct_number','').strip(),
        request.form.get('city','').strip(),
        state, tz,
        (request.form.get('lead_type') or 'COLD').upper(),
        int(request.form.get('google_reviews') or 0),
        request.form.get('notes','').strip(),
        lid,
    ))
    c.commit(); c.close()
    flash('Lead updated', 'success')
    return redirect(url_for('calls_dashboard'))

@app.route('/api/vapi-webhook', methods=['POST'])
def vapi_webhook():
    """
    VAPI sends a POST here when any call ends (inbound or outbound).
    Paste your public URL + /api/vapi-webhook into the VAPI Advanced → Webhook Server field.
    Use ngrok for local: ngrok http 5001 → https://xxxx.ngrok.io/api/vapi-webhook
    """
    data = request.get_json(silent=True) or {}

    msg_type = data.get("message", {}).get("type", "") if "message" in data else data.get("type", "")

    # VAPI wraps events in a "message" key
    payload = data.get("message", data)
    if payload.get("type") not in ("end-of-call-report", "call-ended"):
        return jsonify({"ok": True, "skipped": True})

    call      = payload.get("call", {}) or {}
    analysis  = payload.get("analysis", {}) or {}
    artifact  = payload.get("artifact", {}) or {}
    summary   = analysis.get("summary", "") or payload.get("summary", "") or ""

    call_id      = call.get("id", "") or payload.get("callId", "")
    call_type_raw = call.get("type", "")  # "inboundPhoneCall" or "outboundPhoneCall"
    call_source  = "INBOUND" if "inbound" in call_type_raw.lower() else "OUTBOUND"

    structured = analysis.get("structuredData", {}) or {}

    outcome             = (structured.get("outcome") or "NO_ANSWER").upper()
    email_collected     = structured.get("email", "")
    phone_collected     = structured.get("phone", "")
    best_day            = structured.get("bestDay", "")
    best_time           = structured.get("bestTime", "")
    callback_date       = structured.get("callbackDate", "")
    attorneys           = structured.get("attorneys", "")
    runs_ads            = structured.get("runsAds", "")
    after_hours_process = structured.get("afterHoursProcess", "")
    weekly_inquiries    = structured.get("weeklyInquiries", "")
    avg_case_value      = structured.get("avgCaseValue", "")
    caller_name         = structured.get("callerName", "")
    firm_name_inbound   = structured.get("firmName", "")

    # for inbound calls, lead info comes from structured data
    customer    = call.get("customer", {}) or {}
    caller_num  = customer.get("number", "")

    c = db()
    init_vapi_tables(c)

    # check if this call_id already logged (idempotency)
    existing = c.execute("SELECT id FROM vapi_calls WHERE vapi_call_id=?", (call_id,)).fetchone()
    if existing:
        c.close()
        return jsonify({"ok": True, "duplicate": True})

    # next action
    outcome_actions = {
        "HOT":            ("Send calendar invite",  datetime.now().strftime("%Y-%m-%d")),
        "WARM":           ("Send demo email",        datetime.now().strftime("%Y-%m-%d")),
        "CALLBACK":       ("Ayaan calls on callback date", callback_date or ""),
        "VOICEMAIL":      ("Retry call",             (datetime.now()+timedelta(days=3)).strftime("%Y-%m-%d")),
        "NO_ANSWER":      ("Retry call next day",    (datetime.now()+timedelta(days=1)).strftime("%Y-%m-%d")),
        "NOT_INTERESTED": ("No action for 30 days",  (datetime.now()+timedelta(days=30)).strftime("%Y-%m-%d")),
        "DO_NOT_CALL":    ("Never retry", ""),
    }
    next_action, next_date = outcome_actions.get(outcome, ("Retry", ""))

    # for inbound: lead_name may be unknown until Jordan gets it
    lead_name = caller_name or "Inbound Caller"
    firm_name = firm_name_inbound or "Unknown Firm"

    # try to match to existing vapi_lead by phone
    if caller_num:
        clean_num = caller_num.replace("+1","").replace("-","").replace(" ","").replace("(","").replace(")","")
        row = c.execute(
            "SELECT name, firm_name, state, lead_type FROM vapi_leads WHERE REPLACE(REPLACE(phone,'-',''),' ','')=? OR REPLACE(REPLACE(direct_number,'-',''),' ','')=?",
            (clean_num, clean_num)
        ).fetchone()
        if row:
            lead_name = row["name"]
            firm_name = row["firm_name"]

    c.execute("""
        INSERT INTO vapi_calls
            (lead_name, firm_name, phone_used, call_type, outcome,
             email_collected, phone_collected, best_day, best_time, callback_date,
             attorneys, runs_ads, after_hours_process, weekly_inquiries, avg_case_value,
             next_action, next_action_date, notes, vapi_call_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        lead_name, firm_name, caller_num, call_source, outcome,
        email_collected, phone_collected, best_day, best_time, callback_date,
        attorneys, runs_ads, after_hours_process, weekly_inquiries, avg_case_value,
        next_action, next_date, summary, call_id
    ))
    c.commit()

    # ── sync to main CRM leads table ─────────────────────────────
    crm_stage_map = {
        "HOT":            "call_booked",
        "WARM":           "replied",
        "CALLBACK":       "replied",
        "NOT_INTERESTED": "lost",
        "DO_NOT_CALL":    "lost",
    }
    crm_stage = crm_stage_map.get(outcome)
    if crm_stage:
        contact_email = email_collected or ""
        contact_phone = phone_collected or caller_num or ""
        # try to find existing lead by email, phone, or name+firm
        existing_lead = None
        if contact_email:
            existing_lead = c.execute(
                "SELECT id FROM leads WHERE email=?", (contact_email,)
            ).fetchone()
        if not existing_lead and contact_phone:
            clean = contact_phone.replace("+1","").replace("-","").replace(" ","").replace("(","").replace(")","")
            existing_lead = c.execute(
                "SELECT id FROM leads WHERE REPLACE(REPLACE(REPLACE(phone,'-',''),' ',''),'(','')=?", (clean,)
            ).fetchone()
        if not existing_lead:
            existing_lead = c.execute(
                "SELECT id FROM leads WHERE name=? AND company=?", (lead_name, firm_name)
            ).fetchone()

        crm_notes = f"[VAPI {call_source}] Outcome: {outcome}. {summary or ''}"
        if existing_lead:
            c.execute(
                "UPDATE leads SET pipeline_stage=?, email=COALESCE(NULLIF(email,''),?), phone=COALESCE(NULLIF(phone,''),?), notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (crm_stage, contact_email, contact_phone, crm_notes, existing_lead["id"])
            )
        else:
            c.execute(
                """INSERT INTO leads (name, company, email, phone, niche, source, pipeline_stage, notes)
                   VALUES (?,?,?,?,'law','VAPI Call',?,?)""",
                (lead_name, firm_name, contact_email, contact_phone, crm_stage, crm_notes)
            )
        c.commit()

    # auto-send follow-up email for HOT/WARM
    if outcome in ("HOT", "WARM") and email_collected:
        import subprocess as sp
        sp.Popen([
            "python3",
            os.path.join(os.path.dirname(__file__), "send_followup_email.py"),
            outcome, lead_name, firm_name, email_collected,
            best_day, best_time,
        ])

    c.close()
    return jsonify({"ok": True, "outcome": outcome, "source": call_source})

@app.route('/api/retell-webhook', methods=['POST'])
def retell_webhook():
    """
    Retell sends a POST here when a call ends.
    Set this URL in Retell Dashboard → Agent → Webhook URL.
    """
    data = request.get_json(silent=True) or {}
    event = data.get("event", "")

    if event not in ("call_ended", "call_analyzed"):
        return jsonify({"ok": True, "skipped": True})

    call = data.get("call", {}) or {}
    call_id   = call.get("call_id", "")
    analysis  = call.get("call_analysis") or {}
    summary   = analysis.get("call_summary", "") or ""
    transcript = call.get("transcript", "") or ""
    disconnection_reason = call.get("disconnection_reason", "")

    # determine direction
    from_num = call.get("from_number", "")
    to_num   = call.get("to_number", "")
    call_source = "OUTBOUND" if from_num == "+18324089822" else "INBOUND"
    caller_num  = to_num if call_source == "OUTBOUND" else from_num

    # map disconnection reason → outcome
    reason_map = {
        "voicemail_reached":    "VOICEMAIL",
        "machine_detected":     "VOICEMAIL",
        "dial_no_answer":       "NO_ANSWER",
        "dial_busy":            "NO_ANSWER",
        "dial_failed":          "NO_ANSWER",
        "inactivity":           "NO_ANSWER",
        "concurrency_limit_reached": "NO_ANSWER",
        "call_transfer":        "HOT",
    }
    outcome = reason_map.get(disconnection_reason, "NO_ANSWER")

    t = (transcript + " " + summary).lower()
    if any(w in t for w in ["book", "schedule", "calendar", "15 minutes", "send me the link"]):
        outcome = "HOT"
    elif any(w in t for w in ["send me an email", "email me", "send the demo", "sounds interesting"]):
        outcome = "WARM"
    elif any(w in t for w in ["call back", "better time", "call me"]):
        outcome = "CALLBACK"
    elif any(w in t for w in ["not interested", "remove me", "don't call", "do not call"]):
        outcome = "NOT_INTERESTED"
    elif disconnection_reason in ("voicemail_reached", "machine_detected"):
        outcome = "VOICEMAIL"

    if analysis.get("call_successful") and outcome not in ("HOT", "WARM", "CALLBACK"):
        outcome = "WARM"

    outcome_actions = {
        "HOT":            ("Send calendar invite",  datetime.now().strftime("%Y-%m-%d")),
        "WARM":           ("Send demo email",        datetime.now().strftime("%Y-%m-%d")),
        "CALLBACK":       ("Ayaan calls on callback date", ""),
        "VOICEMAIL":      ("Retry call",             (datetime.now()+timedelta(days=3)).strftime("%Y-%m-%d")),
        "NO_ANSWER":      ("Retry call next day",    (datetime.now()+timedelta(days=1)).strftime("%Y-%m-%d")),
        "NOT_INTERESTED": ("No action for 30 days",  (datetime.now()+timedelta(days=30)).strftime("%Y-%m-%d")),
        "DO_NOT_CALL":    ("Never retry", ""),
    }
    next_action, next_date = outcome_actions.get(outcome, ("Retry", ""))

    lead_name = "Outbound Lead"
    firm_name = "Unknown Firm"

    c = db()
    init_vapi_tables(c)

    # idempotency
    if c.execute("SELECT id FROM vapi_calls WHERE vapi_call_id=?", (call_id,)).fetchone():
        c.close()
        return jsonify({"ok": True, "duplicate": True})

    # try to match lead by phone
    if caller_num:
        clean_num = caller_num.replace("+1","").replace("-","").replace(" ","").replace("(","").replace(")","")
        row = c.execute(
            "SELECT name, firm_name FROM vapi_leads WHERE REPLACE(REPLACE(phone,'-',''),' ','')=? OR REPLACE(REPLACE(direct_number,'-',''),' ','')=?",
            (clean_num, clean_num)
        ).fetchone()
        if row:
            lead_name = row["name"]
            firm_name = row["firm_name"]

    c.execute("""
        INSERT INTO vapi_calls
            (lead_name, firm_name, phone_used, call_type, outcome,
             next_action, next_action_date, notes, vapi_call_id)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (lead_name, firm_name, caller_num, call_source, outcome,
          next_action, next_date, summary, call_id))
    c.commit()

    crm_stage_map = {
        "HOT": "call_booked", "WARM": "replied",
        "CALLBACK": "replied", "NOT_INTERESTED": "lost", "DO_NOT_CALL": "lost",
    }
    crm_stage = crm_stage_map.get(outcome)
    if crm_stage:
        contact_phone = caller_num or ""
        existing_lead = c.execute(
            "SELECT id FROM leads WHERE name=? AND company=?", (lead_name, firm_name)
        ).fetchone()
        crm_notes = f"[Retell {call_source}] Outcome: {outcome}. {summary or ''}"
        if existing_lead:
            c.execute("UPDATE leads SET pipeline_stage=?, notes=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                      (crm_stage, crm_notes, existing_lead["id"]))
        else:
            c.execute("""INSERT INTO leads (name, company, phone, niche, source, pipeline_stage, notes)
                         VALUES (?,?,?,'law','Retell Call',?,?)""",
                      (lead_name, firm_name, contact_phone, crm_stage, crm_notes))
        c.commit()

    c.close()
    return jsonify({"ok": True, "outcome": outcome, "source": call_source})

@app.route('/calls/leads/<int:lid>/dnc', methods=['POST'])
def calls_dnc_lead(lid):
    c = db()
    c.execute("UPDATE vapi_leads SET do_not_call=1 WHERE id=?", (lid,))
    c.commit(); c.close()
    flash('Marked do not call', 'success')
    return redirect(url_for('calls_dashboard'))

# ── Number Scraper ────────────────────────────────────────────────────────────

@app.route('/calls/scraper')
def calls_scraper():
    c = db(); init_vapi_tables(c); c.close()
    return render_template('calls_scraper.html', title='Number Scraper')

@app.route('/api/calls/scrape', methods=['POST'])
def calls_scrape_numbers():
    """Scrape Google local search results for PI law firm phone numbers."""
    import re, subprocess as _sp
    data    = request.get_json(silent=True) or {}
    query   = (data.get('query') or '').strip()
    if not query:
        return jsonify({'ok': False, 'error': 'No query'})

    search_url = f"https://www.google.com/search?q={query.replace(' ','+')}&num=20&tbm=lcl"
    try:
        out = _sp.run([
            'curl', '-sL', '--max-time', '15',
            '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            '-H', 'Accept-Language: en-US,en;q=0.9',
            '-H', 'Accept: text/html,application/xhtml+xml',
            search_url
        ], capture_output=True, text=True, timeout=20)
        html = out.stdout
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, 'html.parser')
    PHONE_RE = re.compile(r'\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}')

    results = []
    seen_phones = set()

    # parse local pack divs
    for div in soup.find_all('div', class_=True):
        text = div.get_text(' ', strip=True)
        phones = PHONE_RE.findall(text)
        if not phones:
            continue
        ph = re.sub(r'\D', '', phones[0])
        if len(ph) != 10 or ph in seen_phones:
            continue
        seen_phones.add(ph)
        # try to get a name near this element
        name = ''
        for tag in ['h3', 'span', 'a']:
            el = div.find(tag)
            if el and len(el.get_text(strip=True)) > 4:
                name = el.get_text(strip=True)[:60]
                break
        results.append({'name': name or 'Unknown Firm', 'phone': ph})

    # also run phone regex over raw text as fallback
    if not results:
        raw_phones = PHONE_RE.findall(soup.get_text(' '))
        for p in raw_phones:
            ph = re.sub(r'\D', '', p)
            if len(ph) == 10 and ph not in seen_phones:
                seen_phones.add(ph)
                results.append({'name': '', 'phone': ph})

    return jsonify({'ok': True, 'results': results[:30], 'count': len(results)})

@app.route('/api/calls/parse-text', methods=['POST'])
def calls_parse_text():
    """Extract firm names + phones from pasted text (Google Maps, Apollo, etc.)."""
    import re
    data = request.get_json(silent=True) or {}
    text = data.get('text', '')
    if not text:
        return jsonify({'ok': False, 'error': 'No text'})

    PHONE_RE  = re.compile(r'\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}')
    lines = text.splitlines()
    results, seen = [], set()

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        phones = PHONE_RE.findall(line)
        if phones:
            ph = re.sub(r'\D', '', phones[0])
            if len(ph) == 10 and ph not in seen:
                seen.add(ph)
                # look back up to 4 lines for a firm name
                name = ''
                for j in range(i-1, max(i-5, -1), -1):
                    candidate = lines[j].strip()
                    if candidate and not PHONE_RE.search(candidate) and len(candidate) > 3:
                        name = candidate[:80]
                        break
                results.append({'name': name or f'Firm #{len(results)+1}', 'phone': ph})
        i += 1

    return jsonify({'ok': True, 'results': results, 'count': len(results)})

@app.route('/api/calls/import-csv', methods=['POST'])
def calls_import_csv():
    """Parse Google Maps CSV → extract firm name, phone, state, reviews → add to call queue."""
    import csv, io, re
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'No file'})

    ltype     = request.form.get('lead_type', 'COLD')
    content   = f.read().decode('utf-8', errors='ignore')
    reader    = csv.reader(io.StringIO(content))

    PHONE_RE  = re.compile(r'\(?\d{3}\)?[\s\-\.]\d{3}[\s\-\.]\d{4}')
    VALID_STATES = {'AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN',
                    'IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV',
                    'NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN',
                    'TX','UT','VT','VA','WA','WV','WI','WY','DC'}
    STATE_RE  = re.compile(r'(?:,\s*|·\s*)([A-Z]{2})\b')
    # same filters as /scraper page
    ALLOWED_TYPES = ('personal injury attorney', 'law firm', 'personal injury lawyer',
                     'trial attorney', 'accident attorney', 'injury attorney')

    c = db(); init_vapi_tables(c)
    added, skipped, dupes = 0, 0, 0

    for row in reader:
        if len(row) < 6:
            continue
        firm_name = (row[0] or '').strip()
        biz_type  = (row[1] or '').lstrip('·•· ').strip().lower()
        try:
            reviews = int(re.sub(r'\D','', row[3] or '0') or 0)
        except Exception:
            reviews = 0
        details   = row[4] if len(row) > 4 else ''
        phone_raw = row[5] if len(row) > 5 else ''

        # Filter 1 (same as scraper): category must be PI attorney or law firm
        if not any(a in biz_type for a in ALLOWED_TYPES):
            skipped += 1; continue

        # Filter 2 (same as scraper): skip if "open 24 hours" anywhere in the row
        all_vals_lower = ' '.join(v.lower() for v in row)
        if 'open 24 hours' in all_vals_lower:
            skipped += 1; continue

        if not firm_name or not phone_raw:
            skipped += 1; continue

        # clean phone
        phone = re.sub(r'\D', '', phone_raw)
        if len(phone) == 11 and phone.startswith('1'):
            phone = phone[1:]
        if len(phone) != 10:
            skipped += 1; continue

        # extract state from details column (e.g. "Dallas, TX · 15 years in business")
        state = ''
        for match in STATE_RE.findall(details.upper()):
            if match in VALID_STATES:
                state = match
                break

        # check duplicate by phone
        if c.execute("SELECT id FROM vapi_leads WHERE REPLACE(REPLACE(phone,'-',''),' ','')=?", (phone,)).fetchone():
            dupes += 1; continue

        base_score = 7 if reviews >= 10 else 5
        # lead name = "Managing Partner" since we don't know individual
        c.execute(
            "INSERT INTO vapi_leads (name,firm_name,phone,state,lead_type,google_reviews,score) VALUES (?,?,?,?,?,?,?)",
            ("Managing Partner", firm_name, phone, state, ltype, reviews, base_score)
        )
        added += 1

    c.commit(); c.close()
    return jsonify({'ok': True, 'added': added, 'skipped': skipped, 'dupes': dupes})

@app.route('/api/calls/clear-history', methods=['POST'])
def calls_clear_history():
    c = db()
    deleted = c.execute("SELECT COUNT(*) FROM vapi_calls").fetchone()[0]
    c.execute("DELETE FROM vapi_calls")
    c.commit(); c.close()
    return jsonify({'ok': True, 'deleted': deleted})

@app.route('/api/calls/add-batch', methods=['POST'])
def calls_add_batch():
    """Bulk-add scraped leads to the call queue."""
    data  = request.get_json(silent=True) or {}
    leads = data.get('leads', [])
    state = (data.get('state') or '').upper()
    city  = data.get('city', '')
    ltype = data.get('lead_type', 'COLD')

    c = db(); init_vapi_tables(c)
    added, skipped = 0, 0
    for lead in leads:
        phone = re.sub(r'\D', '', lead.get('phone', ''))
        name  = (lead.get('name') or '').strip() or 'Unknown'
        firm  = (lead.get('firm') or lead.get('name') or '').strip() or 'Unknown Firm'
        if not phone or len(phone) != 10:
            skipped += 1; continue
        exists = c.execute(
            "SELECT id FROM vapi_leads WHERE REPLACE(REPLACE(phone,'-',''),' ','')=?",
            (phone,)
        ).fetchone()
        if exists:
            skipped += 1; continue
        reviews = int(lead.get('google_reviews') or 0)
        # score: 5 base + 2 if 10+ reviews (HIGH_VOLUME signal)
        base_score = 7 if reviews >= 10 else 5
        c.execute(
            "INSERT INTO vapi_leads (name,firm_name,phone,city,state,lead_type,google_reviews,score) VALUES (?,?,?,?,?,?,?,?)",
            (name, firm, phone, city, state, ltype, reviews, base_score)
        )
        added += 1
    c.commit(); c.close()
    return jsonify({'ok': True, 'added': added, 'skipped': skipped})

# ── Cold Email Sender Dashboard ─────────────────────────────────────────────

import smtplib, random, time as _time, threading
import schedule as _schedule
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

# 6 templates all targeting firms actively hiring intake staff
# 6 templates — all targeting firms actively hiring intake staff (Indeed leads)
COLD_EMAIL_TEMPLATES = [
    {
        "name": "One question before the hire",
        "subject": "one question before you hire",
        "body": """Hi {first_name},

Saw {firm_name} is looking for an intake coordinator.

Before you fill the role — quick question: is the problem that you need more people, or that inquiries aren't getting responded to fast enough?

Most PI firms we work with tried hiring first. The real issue was response time — calls going unanswered after hours, web forms sitting for hours. No hire solves that unless they work 24/7.

We built something that does. Happy to show you a 2-min demo if it's worth a look before you make the hire.

— AJ""",
    },
    {
        "name": "The $45k math",
        "subject": "hiring intake at {firm_name}?",
        "body": """Hi {first_name},

Quick math if you're weighing an intake hire:

An intake coordinator runs $38k–$48k/year. Add benefits, training, turnover. And they still can't answer calls at 11pm or handle five inquiries at once.

We automate the same function for PI firms — every call answered, every web form responded to in under 60 seconds, cases pre-qualified automatically — for a fraction of that cost.

One firm in {city} was about to post the role. Decided against it after seeing this. They now handle more volume with less overhead.

Worth a 15-min look before {firm_name} makes the hire?

AJ
Velaro""",
    },
    {
        "name": "The intake hire cycle",
        "subject": "the intake hire problem",
        "body": """Hi {first_name},

Talked to a PI firm last month that had hired four intake coordinators in three years.

Same pattern every time: new hire comes in, handles volume fine at first, burns out from after-hours calls and overnight web forms, leaves. Repeat.

They stopped replacing the person and replaced the process instead. AI handles every inquiry — 24/7, under 60 seconds, pre-qualifies the case before a human touches it. Their current coordinator only handles warm, confirmed leads.

Sharing because I noticed {firm_name} is hiring for intake. Thought it might be worth knowing there's an option that doesn't have a turnover problem.

AJ""",
    },
    {
        "name": "What the hire can't cover",
        "subject": "what your intake hire can't cover",
        "body": """Hi {first_name},

When {firm_name} hires an intake coordinator, they'll cover 9–5 well.

But PI accidents don't happen on a schedule. The 9pm call, the 2am web form, the Saturday morning inquiry — those go to whichever firm responds first. Usually not the one with a coordinator who's off the clock.

We handle exactly that gap. Every inquiry — at any hour — gets a response in under 60 seconds, pre-qualified, and routed. Your intake person focuses on the warm leads we hand them.

Worth seeing how it looks before you complete the hire?

AJ
Velaro""",
    },
    {
        "name": "Before the role is posted",
        "subject": "before {firm_name} fills the intake role",
        "body": """Hi {first_name},

Came across the intake opening at {firm_name}.

Most PI firms hire for intake when case volume picks up — makes sense. But the firms that convert the most aren't always the ones with the most staff. They're the ones with the fastest first response.

We built an intake system for a {city} PI firm earlier this year — every call answered automatically, web forms responded to in 60 seconds, cases screened before an attorney gets involved. Their conversion rate tripled in 30 days without adding headcount.

If you haven't committed to the hire yet, might be worth a quick look at what this actually costs vs. a salary.

AJ""",
    },
    {
        "name": "Automation vs hire comparison",
        "subject": "instead of the intake hire",
        "body": """Hi {first_name},

Two ways to fix an intake problem at a PI firm:

1. Hire a coordinator. $40k–$50k/year, works 9–5, needs managing, eventually leaves.
2. Automate it. Answers every call, every hour, every web form in under 60 seconds. No turnover.

We build option 2 for PI firms. One firm in {city} was mid-hiring-process when they saw it. Cancelled the search, went live in 4 weeks, handled more volume than the person would have.

Not saying hire is wrong — just worth knowing both options before {firm_name} commits.

AJ
Velaro""",
    },

    # 7. Overnight shift — for firms posting specifically for overnight/after-hours coverage
    {
        "name": "Overnight shift hook",
        "subject": "the overnight intake role at {firm_name}",
        "body": """Hi {first_name},

Saw {firm_name} posted for an overnight intake specialist.

That role exists because calls come in after hours and nobody's picking up — accidents don't follow a 9-5 schedule.

We automate exactly that. Every call answered overnight, first ring. Every web form responded to in 60 seconds. Cases pre-qualified before anyone needs to get involved. One firm cancelled their overnight hire after seeing it — saved $55k a year and handled more volume than the person would have.

Worth a 15-minute look before you fill the role?

AJ
Velaro""",
    },

    # 8. After-hours role — same signal, different job title framing
    {
        "name": "After-hours role hook",
        "subject": "re: after-hours intake at {firm_name}",
        "body": """Hi {first_name},

Noticed the after-hours intake role at {firm_name}.

Quick thought before you hire: the firms that convert the most after-hours inquiries aren't the ones with the biggest staff — they're the ones with the fastest response. A hire covers one shift. Automation covers every hour.

We built this for PI firms specifically. Every inquiry — call, web form, text — answered in under 60 seconds, 24/7, pre-qualified before an attorney touches it.

Happy to show you what it looks like for {firm_name} if it's worth 15 minutes.

— AJ""",
    },
]

# ── Auto-scheduler (runs batch daily at configured time) ─────────────────────

_scheduler_thread = None
_scheduler_running = False

def _get_schedule_settings():
    try:
        c = db()
        enabled = c.execute("SELECT value FROM settings WHERE key='cold_email_auto_enabled'").fetchone()
        send_time = c.execute("SELECT value FROM settings WHERE key='cold_email_send_time'").fetchone()
        last_run = c.execute("SELECT value FROM settings WHERE key='cold_email_last_run'").fetchone()
        c.close()
        return {
            'enabled': enabled and enabled[0] == '1',
            'send_time': send_time[0] if send_time else '09:00',
            'last_run': last_run[0] if last_run else None,
        }
    except:
        return {'enabled': False, 'send_time': '09:00', 'last_run': None}

def _run_auto_batch():
    """Called by scheduler — runs a 3-5 email batch if auto-send is on, weekdays only."""
    settings = _get_schedule_settings()
    if not settings['enabled']:
        return
    # Skip weekends (Mon=0 … Sun=6)
    if datetime.now().weekday() >= 5:
        print(f"[cold-email] Skipping — weekend")
        return
    today = datetime.now().strftime('%Y-%m-%d')
    if settings['last_run'] == today:
        return  # already ran today
    try:
        _init_cold_email_queue()
        c = db()
        already_sent = set(r[0] for r in c.execute("SELECT email FROM cold_emails").fetchall())
        pending = [dict(r) for r in c.execute(
            "SELECT * FROM cold_email_queue WHERE status='pending' ORDER BY added_at ASC LIMIT 50"
        ).fetchall() if r['email'] not in already_sent]
        c.close()
        if not pending:
            return
        send_count = min(random.randint(3, 5), len(pending))
        batch = random.sample(pending, send_count) if len(pending) >= send_count else pending
        tmpl_order = list(range(len(COLD_EMAIL_TEMPLATES)))
        random.shuffle(tmpl_order)
        for i, recipient in enumerate(batch):
            tmpl = COLD_EMAIL_TEMPLATES[tmpl_order[i % len(tmpl_order)]]
            fn   = recipient.get('first_name', 'there')
            firm = recipient.get('firm_name', 'your firm')
            city = recipient.get('city', 'your city')
            subject = tmpl['subject'].format(first_name=fn, firm_name=firm, city=city)
            body    = tmpl['body'].format(first_name=fn, firm_name=firm, city=city)
            ok, _   = _cold_send_email(recipient['email'], subject, body)
            status  = 'sent' if ok else 'failed'
            c = db()
            c.execute(
                "INSERT OR IGNORE INTO cold_emails (email,first_name,firm_name,city,template_index,subject,status) VALUES (?,?,?,?,?,?,?)",
                (recipient['email'], fn, firm, city, tmpl_order[i % len(tmpl_order)], subject, status)
            )
            c.execute("UPDATE cold_email_queue SET status=? WHERE email=?", (status, recipient['email']))
            c.commit(); c.close()
            if i < len(batch) - 1:
                _time.sleep(random.randint(90, 180))
        # mark last run
        c = db()
        c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('cold_email_last_run',?)", (today,))
        c.commit(); c.close()
        print(f"[cold-email] Auto batch sent {send_count} emails on {today}")
    except Exception as e:
        print(f"[cold-email] Auto batch error: {e}")

def _scheduler_loop():
    global _scheduler_running
    while _scheduler_running:
        _schedule.run_pending()
        _time.sleep(30)

def _start_scheduler(send_time='09:00'):
    global _scheduler_thread, _scheduler_running
    _schedule.clear('cold-email')
    _schedule.every().day.at(send_time).do(_run_auto_batch).tag('cold-email')
    if _scheduler_thread is None or not _scheduler_thread.is_alive():
        _scheduler_running = True
        _scheduler_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        _scheduler_thread.start()

def _stop_scheduler():
    global _scheduler_running
    _scheduler_running = False
    _schedule.clear('cold-email')

def _cold_send_email(to_email, subject, body):
    gmail = os.getenv('GMAIL_SENDER', '')
    pwd   = os.getenv('GMAIL_PASSWORD', '')
    if not gmail or not pwd:
        return False, 'GMAIL_SENDER or GMAIL_PASSWORD not set'
    try:
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From']    = formataddr(('AJ | Velaro', gmail))
        msg['To']      = to_email
        msg['Reply-To'] = gmail
        msg['Message-ID'] = make_msgid(domain=gmail.split('@')[-1])
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as srv:
            srv.login(gmail, pwd)
            srv.sendmail(gmail, to_email, msg.as_string())
        return True, ''
    except Exception as e:
        return False, str(e)

def _init_cold_email_queue():
    c = db()
    c.execute("""
        CREATE TABLE IF NOT EXISTS cold_email_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            first_name TEXT DEFAULT 'there',
            firm_name TEXT DEFAULT 'your firm',
            city TEXT DEFAULT 'your city',
            status TEXT DEFAULT 'pending',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(email)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS cold_email_blocklist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            first_name TEXT,
            firm_name TEXT,
            reason TEXT DEFAULT 'manual',
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # add reply_type column to cold_emails if missing
    existing = {r[1] for r in c.execute("PRAGMA table_info(cold_emails)").fetchall()}
    if 'reply_type' not in existing:
        c.execute("ALTER TABLE cold_emails ADD COLUMN reply_type TEXT DEFAULT ''")
    c.commit(); c.close()

@app.route('/cold-emails')
def cold_emails_dashboard():
    _init_cold_email_queue()
    c = db()
    total_q      = c.execute("SELECT COUNT(*) FROM cold_email_queue").fetchone()[0]
    pending_q    = c.execute("SELECT COUNT(*) FROM cold_email_queue WHERE status='pending'").fetchone()[0]
    total_sent   = c.execute("SELECT COUNT(*) FROM cold_emails").fetchone()[0]
    sent_today   = c.execute("SELECT COUNT(*) FROM cold_emails WHERE DATE(sent_at)=DATE('now')").fetchone()[0]
    replied      = c.execute("SELECT COUNT(*) FROM cold_emails WHERE status='replied'").fetchone()[0]
    replied_pos  = c.execute("SELECT COUNT(*) FROM cold_emails WHERE reply_type='POSITIVE'").fetchone()[0]
    replied_neg  = c.execute("SELECT COUNT(*) FROM cold_emails WHERE reply_type='NEGATIVE'").fetchone()[0]
    failed       = c.execute("SELECT COUNT(*) FROM cold_emails WHERE status='failed'").fetchone()[0]
    blocklist    = [dict(r) for r in c.execute("SELECT * FROM cold_email_blocklist ORDER BY added_at DESC").fetchall()]
    queue = c.execute(
        "SELECT * FROM cold_email_queue ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, added_at ASC LIMIT 100"
    ).fetchall()
    sent_log = c.execute(
        "SELECT * FROM cold_emails ORDER BY sent_at DESC LIMIT 100"
    ).fetchall()
    c.close()
    sched = _get_schedule_settings()

    template_names = [t['name'] for t in COLD_EMAIL_TEMPLATES]
    gmail_sender = os.getenv('GMAIL_SENDER', 'not configured')

    # Convert stored IST send_time to approximate US Eastern time for display
    us_eastern_note = ''
    try:
        from datetime import time as dtime
        h, m = map(int, sched['send_time'].split(':'))
        # IST = UTC+5:30, US Eastern (EDT) = UTC-4 → diff = -9h30m
        total_min = h * 60 + m - 9 * 60 - 30
        total_min = total_min % (24 * 60)
        ue_h, ue_m = divmod(total_min, 60)
        us_eastern_note = f"{ue_h:02d}:{ue_m:02d} US Eastern"
    except:
        pass

    return render_template('cold_emails.html', title='Cold Emails',
        total_q=total_q, pending_q=pending_q,
        total_sent=total_sent, sent_today=sent_today,
        replied=replied, replied_pos=replied_pos, replied_neg=replied_neg, failed=failed,
        queue=queue, sent_log=sent_log, blocklist=blocklist, template_names=template_names,
        sched_enabled=sched['enabled'], sched_time=sched['send_time'], sched_last_run=sched['last_run'],
        gmail_sender=gmail_sender, us_eastern_note=us_eastern_note)

@app.route('/api/cold-emails/upload', methods=['POST'])
def cold_emails_upload():
    _init_cold_email_queue()
    f = request.files.get('file')
    if not f:
        return jsonify({'ok': False, 'error': 'No file uploaded'})
    stream = io.StringIO(f.stream.read().decode('utf-8'))
    reader = csv.DictReader(stream)
    added = skipped = 0
    c = db()
    blocklisted = set(r[0] for r in c.execute("SELECT email FROM cold_email_blocklist").fetchall())
    already_sent = set(r[0] for r in c.execute("SELECT email FROM cold_emails").fetchall())
    for row in reader:
        email = (row.get('email') or row.get('Email') or '').strip().lower()
        if not email or '@' not in email:
            continue
        if email in blocklisted or email in already_sent:
            skipped += 1
            continue
        first_name = (row.get('first_name') or row.get('First Name') or 'there').strip().split()[0]
        firm_name  = (row.get('firm_name') or row.get('Company') or 'your firm').strip()
        city       = (row.get('city') or row.get('City') or 'your city').strip()
        try:
            c.execute(
                "INSERT OR IGNORE INTO cold_email_queue (email, first_name, firm_name, city) VALUES (?,?,?,?)",
                (email, first_name, firm_name, city)
            )
            if c.execute("SELECT changes()").fetchone()[0]:
                added += 1
            else:
                skipped += 1
        except:
            skipped += 1
    c.commit(); c.close()
    return jsonify({'ok': True, 'added': added, 'skipped': skipped})

@app.route('/api/cold-emails/send', methods=['POST'])
def cold_emails_send_batch():
    _init_cold_email_queue()
    c = db()
    # get pending from queue that haven't been emailed yet
    already_sent_emails = set(
        r[0] for r in c.execute("SELECT email FROM cold_emails").fetchall()
    )
    pending = [
        dict(r) for r in c.execute(
            "SELECT * FROM cold_email_queue WHERE status='pending' ORDER BY added_at ASC LIMIT 50"
        ).fetchall()
        if r['email'] not in already_sent_emails
    ]
    c.close()

    if not pending:
        return jsonify({'ok': False, 'error': 'No pending recipients to send to'})

    send_count = min(random.randint(3, 5), len(pending))
    batch = random.sample(pending, send_count) if len(pending) >= send_count else pending

    # shuffle templates so consecutive emails use different ones
    tmpl_order = list(range(len(COLD_EMAIL_TEMPLATES)))
    random.shuffle(tmpl_order)

    results = []
    for i, recipient in enumerate(batch):
        tmpl = COLD_EMAIL_TEMPLATES[tmpl_order[i % len(tmpl_order)]]
        fn   = recipient.get('first_name', 'there')
        firm = recipient.get('firm_name', 'your firm')
        city = recipient.get('city', 'your city')

        subject = tmpl['subject'].format(first_name=fn, firm_name=firm, city=city)
        body    = tmpl['body'].format(first_name=fn, firm_name=firm, city=city)

        ok, err = _cold_send_email(recipient['email'], subject, body)
        status  = 'sent' if ok else 'failed'

        c = db()
        c.execute(
            """INSERT OR IGNORE INTO cold_emails
               (email, first_name, firm_name, city, template_index, subject, status)
               VALUES (?,?,?,?,?,?,?)""",
            (recipient['email'], fn, firm, city, tmpl_order[i % len(tmpl_order)], subject, status)
        )
        c.execute(
            "UPDATE cold_email_queue SET status=? WHERE email=?",
            (status, recipient['email'])
        )
        c.commit(); c.close()

        results.append({'email': recipient['email'], 'status': status, 'subject': subject, 'error': err})

        if i < len(batch) - 1:
            delay = random.randint(90, 180)
            _time.sleep(delay)

    sent_ok = sum(1 for r in results if r['status'] == 'sent')
    return jsonify({'ok': True, 'sent': sent_ok, 'total': send_count, 'results': results})

@app.route('/api/cold-emails/mark-replied', methods=['POST'])
def cold_emails_mark_replied():
    d = request.get_json() or {}
    email      = d.get('email', '').strip().lower()
    reply_type = d.get('reply_type', 'REPLIED').upper()  # POSITIVE / NEGATIVE / NOT_NOW
    c = db()
    c.execute("UPDATE cold_emails SET status='replied', reply_type=? WHERE email=?", (reply_type, email))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/cold-emails/blocklist', methods=['POST'])
def cold_emails_blocklist():
    """Add an email to blocklist and remove from queue."""
    d = request.get_json() or {}
    email      = d.get('email', '').strip().lower()
    first_name = d.get('first_name', '')
    firm_name  = d.get('firm_name', '')
    reason     = d.get('reason', 'manual')
    if not email:
        return jsonify({'ok': False, 'error': 'No email'})
    c = db()
    c.execute(
        "INSERT OR IGNORE INTO cold_email_blocklist (email, first_name, firm_name, reason) VALUES (?,?,?,?)",
        (email, first_name, firm_name, reason)
    )
    c.execute("DELETE FROM cold_email_queue WHERE email=?", (email,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/cold-emails/manual-log', methods=['POST'])
def cold_emails_manual_log():
    """Log an email that was sent manually outside the system."""
    d = request.get_json() or {}
    email      = d.get('email', '').strip().lower()
    first_name = d.get('first_name', 'there')
    firm_name  = d.get('firm_name', '')
    reply_type = d.get('reply_type', '').upper()
    subject    = d.get('subject', '(manual)')
    if not email:
        return jsonify({'ok': False, 'error': 'No email'})
    status = 'replied' if reply_type else 'sent'
    c = db()
    c.execute(
        "INSERT OR REPLACE INTO cold_emails (email,first_name,firm_name,city,template_index,subject,status,reply_type) VALUES (?,?,?,?,?,?,?,?)",
        (email, first_name, firm_name, '', -1, subject, status, reply_type)
    )
    # also blocklist if negative
    if reply_type == 'NEGATIVE':
        c.execute(
            "INSERT OR IGNORE INTO cold_email_blocklist (email,first_name,firm_name,reason) VALUES (?,?,?,?)",
            (email, first_name, firm_name, 'replied_negative')
        )
        c.execute("DELETE FROM cold_email_queue WHERE email=?", (email,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/cold-emails/delete-queue', methods=['POST'])
def cold_emails_delete_queue():
    eid = int((request.get_json() or {}).get('id', 0))
    c = db()
    c.execute("DELETE FROM cold_email_queue WHERE id=?", (eid,))
    c.commit(); c.close()
    return jsonify({'ok': True})

@app.route('/api/cold-emails/schedule', methods=['POST'])
def cold_emails_schedule():
    d         = request.get_json() or {}
    enabled   = d.get('enabled', False)
    send_time = d.get('send_time', '09:00')
    c = db()
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('cold_email_auto_enabled',?)", ('1' if enabled else '0',))
    c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES ('cold_email_send_time',?)", (send_time,))
    c.commit(); c.close()
    if enabled:
        _start_scheduler(send_time)
    else:
        _stop_scheduler()
    return jsonify({'ok': True, 'enabled': enabled, 'send_time': send_time})

# ── VAPI Auto-Caller Scheduler ───────────────────────────────────────────────
# 3 sessions/day during US business hours:
#   Session 1: 18:30 IST = 09:00 US Eastern
#   Session 1: 18:30 IST =  9:00am EDT /  8:00am CDT
#   Session 2: 20:30 IST = 11:00am EDT / 10:00am CDT /  8:00am PDT
#   Session 3: 22:30 IST =  1:00pm EDT / 12:00pm CDT / 10:00am PDT
#   Session 4: 00:30 IST =  3:00pm EDT /  2:00pm CDT / 12:00pm PDT
CALL_SESSIONS_IST = ['19:30', '21:30', '23:30', '01:30']

_call_scheduler_running = False
_call_scheduler_thread  = None

def _run_call_session():
    """Runs vapi_caller.py as a subprocess — one session."""
    caller_path = os.path.join(os.path.dirname(__file__), 'vapi_caller.py')
    if not os.path.exists(caller_path):
        print('[auto-caller] vapi_caller.py not found')
        return
    import subprocess as _sp
    print(f'[auto-caller] Starting call session at {datetime.now().strftime("%H:%M IST")}')
    try:
        result = _sp.run(
            ['python3', caller_path],
            cwd=os.path.dirname(__file__),
            capture_output=True, text=True, timeout=3600
        )
        print(f'[auto-caller] Session done.\n{result.stdout[-500:]}')
        if result.returncode != 0:
            print(f'[auto-caller] STDERR: {result.stderr[-200:]}')
    except Exception as e:
        print(f'[auto-caller] Error: {e}')

def _call_scheduler_loop():
    global _call_scheduler_running
    while _call_scheduler_running:
        _schedule.run_pending()
        _time.sleep(30)

def _start_call_scheduler():
    global _call_scheduler_thread, _call_scheduler_running
    _schedule.clear('auto-call')
    for t in CALL_SESSIONS_IST:
        _schedule.every().day.at(t).do(_run_call_session).tag('auto-call')
    if _call_scheduler_thread is None or not _call_scheduler_thread.is_alive():
        _call_scheduler_running = True
        _call_scheduler_thread = threading.Thread(target=_call_scheduler_loop, daemon=True)
        _call_scheduler_thread.start()
    print(f'  [auto-caller] {len(CALL_SESSIONS_IST)} sessions scheduled: {", ".join(CALL_SESSIONS_IST)} IST')

# ── Retell Webhook ────────────────────────────────────────────
@app.route('/webhook/retell', methods=['POST'])
def retell_webhook():
    """
    Retell POSTs here immediately when a call ends.
    Parses outcome, updates DB, fires follow-up email.
    Set this URL in Retell → Settings → Webhook.
    """
    import hmac, hashlib
    data = request.get_json(silent=True) or {}

    # Optional signature verification
    secret = os.getenv("RETELL_WEBHOOK_SECRET", "")
    if secret:
        sig  = request.headers.get("X-Retell-Signature", "")
        body = request.get_data()
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return jsonify({"error": "invalid signature"}), 401

    event = data.get("event", "")
    if event not in ("call_ended", "call_analyzed"):
        return jsonify({"ok": True}), 200

    call   = data.get("call", {})
    call_id = call.get("call_id", "")
    if not call_id:
        return jsonify({"ok": True}), 200

    # Pull stored job info from vapi_calls using call_id
    conn = db()
    row  = conn.execute(
        "SELECT * FROM vapi_calls WHERE vapi_call_id = ? ORDER BY id DESC LIMIT 1",
        (call_id,)
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"ok": True}), 200

    # Parse result from webhook payload
    try:
        from vapi_caller import parse_call_result, next_action_for_outcome, _detect_gatekeeper_status
        result = parse_call_result(call)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    outcome        = result.get("outcome", "NO_ANSWER")
    summary        = result.get("notes", "")
    gk_status      = result.get("gatekeeper_status", "UNKNOWN")
    email_collected = result.get("email_collected", "")
    next_action, next_date = next_action_for_outcome(outcome, row["attempt_number"] or 1)

    # Update the call record with final outcome
    conn = db()
    conn.execute("""
        UPDATE vapi_calls SET
            outcome = ?, notes = ?, gatekeeper_status = ?,
            email_collected = ?, next_action = ?, next_action_date = ?
        WHERE vapi_call_id = ?
    """, (outcome, summary, gk_status, email_collected, next_action, next_date, call_id))

    # Update lead stage
    if outcome == "NOT_INTERESTED":
        conn.execute("UPDATE vapi_leads SET do_not_call = 1 WHERE name = ? AND firm_name = ?",
                     (row["lead_name"], row["firm_name"]))
    elif outcome in ("HOT", "WARM"):
        conn.execute("UPDATE leads SET pipeline_stage = ? WHERE name = ? AND company = ?",
                     (outcome.lower(), row["lead_name"], row["firm_name"]))

    conn.commit()
    conn.close()

    # Fire follow-up email immediately for HOT/WARM
    if outcome in ("HOT", "WARM") and email_collected:
        subprocess.Popen([
            "python3",
            os.path.join(os.path.dirname(__file__), "send_followup_email.py"),
            outcome, row["lead_name"], row["firm_name"],
            email_collected,
            result.get("best_day", ""),
            result.get("best_time", ""),
        ])

    return jsonify({"ok": True, "outcome": outcome}), 200


# ── Webhook status page ────────────────────────────────────────
@app.route('/webhook/status')
def webhook_status():
    return jsonify({
        "status": "live",
        "endpoint": "/webhook/retell",
        "method": "POST"
    })


if __name__=='__main__':
    init_db()
    # Resume email scheduler
    s = _get_schedule_settings()
    if s['enabled']:
        _start_scheduler(s['send_time'])
        print(f"  [cold-email] Auto-send ON — daily at {s['send_time']} IST")
    # Always start call scheduler
    _start_call_scheduler()
    print('\n  Velaro OS → http://localhost:5001\n')
    port = int(os.environ.get("PORT", 5001))
    app.run(debug=False, port=port, host='0.0.0.0')

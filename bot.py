import os
import re
import json
import sqlite3
from datetime import datetime, date, timedelta

import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.background import BackgroundScheduler

# --------------------
# ENV
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # group chat id like "-5144431873"

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable")

bot = telebot.TeleBot(BOT_TOKEN)

# --------------------
# DB SETUP
# --------------------
db = sqlite3.connect("chores.db", check_same_thread=False)
cur = db.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS household (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  person1 TEXT,
  person2 TEXT,
  rotate_index INTEGER DEFAULT 0
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS chores (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  category TEXT,                    -- e.g. 'cat', 'dailycleaning'
  mode TEXT NOT NULL,               -- fixed/rotate (rotate only used at creation to pick initial assignee)
  assignee TEXT NOT NULL,           -- responsibility owner (stays same even if other completes)
  interval_days INTEGER NOT NULL,   -- rolling repeat interval
  start_date TEXT,                  -- YYYY-MM-DD
  time TEXT NOT NULL,               -- HH:MM
  last_done TEXT,                   -- YYYY-MM-DD
  last_reminded TEXT,               -- YYYY-MM-DD
  skip_until TEXT                   -- YYYY-MM-DD
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS completions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chore_id INTEGER NOT NULL,
  chore_name TEXT NOT NULL,
  assigned_to TEXT NOT NULL,
  completed_by TEXT NOT NULL,
  completed_on TEXT NOT NULL,       -- YYYY-MM-DD
  completed_at TEXT NOT NULL        -- ISO datetime string
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (chat_id, user_id)
)
""")

db.commit()

def ensure_household_row():
    cur.execute("SELECT id FROM household WHERE id=1")
    if not cur.fetchone():
        cur.execute("INSERT INTO household (id, person1, person2, rotate_index) VALUES (1, NULL, NULL, 0)")
        db.commit()

def add_column_if_missing(table, column, coltype="TEXT"):
    cur.execute(f"PRAGMA table_info({table})")
    cols = [r[1] for r in cur.fetchall()]
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        db.commit()

ensure_household_row()
# safe migrations (won't hurt if already present)
add_column_if_missing("chores", "category", "TEXT")
add_column_if_missing("chores", "start_date", "TEXT")

# --------------------
# HELPERS
# --------------------
TIME_RE = re.compile(r"^\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")

VALID_CATEGORIES = [
    "cat",
    "dailycleaning",
    "deepcleaning",
    "kitchen",
    "laundry",
    "maintenance",
    "admin",
]

CATEGORY_LABELS = {
    "cat": "🐱 Cat",
    "dailycleaning": "🧹 Daily Cleaning",
    "deepcleaning": "🧽 Deep Cleaning",
    "kitchen": "🍳 Kitchen",
    "laundry": "🧺 Laundry",
    "maintenance": "🛠️ Maintenance",
    "admin": "📋 Admin",
}

def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")

def parse_hhmm(s: str) -> str:
    s = s.strip()
    if not TIME_RE.match(s):
        raise ValueError("Time must be HH:MM (24h), e.g., 21:00")
    hh, mm = s.split(":")
    hh_i, mm_i = int(hh), int(mm)
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        raise ValueError("Invalid time")
    return f"{hh_i:02d}:{mm_i:02d}"

def parse_ddmmyyyy(s: str) -> str:
    """
    Input:  DD-MM-YYYY
    Output: YYYY-MM-DD (store + compute safely)
    """
    s = s.strip()
    if not DATE_RE.match(s):
        raise ValueError("Date must be DD-MM-YYYY (e.g., 05-01-2026)")
    dt = datetime.strptime(s, "%d-%m-%Y").date()
    return dt.strftime("%Y-%m-%d")

def format_ddmmyyyy(iso_yyyy_mm_dd: str) -> str:
    dt = datetime.strptime(iso_yyyy_mm_dd, "%Y-%m-%d").date()
    return dt.strftime("%d-%m-%Y")

def get_people():
    cur.execute("SELECT person1, person2, rotate_index FROM household WHERE id=1")
    p1, p2, idx = cur.fetchone()
    return p1, p2, idx

def set_people(p1: str, p2: str):
    cur.execute("UPDATE household SET person1=?, person2=?, rotate_index=0 WHERE id=1", (p1, p2))
    db.commit()

def next_rotate_person(advance: bool = True) -> str:
    p1, p2, idx = get_people()
    if not p1 or not p2:
        return "rotate"
    person = p1 if (idx % 2 == 0) else p2
    if advance:
        cur.execute("UPDATE household SET rotate_index=? WHERE id=1", (idx + 1,))
        db.commit()
    return person

def load_session(chat_id: int, user_id: int):
    cur.execute("SELECT step, data FROM sessions WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    row = cur.fetchone()
    if not row:
        return None
    step, data = row
    return step, json.loads(data)

def save_session(chat_id: int, user_id: int, step: str, data: dict):
    cur.execute(
        "INSERT INTO sessions (chat_id, user_id, step, data) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET step=excluded.step, data=excluded.data",
        (chat_id, user_id, step, json.dumps(data))
    )
    db.commit()

def clear_session(chat_id: int, user_id: int):
    cur.execute("DELETE FROM sessions WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    db.commit()

def next_due_date(start_date: str | None, last_done: str | None, interval_days: int) -> date:
    """
    Rolling repeats.
    If last_done exists -> last_done + interval_days
    Else -> start_date (or today if missing)
    """
    if last_done:
        base = datetime.strptime(last_done, "%Y-%m-%d").date()
        return base + timedelta(days=int(interval_days))
    if start_date:
        return datetime.strptime(start_date, "%Y-%m-%d").date()
    return date.today()

def days_until_due(chore_row) -> int:
    """
    chore_row:
    (id, name, category, mode, assignee, interval_days, start_date, time, last_done, last_reminded, skip_until)
    """
    interval_days = int(chore_row[5])
    start_date = chore_row[6]
    last_done = chore_row[8]
    due_dt = next_due_date(start_date, last_done, interval_days)
    return (due_dt - date.today()).days

def chore_is_due(chore_row) -> bool:
    skip_until = chore_row[10]
    if skip_until == today_str():
        return False
    return days_until_due(chore_row) <= 0

def due_string(chore_row) -> str:
    interval_days = int(chore_row[5])
    start_date = chore_row[6]
    time_str = chore_row[7]
    last_done = chore_row[8]

    due_dt = next_due_date(start_date, last_done, interval_days)
    due_iso = due_dt.strftime("%Y-%m-%d")
    dleft = (due_dt - date.today()).days

    if dleft == 0:
        return f"due today ({time_str})"
    return f"due {format_ddmmyyyy(due_iso)} ({time_str})"

def reminder_keyboard(chore_id: int):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("Done ✅", callback_data=f"done:{chore_id}:self"),
        InlineKeyboardButton("Done by other 👥", callback_data=f"done_other:{chore_id}")
    )
    kb.row(InlineKeyboardButton("Skip ⏭️", callback_data=f"skip:{chore_id}"))
    return kb

def done_other_keyboard(chore_id: int):
    p1, p2, _ = get_people()
    kb = InlineKeyboardMarkup()
    if p1 and p2:
        kb.row(
            InlineKeyboardButton(p1, callback_data=f"done:{chore_id}:{p1}"),
            InlineKeyboardButton(p2, callback_data=f"done:{chore_id}:{p2}")
        )
    kb.row(InlineKeyboardButton("Cancel", callback_data=f"cancel_other:{chore_id}"))
    return kb

def send_to_household(text: str, reply_markup=None):
    if CHAT_ID:
        bot.send_message(int(CHAT_ID), text, reply_markup=reply_markup)

def record_completion(chore_id: int, completed_by: str):
    # includes category/start_date in SELECT for consistency (though not required here)
    cur.execute("""
        SELECT id, name, category, mode, assignee, interval_days, start_date, time, last_done, last_reminded, skip_until
        FROM chores WHERE id=?
    """, (chore_id,))
    c = cur.fetchone()
    if not c:
        return False, "Chore not found."

    _, name, category, mode, assigned_to, interval_days, start_date, time_str, last_done, last_reminded, skip_until = c
    today = today_str()
    now_iso = datetime.now().isoformat()

    # mark done: keep assignment the same (your rule)
    cur.execute("""
        UPDATE chores
        SET last_done=?, skip_until=NULL, last_reminded=NULL
        WHERE id=?
    """, (today, chore_id))

    # log who did it
    cur.execute("""
        INSERT INTO completions (chore_id, chore_name, assigned_to, completed_by, completed_on, completed_at)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (chore_id, name, assigned_to, completed_by, today, now_iso))

    db.commit()
    return True, (name, assigned_to, completed_by, interval_days)

# --------------------
# DAILY DIGEST (8am SGT = 00:00 UTC)
# --------------------
def daily_digest_message():
    cur.execute("""
        SELECT id, name, category, mode, assignee, interval_days, start_date, time, last_done, last_reminded, skip_until
        FROM chores
    """)
    chores = cur.fetchall()

    due_today = []
    due_next_3 = []

    for c in chores:
        # ignore chores skipped today
        if c[10] == today_str():
            continue
        d = days_until_due(c)
        if d == 0:
            due_today.append(c)
        elif 1 <= d <= 3:
            due_next_3.append((c, d))

    lines = ["☀️ Daily Chore Update\n"]

    if due_today:
        lines.append("📌 Due today:")
        for c in due_today:
            label = CATEGORY_LABELS.get(c[2] or "admin", c[2] or "admin")
            lines.append(f"• {c[1]} — {c[4]} ({label}) @ {c[7]}")
    else:
        lines.append("📌 Due today: None 🎉")

    if due_next_3:
        lines.append("\n🔜 Due in next 3 days:")
        for c, d in due_next_3:
            label = CATEGORY_LABELS.get(c[2] or "admin", c[2] or "admin")
            lines.append(f"• {c[1]} — {c[4]} ({label}) in {d}d")
    else:
        lines.append("\n🔜 Due in next 3 days: None")

    return "\n".join(lines)

def daily_digest_job():
    if not CHAT_ID:
        return
    send_to_household(daily_digest_message())

# --------------------
# REMINDERS
# --------------------
def reminder_job():
    now = datetime.now().strftime("%H:%M")
    tday = today_str()

    cur.execute("""
        SELECT id, name, category, mode, assignee, interval_days, start_date, time, last_done, last_reminded, skip_until
        FROM chores
    """)
    chores = cur.fetchall()

    for c in chores:
        chore_id, name, category, mode, assignee, interval_days, start_date, time_str, last_done, last_reminded, skip_until = c

        if time_str != now:
            continue
        if last_reminded == tday:
            continue
        if not chore_is_due(c):
            continue

        label = CATEGORY_LABELS.get(category or "admin", category or "admin")
        text = (
            f"🔔 Chore due: {name}\n"
            f"Category: {label}\n"
            f"Assigned to: {assignee}\n"
            f"({due_string(c)})\n"
            f"Mark done: /done {chore_id}  (or use buttons)"
        )
        send_to_household(text, reply_markup=reminder_keyboard(chore_id))

        cur.execute("UPDATE chores SET last_reminded=? WHERE id=?", (tday, chore_id))
        db.commit()

scheduler = BackgroundScheduler()
scheduler.add_job(reminder_job, "interval", minutes=1)
# 8am SGT = 00:00 UTC
scheduler.add_job(daily_digest_job, trigger="cron", hour=0, minute=0)
scheduler.start()

# --------------------
# COMMANDS
# --------------------
@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    bot.reply_to(
        message,
        "👋 Household Chore Bot\n\n"
        "Setup:\n"
        "/setpeople Wife Husband\n\n"
        "Chores:\n"
        "/add (wizard)\n"
        "/cancel\n"
        "/list [category]\n"
        "/today\n"
        "/done <id> [who]\n"
        "/skip <id>\n"
        "/remove <id>\n\n"
        "Review:\n"
        "/history [days|name|person]\n"
        "/summary [days]\n"
        "/stats\n"
    )

@bot.message_handler(commands=["setpeople"])
def cmd_setpeople(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Usage: /setpeople Person1 Person2\nExample: /setpeople Wife Husband")
        return
    p1, p2 = parts[1].strip(), parts[2].strip()
    set_people(p1, p2)
    bot.reply_to(message, f"✅ Household people set:\n1) {p1}\n2) {p2}")

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    clear_session(message.chat.id, message.from_user.id)
    bot.reply_to(message, "❎ Cancelled.")

@bot.message_handler(commands=["add"])
def cmd_add(message):
    clear_session(message.chat.id, message.from_user.id)
    save_session(message.chat.id, message.from_user.id, "ASK_NAME", {})
    bot.reply_to(message, "🧹 Add chore (step 1/6)\nWhat is the chore name?\nExample: mopping")

@bot.message_handler(commands=["list"])
def cmd_list(message):
    # /list [category]
    parts_cmd = message.text.split(maxsplit=1)
    category_filter = None
    if len(parts_cmd) == 2:
        category_filter = parts_cmd[1].strip().lower()
        if category_filter not in VALID_CATEGORIES:
            bot.reply_to(
                message,
                "❌ Unknown category.\n"
                f"Use one of: {', '.join(VALID_CATEGORIES)}\n"
                "Example: /list cat"
            )
            return

    cur.execute("""
        SELECT id, name, category, mode, assignee, interval_days, start_date, time, last_done, last_reminded, skip_until
        FROM chores
        ORDER BY id
    """)
    chores = cur.fetchall()

    if category_filter:
        chores = [c for c in chores if (c[2] or "admin") == category_filter]

    if not chores:
        bot.reply_to(message, "No chores found for that view. Use /add to create one.")
        return

    overdue, d03, d47, d814, later = [], [], [], [], []
    for c in chores:
        d = days_until_due(c)
        if chore_is_due(c) and d < 0:
            overdue.append(c)
        elif d <= 3:
            d03.append(c)
        elif d <= 7:
            d47.append(c)
        elif d <= 14:
            d814.append(c)
        else:
            later.append(c)

    def render_bucket(title, items):
        if not items:
            return None

        # due -> category -> who
        grouped = {}
        for c in items:
            cat = (c[2] or "admin").strip()
            who = c[4]
            grouped.setdefault(cat, {}).setdefault(who, []).append(c)

        lines = [f"{title} ({len(items)})"]
        for cat in sorted(grouped.keys()):
            lines.append(f"  {CATEGORY_LABELS.get(cat, cat)} ({sum(len(v) for v in grouped[cat].values())})")
            for who in sorted(grouped[cat].keys()):
                lines.append(f"    👤 {who} ({len(grouped[cat][who])})")
                for c in grouped[cat][who]:
                    lines.append(f"      {c[0]}) {c[1]} — {due_string(c)}")
        return "\n".join(lines)

    blocks = [
        render_bucket("⛔ Overdue", overdue),
        render_bucket("🟠 Due in 0–3 days", d03),
        render_bucket("🟡 Due in 4–7 days", d47),
        render_bucket("🟢 Due in 8–14 days", d814),
        render_bucket("🔵 Due later", later),
    ]

    output = "\n\n".join(b for b in blocks if b)
    bot.reply_to(message, output)

@bot.message_handler(commands=["today"])
def cmd_today(message):
    """
    Viewer friendly + actionable:
    - sends one compact summary message
    - then sends a message per due chore with buttons (Done/Other/Skip)
    """
    cur.execute("""
        SELECT id, name, category, mode, assignee, interval_days, start_date, time, last_done, last_reminded, skip_until
        FROM chores
        ORDER BY id
    """)
    chores = cur.fetchall()
    due = [c for c in chores if chore_is_due(c)]

    if not due:
        bot.reply_to(message, "✅ No chores due right now.")
        return

    # summary header
    bot.reply_to(message, f"📅 Due / overdue now: {len(due)} chore(s). Sending details with buttons…")

    # detail per chore (less clutter than one giant keyboard-less list; each has buttons)
    for c in due:
        chore_id = c[0]
        name = c[1]
        cat = CATEGORY_LABELS.get(c[2] or "admin", c[2] or "admin")
        who = c[4]
        dleft = days_until_due(c)
        status = "⛔ Overdue" if dleft < 0 else "📌 Due today"
        text = (
            f"{status}\n"
            f"{chore_id}) {name}\n"
            f"{cat}\n"
            f"👤 {who}\n"
            f"{due_string(c)}"
        )
        bot.send_message(message.chat.id, text, reply_markup=reminder_keyboard(chore_id))

@bot.message_handler(commands=["done"])
def cmd_done(message):
    # /done <id> [who]
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /done <id> [who]\nExamples:\n/done 3\n/done 3 wife")
        return

    chore_id = int(parts[1])
    completed_by = parts[2].strip() if len(parts) == 3 else message.from_user.first_name

    ok, result = record_completion(chore_id, completed_by)
    if not ok:
        bot.reply_to(message, f"❌ {result}")
        return

    name, assigned_to, completed_by, interval_days = result
    if completed_by.lower() != assigned_to.lower():
        bot.reply_to(
            message,
            f"✅ {name} marked done.\nAssigned to: {assigned_to}\nCompleted by: {completed_by}\nNext due in {interval_days} day(s)."
        )
    else:
        bot.reply_to(message, f"🎉 {name} marked done! Next due in {interval_days} day(s).")

@bot.message_handler(commands=["skip"])
def cmd_skip(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /skip <id>\nExample: /skip 2")
        return
    chore_id = int(parts[1])
    cur.execute("SELECT id FROM chores WHERE id=?", (chore_id,))
    if not cur.fetchone():
        bot.reply_to(message, f"Chore #{chore_id} not found.")
        return
    cur.execute("UPDATE chores SET skip_until=? WHERE id=?", (today_str(), chore_id))
    db.commit()
    bot.reply_to(message, "⏭️ Skipped for today.")

@bot.message_handler(commands=["remove"])
def cmd_remove(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /remove <id>\nExample: /remove 2")
        return
    chore_id = int(parts[1])
    cur.execute("DELETE FROM chores WHERE id=?", (chore_id,))
    db.commit()
    bot.reply_to(message, "🗑️ Removed.")

@bot.message_handler(commands=["history"])
def cmd_history(message):
    arg = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1)) == 2 else ""
    where = []
    params = []

    if arg:
        if arg.isdigit():
            days = int(arg)
            since = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")
            where.append("completed_on >= ?")
            params.append(since)
        else:
            where.append("(LOWER(completed_by) LIKE ? OR LOWER(chore_name) LIKE ? OR LOWER(assigned_to) LIKE ?)")
            like = f"%{arg.lower()}%"
            params.extend([like, like, like])

    sql = """
      SELECT chore_name, assigned_to, completed_by, completed_on
      FROM completions
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY completed_at DESC LIMIT 50"

    cur.execute(sql, params)
    rows = cur.fetchall()

    if not rows:
        bot.reply_to(message, "📜 No matching history yet.")
        return

    lines = ["📜 History (most recent first):\n"]
    for name, assigned_to, completed_by, completed_on in rows:
        when = format_ddmmyyyy(completed_on)
        if completed_by.lower() != assigned_to.lower():
            lines.append(f"• {name} — assigned to {assigned_to}, completed by {completed_by} on {when}")
        else:
            lines.append(f"• {name} — {completed_by} on {when}")

    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["summary"])
def cmd_summary(message):
    days = 7
    parts = message.text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].strip().isdigit():
        days = int(parts[1].strip())

    since = (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")

    cur.execute("""
        SELECT completed_by, COUNT(*)
        FROM completions
        WHERE completed_on >= ?
        GROUP BY completed_by
        ORDER BY COUNT(*) DESC
    """, (since,))
    by_doer = cur.fetchall()

    cur.execute("""
        SELECT assigned_to, completed_by, COUNT(*)
        FROM completions
        WHERE completed_on >= ?
          AND LOWER(assigned_to) != LOWER(completed_by)
        GROUP BY assigned_to, completed_by
        ORDER BY COUNT(*) DESC
    """, (since,))
    covers = cur.fetchall()

    lines = [f"📈 Summary (last {days} days):\n"]
    if by_doer:
        lines.append("✅ Completed (by who did it):")
        for who, cnt in by_doer:
            lines.append(f"• {who}: {cnt}")
    else:
        lines.append("No completions in this period.")

    if covers:
        lines.append("\n🤝 Covers (assigned → completed by):")
        for assigned_to, completed_by, cnt in covers:
            lines.append(f"• {assigned_to} → {completed_by}: {cnt}")

    bot.reply_to(message, "\n".join(lines))

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    cur.execute("""
        SELECT completed_by, COUNT(*)
        FROM completions
        GROUP BY completed_by
        ORDER BY COUNT(*) DESC
    """)
    by_doer = cur.fetchall()

    cur.execute("""
        SELECT assigned_to, COUNT(*)
        FROM completions
        GROUP BY assigned_to
        ORDER BY COUNT(*) DESC
    """)
    by_assigned = cur.fetchall()

    cur.execute("""
        SELECT COUNT(*)
        FROM completions
        WHERE LOWER(assigned_to) != LOWER(completed_by)
    """)
    cover_count = cur.fetchone()[0]

    lines = ["📊 Lifetime stats:\n"]
    if by_doer:
        lines.append("✅ Completed (who did it):")
        for who, cnt in by_doer:
            lines.append(f"• {who}: {cnt}")

    if by_assigned:
        lines.append("\n🎯 Responsibility (assigned to):")
        for who, cnt in by_assigned:
            lines.append(f"• {who}: {cnt}")

    lines.append(f"\n🤝 Total covers: {cover_count}")
    bot.reply_to(message, "\n".join(lines))

# --------------------
# INLINE BUTTON HANDLERS
# --------------------
@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    try:
        data = call.data or ""

        if data.startswith("done_other:"):
            chore_id = int(data.split(":")[1])
            bot.answer_callback_query(call.id)
            bot.send_message(call.message.chat.id, "Who completed it?", reply_markup=done_other_keyboard(chore_id))
            return

        if data.startswith("cancel_other:"):
            bot.answer_callback_query(call.id, "Cancelled")
            return

        if data.startswith("skip:"):
            chore_id = int(data.split(":")[1])
            cur.execute("UPDATE chores SET skip_until=? WHERE id=?", (today_str(), chore_id))
            db.commit()
            bot.answer_callback_query(call.id, "Skipped")
            bot.send_message(call.message.chat.id, f"⏭️ Skipped chore #{chore_id} for today.")
            return

        if data.startswith("done:"):
            _, chore_id_s, who = data.split(":", 2)
            chore_id = int(chore_id_s)
            completed_by = call.from_user.first_name if who == "self" else who

            ok, result = record_completion(chore_id, completed_by)
            if not ok:
                bot.answer_callback_query(call.id, "Error")
                bot.send_message(call.message.chat.id, f"❌ {result}")
                return

            name, assigned_to, completed_by, interval_days = result
            bot.answer_callback_query(call.id, "Marked done")

            if completed_by.lower() != assigned_to.lower():
                bot.send_message(
                    call.message.chat.id,
                    f"✅ {name} marked done.\nAssigned to: {assigned_to}\nCompleted by: {completed_by}\nNext due in {interval_days} day(s)."
                )
            else:
                bot.send_message(call.message.chat.id, f"🎉 {name} marked done! Next due in {interval_days} day(s).")
            return

        bot.answer_callback_query(call.id)

    except Exception as e:
        bot.answer_callback_query(call.id, "Error")
        bot.send_message(call.message.chat.id, f"❌ Callback error: {e}")

# --------------------
# WIZARD (session-driven)
# --------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def wizard_handler(message):
    if message.text.startswith("/"):
        return

    sess = load_session(message.chat.id, message.from_user.id)
    if not sess:
        return

    step, data = sess
    text = message.text.strip()

    try:
        if step == "ASK_NAME":
            if len(text) < 2:
                bot.reply_to(message, "Please enter a valid chore name (e.g., mopping).")
                return
            data["name"] = text
            save_session(message.chat.id, message.from_user.id, "ASK_ASSIGNEE", data)

            p1, p2, _ = get_people()
            if p1 and p2:
                bot.reply_to(message, f"Step 2/6: Who is it assigned to?\nReply: {p1} / {p2} / rotate")
            else:
                bot.reply_to(message, "Step 2/6: Who is it assigned to?\nReply with a name or 'rotate'. (Tip: /setpeople first)")

        elif step == "ASK_ASSIGNEE":
            if text.lower() == "rotate":
                data["mode"] = "rotate"
                data["assignee"] = next_rotate_person(advance=True)
            else:
                data["mode"] = "fixed"
                data["assignee"] = text

            save_session(message.chat.id, message.from_user.id, "ASK_CATEGORY", data)
            bot.reply_to(
                message,
                "Step 3/6: Category?\n"
                f"Choose one: {', '.join(VALID_CATEGORIES)}"
            )

        elif step == "ASK_CATEGORY":
            cat = text.strip().lower()
            if cat not in VALID_CATEGORIES:
                bot.reply_to(message, f"❌ Invalid category.\nChoose one: {', '.join(VALID_CATEGORIES)}")
                return
            data["category"] = cat
            save_session(message.chat.id, message.from_user.id, "ASK_INTERVAL", data)
            bot.reply_to(message, "Step 4/6: Repeat every how many days?\nReply with a number (e.g., 7)")

        elif step == "ASK_INTERVAL":
            if not text.isdigit():
                bot.reply_to(message, "Please reply with a number of days (e.g., 7).")
                return
            interval_days = int(text)
            if interval_days < 1 or interval_days > 365:
                bot.reply_to(message, "Please choose an interval from 1–365.")
                return
            data["interval_days"] = interval_days

            save_session(message.chat.id, message.from_user.id, "ASK_START_DATE", data)
            bot.reply_to(
                message,
                "Step 5/6: When should this chore start?\n"
                "Reply DD-MM-YYYY (e.g., 05-01-2026) or type: today"
            )

        elif step == "ASK_START_DATE":
            if text.lower() == "today":
                data["start_date"] = today_str()
            else:
                data["start_date"] = parse_ddmmyyyy(text)

            save_session(message.chat.id, message.from_user.id, "ASK_TIME", data)
            bot.reply_to(message, "Step 6/6: Reminder time? Reply HH:MM (24h), e.g., 21:00")

        elif step == "ASK_TIME":
            time_str = parse_hhmm(text)
            data["time"] = time_str

            cur.execute(
                """
                INSERT INTO chores (
                    name, category, mode, assignee,
                    interval_days, start_date, time,
                    last_done, last_reminded, skip_until
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    data["name"],
                    data.get("category"),
                    data["mode"],
                    data["assignee"],
                    int(data["interval_days"]),
                    data.get("start_date"),
                    data["time"],
                )
            )
            db.commit()
            clear_session(message.chat.id, message.from_user.id)

            label = CATEGORY_LABELS.get(data.get("category") or "admin", data.get("category") or "admin")
            bot.reply_to(
                message,
                "✅ Chore added!\n"
                f"Name: {data['name']}\n"
                f"Category: {label}\n"
                f"Assigned to: {data['assignee']} ({data['mode']})\n"
                f"Repeat: every {data['interval_days']} day(s)\n"
                f"Starts: {format_ddmmyyyy(data['start_date'])}\n"
                f"Reminder: {data['time']}\n\n"
                "Use /list to view chores."
            )

        else:
            clear_session(message.chat.id, message.from_user.id)
            bot.reply_to(message, "Session reset. Use /add to start again.")

    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}\nType /cancel then /add to try again.")

# --------------------
# START
# --------------------
bot.infinity_polling()

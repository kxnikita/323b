import os
import re
import json
import sqlite3
from datetime import datetime, date, timedelta
import telebot
from apscheduler.schedulers.background import BackgroundScheduler

# --------------------
# ENV
# --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")  # Your group chat id as a string like "-100123..."

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
  mode TEXT NOT NULL,              -- "fixed" or "rotate"
  assignee TEXT NOT NULL,          -- current assignee (resolved person name)
  interval_days INTEGER NOT NULL,  -- rolling repeat interval
  time TEXT NOT NULL,              -- HH:MM
  last_done TEXT,                  -- YYYY-MM-DD
  last_reminded TEXT,              -- YYYY-MM-DD (avoid spamming)
  skip_until TEXT                  -- YYYY-MM-DD (skip for today)
)
""")

cur.execute("""
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER NOT NULL,
  user_id INTEGER NOT NULL,
  step TEXT NOT NULL,
  data TEXT NOT NULL,              -- JSON blob as TEXT
  PRIMARY KEY (chat_id, user_id)
)
""")

db.commit()

def ensure_household_row():
    cur.execute("SELECT id FROM household WHERE id=1")
    if not cur.fetchone():
        cur.execute("INSERT INTO household (id, person1, person2, rotate_index) VALUES (1, NULL, NULL, 0)")
        db.commit()

ensure_household_row()

# --------------------
# HELPERS
# --------------------
TIME_RE = re.compile(r"^\d{2}:\d{2}$")

def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")

def parse_hhmm(s: str) -> str:
    s = s.strip()
    if not TIME_RE.match(s):
        raise ValueError("Time must be HH:MM")
    hh, mm = s.split(":")
    hh_i, mm_i = int(hh), int(mm)
    if not (0 <= hh_i <= 23 and 0 <= mm_i <= 59):
        raise ValueError("Invalid time")
    return f"{hh_i:02d}:{mm_i:02d}"

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
        # If not configured, just return placeholder
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

def chore_is_due(chore_row) -> bool:
    """
    chore_row columns:
      (id, name, mode, assignee, interval_days, time, last_done, last_reminded, skip_until)
    due logic: due if last_done is NULL OR today >= last_done + interval_days
    """
    _, _, _, _, interval_days, _, last_done, _, skip_until = chore_row
    today = date.today()

    if skip_until and skip_until == today_str():
        return False

    if not last_done:
        # User said "YES" => new chores are due immediately
        return True

    last = datetime.strptime(last_done, "%Y-%m-%d").date()
    next_due = last + timedelta(days=int(interval_days))
    return today >= next_due

def format_chore(chore_row) -> str:
    chore_id, name, mode, assignee, interval_days, time_str, last_done, _, skip_until = chore_row
    if last_done:
        last = datetime.strptime(last_done, "%Y-%m-%d").date()
        next_due = (last + timedelta(days=int(interval_days))).strftime("%Y-%m-%d")
    else:
        next_due = "today"

    skip_note = " (skipped today)" if skip_until == today_str() else ""
    mode_note = "rotate" if mode == "rotate" else "fixed"
    return (f"{chore_id}) {name} — {assignee} ({mode_note}) — every {interval_days}d @ {time_str} — "
            f"next due: {next_due}{skip_note}")

def send_to_household(text: str):
    if CHAT_ID:
        bot.send_message(int(CHAT_ID), text)

# --------------------
# REMINDERS
# --------------------
def reminder_job():
    now = datetime.now().strftime("%H:%M")
    tday = today_str()

    cur.execute("SELECT id, name, mode, assignee, interval_days, time, last_done, last_reminded, skip_until FROM chores")
    chores = cur.fetchall()

    for c in chores:
        chore_id, name, mode, assignee, interval_days, time_str, last_done, last_reminded, skip_until = c

        if time_str != now:
            continue

        if last_reminded == tday:
            continue

        if not chore_is_due(c):
            continue

        send_to_household(f"🔔 Chore due: {name}\nAssigned to: {assignee}\nRepeat: every {interval_days} day(s)\nUse /done {chore_id} when finished.")
        cur.execute("UPDATE chores SET last_reminded=? WHERE id=?", (tday, chore_id))
        db.commit()

scheduler = BackgroundScheduler()
scheduler.add_job(reminder_job, "interval", minutes=1)
scheduler.start()

# --------------------
# COMMANDS
# --------------------
@bot.message_handler(commands=["start", "help"])
def cmd_help(message):
    bot.reply_to(
        message,
        "👋 Household Chore Bot (rolling schedule)\n\n"
        "Setup:\n"
        "/setpeople Alex Sam\n\n"
        "Chores:\n"
        "/add  (wizard)\n"
        "/cancel\n"
        "/list\n"
        "/today\n"
        "/done <id>\n"
        "/skip <id>\n"
        "/remove <id>\n\n"
        "Notes:\n"
        "- Repeats are rolling: next due = last_done + interval_days\n"
        "- New chores are due immediately (until first /done)\n"
    )

@bot.message_handler(commands=["setpeople"])
def cmd_setpeople(message):
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        bot.reply_to(message, "Usage: /setpeople Person1 Person2\nExample: /setpeople Alex Sam")
        return
    p1, p2 = parts[1].strip(), parts[2].strip()
    set_people(p1, p2)
    bot.reply_to(message, f"✅ Set household people:\n1) {p1}\n2) {p2}\nRotation will alternate between them.")

@bot.message_handler(commands=["cancel"])
def cmd_cancel(message):
    clear_session(message.chat.id, message.from_user.id)
    bot.reply_to(message, "❎ Cancelled.")

@bot.message_handler(commands=["add"])
def cmd_add(message):
    # Start wizard
    clear_session(message.chat.id, message.from_user.id)
    save_session(message.chat.id, message.from_user.id, "ASK_NAME", {})
    bot.reply_to(message, "🧹 Add chore (step 1/4)\nWhat is the chore name?\nExample: mopping")

@bot.message_handler(commands=["list"])
def cmd_list(message):
    cur.execute("SELECT id, name, mode, assignee, interval_days, time, last_done, last_reminded, skip_until FROM chores ORDER BY id")
    chores = cur.fetchall()
    if not chores:
        bot.reply_to(message, "No chores yet. Use /add to create one.")
        return
    text = "📋 All chores:\n" + "\n".join(format_chore(c) for c in chores)
    bot.reply_to(message, text)

@bot.message_handler(commands=["today"])
def cmd_today(message):
    cur.execute("SELECT id, name, mode, assignee, interval_days, time, last_done, last_reminded, skip_until FROM chores ORDER BY id")
    chores = cur.fetchall()
    due = [c for c in chores if chore_is_due(c)]
    if not due:
        bot.reply_to(message, "✅ No chores due right now.")
        return
    text = "📅 Due / overdue chores:\n" + "\n".join(format_chore(c) for c in due)
    bot.reply_to(message, text)

@bot.message_handler(commands=["done"])
def cmd_done(message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].isdigit():
        bot.reply_to(message, "Usage: /done <id>\nExample: /done 2")
        return
    chore_id = int(parts[1])
    tday = today_str()

    cur.execute("SELECT id, name, mode, assignee, interval_days, time, last_done, last_reminded, skip_until FROM chores WHERE id=?", (chore_id,))
    c = cur.fetchone()
    if not c:
        bot.reply_to(message, f"Chore #{chore_id} not found.")
        return

    # mark done today; clear skip & reminder marker so next due is computed from today
    cur.execute("UPDATE chores SET last_done=?, skip_until=NULL, last_reminded=NULL WHERE id=?", (tday, chore_id))
    db.commit()

    # rotation: assign next person immediately for next cycle
    if c[2] == "rotate":
        nxt = next_rotate_person(advance=True)
        cur.execute("UPDATE chores SET assignee=? WHERE id=?", (nxt, chore_id))
        db.commit()
        bot.reply_to(message, f"🎉 Done! Next time assigned to: {nxt}")
    else:
        bot.reply_to(message, "🎉 Marked done!")

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

# --------------------
# WIZARD (session-driven)
# --------------------
@bot.message_handler(func=lambda m: True, content_types=["text"])
def wizard_handler(message):
    # ignore commands here (handled above)
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
                bot.reply_to(message, f"Step 2/4: Who is it assigned to?\nReply with: {p1} / {p2} / rotate")
            else:
                bot.reply_to(message, "Step 2/4: Who is it assigned to?\nReply with a name (e.g., Alex) or 'rotate'.\nTip: set /setpeople first for rotation.")

        elif step == "ASK_ASSIGNEE":
            p1, p2, _ = get_people()
            if text.lower() == "rotate":
                data["mode"] = "rotate"
                # assign current person now, advance rotation for fairness
                data["assignee"] = next_rotate_person(advance=True)
            else:
                data["mode"] = "fixed"
                data["assignee"] = text  # allow any name; household names recommended

            save_session(message.chat.id, message.from_user.id, "ASK_INTERVAL", data)
            bot.reply_to(message, "Step 3/4: Repeat every how many days?\nReply with a number (e.g., 7)")

        elif step == "ASK_INTERVAL":
            if not text.isdigit():
                bot.reply_to(message, "Please reply with a number of days (e.g., 7).")
                return
            interval_days = int(text)
            if interval_days < 1 or interval_days > 365:
                bot.reply_to(message, "Please choose a sensible interval (1–365).")
                return
            data["interval_days"] = interval_days
            save_session(message.chat.id, message.from_user.id, "ASK_TIME", data)
            bot.reply_to(message, "Step 4/4: What time should I remind you?\nReply HH:MM (24h), e.g., 21:00")

        elif step == "ASK_TIME":
            time_str = parse_hhmm(text)
            data["time"] = time_str

            # Save chore
            cur.execute(
                "INSERT INTO chores (name, mode, assignee, interval_days, time, last_done, last_reminded, skip_until) "
                "VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL)",
                (data["name"], data["mode"], data["assignee"], int(data["interval_days"]), data["time"])
            )
            db.commit()

            clear_session(message.chat.id, message.from_user.id)

            bot.reply_to(
                message,
                "✅ Chore added!\n"
                f"Name: {data['name']}\n"
                f"Assigned to: {data['assignee']} ({data['mode']})\n"
                f"Repeat: every {data['interval_days']} day(s)\n"
                f"Reminder time: {data['time']}\n\n"
                "New chores are due immediately until you mark them done with /done <id>.\n"
                "Use /list to see the id."
            )

        else:
            clear_session(message.chat.id, message.from_user.id)
            bot.reply_to(message, "Session reset. Use /add to start again.")
    except Exception as e:
        # keep it user-friendly
        bot.reply_to(message, f"❌ Something went wrong: {e}\nType /cancel then /add to try again.")

# --------------------
# START
# --------------------
bot.infinity_polling()

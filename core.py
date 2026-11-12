"""Общая часть: настройки, база данных, Telegram API и логика салона."""
import html, os, re, secrets, sqlite3, threading
from contextlib import closing
from datetime import date, datetime, timedelta

import requests
from werkzeug.security import generate_password_hash

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

env = os.environ.get
BOT_TOKEN = env("BOT_TOKEN", "")
OWNER = str(env("OWNER_CHAT_ID", ""))
BOT_USERNAME = env("BOT_USERNAME", "")
SITE_URL = env("SITE_URL", "").rstrip("/")
WHATSAPP = re.sub(r"\D", "", env("WHATSAPP_NUMBER", ""))
TZ_OFFSET = int(env("TZ_OFFSET", "3"))  # часовой пояс салона, Москва = 3
DB_PATH = env("DB_PATH", "salon.db")

SLOTS = ["09:00", "11:00", "13:00", "15:00", "17:00", "19:00"]  # приём 2 часа, 9:00–21:00
MAX_DAYS = 60
HOURS_PER_VISIT = 2
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
esc = html.escape

SCHEMA = """
CREATE TABLE IF NOT EXISTS masters (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, telegram_id TEXT,
  active INTEGER NOT NULL DEFAULT 1, password_hash TEXT);
CREATE TABLE IF NOT EXISTS bookings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  master_id INTEGER NOT NULL REFERENCES masters(id),
  date TEXT NOT NULL, time TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
  tg_username TEXT, comment TEXT, created_at TEXT NOT NULL, token TEXT, client_chat_id TEXT,
  r24 INTEGER NOT NULL DEFAULT 0, r2 INTEGER NOT NULL DEFAULT 0,
  UNIQUE (master_id, date, time));
CREATE INDEX IF NOT EXISTS idx_bookings_date ON bookings(date);
CREATE INDEX IF NOT EXISTS idx_bookings_token ON bookings(token);
CREATE TABLE IF NOT EXISTS days_off (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  master_id INTEGER NOT NULL REFERENCES masters(id), date TEXT NOT NULL, UNIQUE (master_id, date));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS support_staff (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, telegram_id TEXT,
  active INTEGER NOT NULL DEFAULT 1, password_hash TEXT);
CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT NOT NULL, client_name TEXT, contact TEXT,
  tg_chat_id TEXT, token TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tickets_token ON tickets(token);
CREATE TABLE IF NOT EXISTS ticket_messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
  sender TEXT NOT NULL, author TEXT, text TEXT NOT NULL, created_at TEXT NOT NULL, staff_id INTEGER);
"""
# для баз, созданных старой версией (ошибка «duplicate column» игнорируется)
MIGRATIONS = [
    "ALTER TABLE masters ADD COLUMN password_hash TEXT",
    "ALTER TABLE bookings ADD COLUMN r24 INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE bookings ADD COLUMN r2 INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE ticket_messages ADD COLUMN staff_id INTEGER",
]


def db():
    c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    with closing(db()) as c:
        c.executescript(SCHEMA)
        for q in MIGRATIONS:
            try:
                c.execute(q)
            except sqlite3.OperationalError:
                pass


init_db()


# ---------- время и даты ----------
def now_local():
    return datetime.utcnow() + timedelta(hours=TZ_OFFSET)


def stamp():
    return now_local().strftime("%Y-%m-%d %H:%M")


def parse_date(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def valid_booking_date(s):
    d, today = parse_date(s), now_local().date()
    return d is not None and today <= d <= today + timedelta(days=MAX_DAYS)


def fmt_date(iso):
    d = date.fromisoformat(iso)
    return f"{d:%d.%m.%Y} ({WD[d.weekday()]})"


def plural(n, forms):
    n10, n100 = n % 10, n % 100
    return forms[0] if n10 == 1 and n100 != 11 else forms[1] if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else forms[2]


def period_range(period):
    today = now_local().date()
    mon = today - timedelta(days=today.weekday())
    if period == "prev":
        return mon - timedelta(days=7), mon - timedelta(days=1)
    if period == "month":
        first = today.replace(day=1)
        return first, (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return mon, mon + timedelta(days=6)


# ---------- настройки ----------
def get_setting(key, default=""):
    with closing(db()) as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(key, value):
    with closing(db()) as c:
        if value:
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        else:
            c.execute("DELETE FROM settings WHERE key=?", (key,))


def group_id():
    return get_setting("group_id") or env("GROUP_CHAT_ID", "")


# ---------- Telegram ----------
def api_raw(method, **params):
    if not BOT_TOKEN:
        return {}
    try:
        return requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params,
                             timeout=45 if method == "getUpdates" else 10).json()
    except (requests.RequestException, ValueError):
        return {}


def api(method, **params):
    r = api_raw(method, **params)
    return r.get("result") if r.get("ok") else None


def B(text, data=None, url=None):
    return {"text": text, **({"url": url} if url else {"callback_data": data})}


def send(chat, text, rows=None):
    if not chat:
        return None
    p = {"chat_id": chat, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if rows:
        p["reply_markup"] = {"inline_keyboard": rows}
    return api("sendMessage", **p)


def tg(chat, text, rows=None):
    """Отправка в фоне, чтобы не задерживать ответ сайту."""
    if chat and BOT_TOKEN:
        threading.Thread(target=send, args=(chat, text, rows), daemon=True).start()


def kick_from_group(uid):
    gid = group_id()
    if gid and uid and str(uid).lstrip("-").isdigit():
        api("banChatMember", chat_id=gid, user_id=int(uid))
        api("unbanChatMember", chat_id=gid, user_id=int(uid), only_if_banned=True)  # можно вернуть позже


def roles_of(uid):
    uid = str(uid)
    with closing(db()) as c:
        m = c.execute("SELECT id, name FROM masters WHERE active=1 AND telegram_id=?", (uid,)).fetchone()
        s = c.execute("SELECT id, name FROM support_staff WHERE active=1 AND telegram_id=?", (uid,)).fetchone()
    return {"owner": bool(OWNER) and uid == OWNER, "master": m, "support": s}


def gen_password(n=8):
    return "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(n))


# ---------- запись клиентов ----------
def free_slots(c, day):
    """Время свободно, если хотя бы у одного активного мастера (не в выходной) оно не занято."""
    masters = [r["id"] for r in c.execute(
        "SELECT id FROM masters WHERE active=1 AND id NOT IN (SELECT master_id FROM days_off WHERE date=?)", (day,))]
    booked = {(r["master_id"], r["time"]) for r in c.execute("SELECT master_id, time FROM bookings WHERE date=?", (day,))}
    now, out = now_local(), []
    for t in SLOTS:
        if day == now.date().isoformat():
            h, m = map(int, t.split(":"))
            if h * 60 + m <= now.hour * 60 + now.minute + 60:  # ближайший час уже не берём
                continue
        if any((mid, t) not in booked for mid in masters):
            out.append(t)
    return out


def _soon(day, t):
    delta = datetime.fromisoformat(f"{day}T{t}") - now_local()
    return int(delta <= timedelta(hours=24)), int(delta <= timedelta(hours=2))


def _reserve(c, day, t, name, phone, username, comment, token):
    free = free_slots(c, day)
    if not free:
        return 409, {"error": "На эту дату нет свободных мест.", "free": free}, None
    if t not in free:
        return 409, {"error": "Это время занято. Выберите другое.", "free": free}, None
    masters = c.execute(
        """SELECT m.id, m.name, m.telegram_id,
                  (SELECT COUNT(*) FROM bookings b WHERE b.master_id=m.id AND b.date=?) AS load
           FROM masters m WHERE m.active=1 AND m.id NOT IN (SELECT master_id FROM days_off WHERE date=?)
           ORDER BY load, m.id""", (day, day)).fetchall()
    r24, r2 = _soon(day, t)
    for m in masters:
        try:
            c.execute("""INSERT INTO bookings (master_id, date, time, name, phone, tg_username, comment, created_at, token, r24, r2)
                         VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                      (m["id"], day, t, name, phone, username or None, comment or None, stamp(), token, r24, r2))
        except sqlite3.IntegrityError:
            continue
        link = f"https://t.me/{BOT_USERNAME}?start={token}" if BOT_USERNAME else None
        return 200, {"ok": True, "master": m["name"], "date": day, "time": t, "tg_link": link}, m
    return 409, {"error": "Это время только что заняли. Выберите другое.", "free": free_slots(c, day)}, None


def book(d):
    name = str(d.get("name") or "").strip()[:80]
    phone = str(d.get("phone") or "").strip()[:30]
    username = str(d.get("tg_username") or "").strip().lstrip("@")
    comment = str(d.get("comment") or "").strip()[:300]
    if not name:
        return 400, {"error": "Введите имя."}
    if len(re.sub(r"\D", "", phone)) < 10:
        return 400, {"error": "Введите телефон полностью, с кодом."}
    if username and not re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
        return 400, {"error": "Telegram-юзернейм указан неверно."}
    day, t = d.get("date"), d.get("time")
    if not valid_booking_date(day) or t not in SLOTS:
        return 400, {"error": "Неверная дата или время."}
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")  # два клиента не займут одно окно
        try:
            code, payload, m = _reserve(c, day, t, name, phone, username, comment, secrets.token_hex(16))
            c.execute("COMMIT" if code == 200 else "ROLLBACK")
        except Exception:
            c.execute("ROLLBACK")
            raise
    if m:
        text = (f"💅 <b>Новая запись</b>\n\n📅 {fmt_date(day)}\n🕒 {t}\n👩‍🎨 Мастер: {esc(m['name'])}\n"
                f"👤 Клиент: {esc(name)}\n📞 {esc(phone)}\n"
                + (f"✈️ Telegram: @{username}\n" if username else "") + (f"💬 {esc(comment)}" if comment else ""))
        for chat in {OWNER, str(m["telegram_id"] or "")} - {""}:
            tg(chat, text)
    return code, payload


def move_booking(bid, master_id=None, day=None, t=None):
    """Смена мастера и/или переноса записи. Возвращает (ok, сообщение)."""
    with closing(db()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            res = _move(c, bid, master_id, day, t)
            c.execute("COMMIT" if res[0] else "ROLLBACK")
        except Exception:
            c.execute("ROLLBACK")
            raise
    ok, msg, i = res
    if not ok:
        return False, msg
    b, old, m = i["b"], i["old"], i["m"]
    info = f"📅 {fmt_date(i['day'])}\n🕒 {i['t']}\n👩‍🎨 Мастер: {esc(m['name'])}"
    if b["client_chat_id"]:
        tg(b["client_chat_id"], f"🔄 <b>Ваша запись изменена</b>\n\n{info}\n\nЖдём вас! 💅")
    who = f"👤 {esc(b['name'])}, 📞 {esc(b['phone'])}"
    if m["id"] != b["master_id"]:
        tg(old["telegram_id"], f"↪️ <b>Запись передана другому мастеру</b>\n\n{who}\n"
                              f"Было: {fmt_date(b['date'])}, {b['time']}\nТеперь: {esc(m['name'])}")
        tg(m["telegram_id"], f"📥 <b>Вам передана запись</b>\n\n{info}\n{who}")
    elif (i["day"], i["t"]) != (b["date"], b["time"]):
        tg(m["telegram_id"], f"🔄 <b>Запись перенесена</b>\n\n{info}\n{who}")
    return True, "ok"


def _move(c, bid, master_id, day, t):
    b = c.execute("SELECT * FROM bookings WHERE id=?", (bid,)).fetchone()
    if not b:
        return False, "Запись не найдена.", None
    try:
        mid = int(master_id or b["master_id"])
    except (TypeError, ValueError):
        return False, "Мастер не найден.", None
    day, t = day or b["date"], t or b["time"]
    if parse_date(day) is None or parse_date(day) < now_local().date():
        return False, "Неверная дата.", None
    if t not in SLOTS:
        return False, "Время должно быть одним из: " + ", ".join(SLOTS), None
    m = c.execute("SELECT id, name, telegram_id FROM masters WHERE id=? AND active=1", (mid,)).fetchone()
    if not m:
        return False, "Мастер не найден.", None
    if c.execute("SELECT 1 FROM days_off WHERE master_id=? AND date=?", (mid, day)).fetchone():
        return False, f"У мастера {m['name']} в этот день выходной.", None
    if c.execute("SELECT 1 FROM bookings WHERE master_id=? AND date=? AND time=? AND id<>?", (mid, day, t, bid)).fetchone():
        return False, f"У мастера {m['name']} это время уже занято.", None
    r24, r2 = _soon(day, t)
    c.execute("UPDATE bookings SET master_id=?, date=?, time=?, r24=?, r2=? WHERE id=?", (mid, day, t, r24, r2, bid))
    old = c.execute("SELECT name, telegram_id FROM masters WHERE id=?", (b["master_id"],)).fetchone()
    return True, "ok", {"b": dict(b), "old": dict(old), "m": dict(m), "day": day, "t": t}


def cancel_booking(bid, skip_chat=None):
    """Отмена: уведомляются мастер, владелец и клиент (если подключил бота)."""
    with closing(db()) as c:
        b = c.execute("""SELECT b.*, m.name AS master, m.telegram_id AS mtg FROM bookings b
                         JOIN masters m ON m.id=b.master_id WHERE b.id=?""", (bid,)).fetchone()
        if not b:
            return False
        c.execute("DELETE FROM bookings WHERE id=?", (bid,))
    text = (f"🗑 <b>Запись отменена</b>\n\n📅 {fmt_date(b['date'])}\n🕒 {b['time']}\n"
            f"👩‍🎨 Мастер: {esc(b['master'])}\n👤 Клиент: {esc(b['name'])}\n📞 {esc(b['phone'])}")
    for chat in {OWNER, str(b["mtg"] or "")} - {"", str(skip_chat or "")}:
        tg(chat, text)
    if b["client_chat_id"]:
        tg(b["client_chat_id"], f"❌ <b>Запись отменена</b>\n\nК сожалению, ваша запись на {fmt_date(b['date'])} "
                                f"в {b['time']} отменена. Позвоните нам, чтобы выбрать другое время. 📞")
    return True


# ---------- мастера и поддержка ----------
def add_person(table, name, telegram_id):
    pw = gen_password()
    with closing(db()) as c:
        pid = c.execute(f"INSERT INTO {table} (name, telegram_id, password_hash) VALUES (?,?,?)",
                        (name, telegram_id or None, generate_password_hash(pw))).lastrowid
    return pid, pw


def add_master(name, telegram_id):
    return add_person("masters", name, telegram_id)


def add_support(name, telegram_id):
    return add_person("support_staff", name, telegram_id)


def new_password(table, pid):
    assert table in ("masters", "support_staff")
    pw = gen_password()
    with closing(db()) as c:
        n = c.execute(f"UPDATE {table} SET password_hash=? WHERE id=? AND active=1", (generate_password_hash(pw), pid)).rowcount
    return pw if n else None


def deactivate_master(mid):
    """Убирает мастера, выгоняет из общего чата. Возвращает (имя, число будущих записей)."""
    with closing(db()) as c:
        m = c.execute("SELECT name, telegram_id FROM masters WHERE id=? AND active=1", (mid,)).fetchone()
        if not m:
            return None
        c.execute("UPDATE masters SET active=0 WHERE id=?", (mid,))
        n = c.execute("SELECT COUNT(*) FROM bookings WHERE master_id=? AND date>=?", (mid, now_local().date().isoformat())).fetchone()[0]
    if m["telegram_id"]:
        kick_from_group(m["telegram_id"])
        tg(m["telegram_id"], "👋 Ваш доступ мастера в системе салона закрыт, вы удалены из общего чата.")
    return m["name"], n


def deactivate_support(sid):
    """Убирает сотрудника поддержки и выгоняет его из общего чата."""
    with closing(db()) as c:
        s = c.execute("SELECT telegram_id FROM support_staff WHERE id=? AND active=1", (sid,)).fetchone()
        if not s:
            return False
        c.execute("UPDATE support_staff SET active=0 WHERE id=?", (sid,))
    if s["telegram_id"]:
        kick_from_group(s["telegram_id"])
        tg(s["telegram_id"], "👋 Ваш доступ в службе поддержки салона закрыт, вы удалены из общего чата.")
    return True


# ---------- статистика ----------
MEDALS = ["🥇", "🥈", "🥉"]


def hours_stats(start, end):
    cap = min(end, now_local().date()).isoformat()
    with closing(db()) as c:
        rows = c.execute("""SELECT m.name, COUNT(b.id) AS n FROM masters m
                            LEFT JOIN bookings b ON b.master_id=m.id AND b.date>=? AND b.date<=?
                            WHERE m.active=1 OR b.id IS NOT NULL GROUP BY m.id ORDER BY n DESC, m.name""",
                         (start.isoformat(), cap)).fetchall()
    return [{"name": r["name"], "visits": r["n"], "hours": r["n"] * HOURS_PER_VISIT} for r in rows]


def hours_for(mid, start, end):
    cap = min(end, now_local().date()).isoformat()
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) FROM bookings WHERE master_id=? AND date>=? AND date<=?",
                      (mid, start.isoformat(), cap)).fetchone()[0]
    return n * HOURS_PER_VISIT, n


def stats_text(title, start, end):
    rows = hours_stats(start, end)
    head = f"📊 <b>{title}</b>\n🗓 {start:%d.%m} – {end:%d.%m}\n\n"
    if not rows:
        return head + "Пока нет мастеров."
    lines = []
    for i, r in enumerate(rows):
        icon = MEDALS[i] if i < 3 and r["visits"] else "▫️"
        lines.append(f"{icon} {esc(r['name'])} — <b>{r['hours']} ч</b> ({r['visits']} {plural(r['visits'], ('приём', 'приёма', 'приёмов'))})")
    return head + "\n".join(lines) + f"\n\n⏱ Всего: <b>{sum(r['hours'] for r in rows)} ч</b>"


def support_counts(sid, start, end):
    """(вопросов обработано, ответов) сотрудником за период. Вопрос обработан, если сотрудник на него ответил."""
    cap = min(end, now_local().date()).isoformat()
    with closing(db()) as c:
        r = c.execute("""SELECT COUNT(DISTINCT ticket_id), COUNT(*) FROM ticket_messages
                         WHERE staff_id=? AND sender='staff' AND substr(created_at,1,10) BETWEEN ? AND ?""",
                      (sid, start.isoformat(), cap)).fetchone()
    return r[0], r[1]


def support_stats_text(start, end):
    cap = min(end, now_local().date()).isoformat()
    with closing(db()) as c:
        rows = c.execute("""SELECT s.name, COUNT(DISTINCT m.ticket_id) AS t, COUNT(m.id) AS r FROM support_staff s
                            LEFT JOIN ticket_messages m ON m.staff_id=s.id AND m.sender='staff'
                                 AND substr(m.created_at,1,10) BETWEEN ? AND ?
                            WHERE s.active=1 OR m.id IS NOT NULL GROUP BY s.id ORDER BY t DESC, s.name""",
                         (start.isoformat(), cap)).fetchall()
    if not rows:
        return ""
    lines = [f"{MEDALS[i] if i < 3 and r['t'] else '▫️'} {esc(r['name'])} — <b>{r['t']} {plural(r['t'], ('вопрос', 'вопроса', 'вопросов'))}</b> "
             f"({r['r']} {plural(r['r'], ('ответ', 'ответа', 'ответов'))})" for i, r in enumerate(rows)]
    return "🛟 <b>Служба поддержки: обработано вопросов</b>\n\n" + "\n".join(lines)


def support_tail(rng):
    t = support_stats_text(*rng)
    return f"\n\n{t}" if t else ""


# ---------- вопросы клиентов ----------
def staff_chats():
    with closing(db()) as c:
        ids = [r["telegram_id"] for r in c.execute("SELECT telegram_id FROM support_staff WHERE active=1 AND telegram_id IS NOT NULL")]
    return {OWNER, *map(str, ids)} - {""}


def notify_staff(tid, name, channel, text, new=True):
    icon = "🌐 сайт" if channel == "site" else "✈️ Telegram"
    head = f"🆕 <b>Новый вопрос #{tid}</b>" if new else f"💬 <b>Новое сообщение в вопросе #{tid}</b>"
    for chat in staff_chats():
        tg(chat, f"{head} · {icon}\n👤 {esc(name or 'Клиент')}\n\n💬 {esc(text[:400])}", [[B("💬 Открыть и ответить", f"tk:{tid}")]])


def ticket_create(channel, name, contact, tg_chat, text):
    token, now = secrets.token_hex(16), stamp()
    with closing(db()) as c:
        tid = c.execute("""INSERT INTO tickets (channel, client_name, contact, tg_chat_id, token, created_at, updated_at)
                           VALUES (?,?,?,?,?,?,?)""", (channel, name, contact, tg_chat, token, now, now)).lastrowid
        c.execute("INSERT INTO ticket_messages (ticket_id, sender, author, text, created_at) VALUES (?,?,?,?,?)",
                  (tid, "client", name, text, now))
    notify_staff(tid, name, channel, text)
    return tid, token


def ticket_client_message(tid, text):
    with closing(db()) as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        c.execute("INSERT INTO ticket_messages (ticket_id, sender, author, text, created_at) VALUES (?,?,?,?,?)",
                  (tid, "client", t["client_name"], text, stamp()))
        c.execute("UPDATE tickets SET status='open', updated_at=? WHERE id=?", (stamp(), tid))
    notify_staff(tid, t["client_name"], t["channel"], text, new=False)


def ticket_staff_reply(tid, author, text, staff_id=None):
    with closing(db()) as c:
        t = c.execute("SELECT * FROM tickets WHERE id=?", (tid,)).fetchone()
        if not t:
            return False
        c.execute("INSERT INTO ticket_messages (ticket_id, sender, author, text, created_at, staff_id) VALUES (?,?,?,?,?,?)",
                  (tid, "staff", author, text, stamp(), staff_id))
        c.execute("UPDATE tickets SET updated_at=? WHERE id=?", (stamp(), tid))
    if t["channel"] == "telegram":
        tg(t["tg_chat_id"], f"🛟 <b>Ответ поддержки</b>\n\n{esc(text)}\n\n<i>Чтобы продолжить, просто напишите сообщение.</i>")
    return True


def ticket_close(tid):
    with closing(db()) as c:
        c.execute("UPDATE tickets SET status='closed', updated_at=? WHERE id=?", (stamp(), tid))


def ticket_messages(tid, limit=200):
    with closing(db()) as c:
        rows = c.execute("SELECT sender, author, text, created_at FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT ?",
                         (tid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]

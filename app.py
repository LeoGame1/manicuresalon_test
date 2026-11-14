"""Запись на маникюр: сайт, админ-панель, кабинеты мастеров и поддержки + Telegram-бот."""
import hmac, os, re, secrets, time
from contextlib import closing
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, URLSafeTimedSerializer
from werkzeug.security import check_password_hash

import core
from core import *  # noqa: F401,F403
from bot import start_bot

ADMIN_PASSWORD = env("ADMIN_PASSWORD", "")
SECRET_KEY = env("SECRET_KEY") or secrets.token_hex(32)  # без SECRET_KEY вход сбрасывается при перезапуске

app = Flask(__name__, static_folder=None)
signer = URLSafeTimedSerializer(SECRET_KEY)


def err(msg, code=400, **extra):
    return jsonify(error=msg, **extra), code


def body():
    return request.get_json(silent=True) or {}


# ---------- страницы ----------
for _route, _file in (("/", "index.html"), ("/admin", "admin.html"), ("/master", "master.html"), ("/support", "support.html")):
    app.add_url_rule(_route, _file, lambda f=_file: send_from_directory("static", f))


@app.get("/static/<path:p>")
def static_files(p):
    return send_from_directory("static", p)


# ---------- вход и права ----------
_fails = {}  # ip -> время неудачных попыток


def blocked(ip):
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 900]
    return len(_fails[ip]) >= 5


def token_for(role, pid=None):
    return signer.dumps({"role": role, "id": pid}, salt="auth")


def need(*roles):
    """Доступ: перечисленные роли и всегда владелец."""
    def deco(f):
        @wraps(f)
        def wrapper(*a, **kw):
            try:
                p = signer.loads(request.headers.get("Authorization", "").replace("Bearer ", ""), max_age=7 * 86400, salt="auth")
            except BadSignature:
                return err("Нужен вход.", 401)
            if p["role"] != "owner" and p["role"] not in roles:
                return err("Нет доступа.", 403)
            g.user = p
            return f(*a, **kw)
        return wrapper
    return deco


@app.post("/api/admin/login")
def admin_login():
    if blocked(request.remote_addr):
        return err("Слишком много попыток. Подождите 15 минут.", 429)
    pw = str(body().get("password") or "")
    if ADMIN_PASSWORD and hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
        return jsonify(token=token_for("owner"))
    _fails[request.remote_addr].append(time.time())
    return err("Неверный пароль.", 401)


@app.post("/api/staff/login")
def staff_login():
    if blocked(request.remote_addr):
        return err("Слишком много попыток. Подождите 15 минут.", 429)
    d = body()
    role = d.get("role")
    table = {"master": "masters", "support": "support_staff"}.get(role)
    name, pw = str(d.get("name") or "").strip().lower(), str(d.get("password") or "")
    if table:
        with closing(db()) as c:
            for r in c.execute(f"SELECT id, name, password_hash FROM {table} WHERE active=1"):
                if r["name"].strip().lower() == name and r["password_hash"] and check_password_hash(r["password_hash"], pw):
                    return jsonify(token=token_for(role, r["id"]), name=r["name"])
    _fails[request.remote_addr].append(time.time())
    return err("Неверное имя или пароль.", 401)


# ---------- публичное ----------
@app.get("/api/config")
def config():
    return jsonify(tg=f"https://t.me/{BOT_USERNAME}?start=ask" if BOT_USERNAME else None,
                   whatsapp=f"https://wa.me/{WHATSAPP}" if WHATSAPP else None)


@app.get("/api/slots")
def api_slots():
    day = request.args.get("date")
    if not valid_booking_date(day):
        return err("Неверная дата.")
    with closing(db()) as c:
        return jsonify(free=free_slots(c, day))


@app.post("/api/book")
def api_book():
    code, payload = book(body())
    return jsonify(payload), code


_ticket_hits = {}


@app.post("/api/ticket")
def ticket_new():
    ip, now = request.remote_addr, time.time()
    _ticket_hits[ip] = [t for t in _ticket_hits.get(ip, []) if now - t < 3600]
    if len(_ticket_hits[ip]) >= 10:
        return err("Слишком много вопросов. Попробуйте позже.", 429)
    d = body()
    name, contact, text = str(d.get("name") or "").strip()[:80], str(d.get("contact") or "").strip()[:80], str(d.get("text") or "").strip()[:1000]
    if not text:
        return err("Напишите ваш вопрос.")
    _ticket_hits[ip].append(now)
    _, token = ticket_create("site", name or "Клиент с сайта", contact, None, text)
    return jsonify(token=token)


def ticket_by_token(token):
    with closing(db()) as c:
        return c.execute("SELECT id, status FROM tickets WHERE token=? AND channel='site'", (token,)).fetchone()


@app.get("/api/ticket/<token>")
def ticket_read(token):
    t = ticket_by_token(token)
    if not t:
        return err("Не найдено.", 404)
    return jsonify(status=t["status"], messages=ticket_messages(t["id"]))


@app.post("/api/ticket/<token>")
def ticket_write(token):
    t = ticket_by_token(token)
    text = str(body().get("text") or "").strip()[:1000]
    if not t:
        return err("Не найдено.", 404)
    if not text:
        return err("Напишите сообщение.")
    ticket_client_message(t["id"], text)
    return jsonify(ok=True)


# ---------- мастер ----------
@app.get("/api/master/data")
@need("master")
def master_data():
    mid = g.user["id"]
    if not mid:
        return err("Это кабинет мастера.", 403)
    with closing(db()) as c:
        me = c.execute("SELECT name FROM masters WHERE id=? AND active=1", (mid,)).fetchone()
        if not me:
            return err("Нужен вход.", 401)
        rows = c.execute("SELECT date, time, name, phone, tg_username, comment FROM bookings WHERE master_id=? AND date>=? ORDER BY date, time",
                         (mid, now_local().date().isoformat())).fetchall()
    return jsonify(name=me["name"], bookings=[dict(r) for r in rows], team=dict(zip(("masters", "support"), team())), chat_link=get_setting("chat_link") or None,
                   hours={p: hours_for(mid, *period_range(p))[0] for p in ("week", "prev", "month")},
                   days_off=(days := [r["date"] for r in days_off_rows("master", mid)]), off_today=now_local().date().isoformat() in days)


# ---------- поддержка (сотрудник или владелец) ----------
@app.get("/api/support/tickets")
@need("support")
def support_list():
    status = "closed" if request.args.get("status") == "closed" else "open"
    with closing(db()) as c:
        rows = c.execute("""SELECT t.id, t.channel, t.client_name, t.contact, t.status, t.updated_at,
                              (SELECT sender FROM ticket_messages WHERE ticket_id=t.id ORDER BY id DESC LIMIT 1) AS last_sender,
                              (SELECT text FROM ticket_messages WHERE ticket_id=t.id ORDER BY id DESC LIMIT 1) AS last_text
                            FROM tickets t WHERE t.status=? ORDER BY t.updated_at DESC LIMIT 100""", (status,)).fetchall()
    return jsonify(tickets=[dict(r) for r in rows])


@app.get("/api/support/tickets/<int:tid>")
@need("support")
def support_view(tid):
    with closing(db()) as c:
        t = c.execute("SELECT id, channel, client_name, contact, status FROM tickets WHERE id=?", (tid,)).fetchone()
    if not t:
        return err("Не найдено.", 404)
    return jsonify(ticket=dict(t), messages=ticket_messages(tid))


@app.post("/api/support/tickets/<int:tid>/reply")
@need("support")
def support_reply(tid):
    text = str(body().get("text") or "").strip()[:1000]
    if not text:
        return err("Напишите ответ.")
    author = "Владелец"
    if g.user["role"] == "support":
        with closing(db()) as c:
            r = c.execute("SELECT name FROM support_staff WHERE id=? AND active=1", (g.user["id"],)).fetchone()
        if not r:
            return err("Нужен вход.", 401)
        author = r["name"]
    return jsonify(ok=True) if ticket_staff_reply(tid, author, text, g.user["id"] if g.user["role"] == "support" else None) else err("Не найдено.", 404)


@app.post("/api/support/tickets/<int:tid>/close")
@need("support")
def support_close(tid):
    ticket_close(tid)
    return jsonify(ok=True)


@app.get("/api/support/me")
@need("support")
def support_me():
    """Личный кабинет сотрудника: статистика, выходные, ссылка на общий чат."""
    sid = g.user["id"]
    if not sid:
        return err("Это кабинет сотрудника поддержки.", 403)
    with closing(db()) as c:
        me = c.execute("SELECT name FROM support_staff WHERE id=? AND active=1", (sid,)).fetchone()
    if not me:
        return err("Нужен вход.", 401)
    days = [r["date"] for r in days_off_rows("support", sid)]
    stats = {p: dict(zip(("tickets", "replies"), support_counts(sid, *period_range(p)))) for p in ("week", "prev", "month")}
    return jsonify(name=me["name"], stats=stats, chat_link=get_setting("chat_link") or None,
                   days_off=days, off_today=now_local().date().isoformat() in days,
                   team=dict(zip(("masters", "support"), team())))


# ---------- админ (владелец) ----------
@app.get("/api/admin/masters")
@need()
def masters_list():
    with closing(db()) as c:
        return jsonify(masters=[dict(r) for r in c.execute("SELECT id, name, telegram_id FROM masters WHERE active=1 ORDER BY id")])


def person_add(kind):
    d = body()
    name, tgid = str(d.get("name") or "").strip()[:80], str(d.get("telegram_id") or "").strip()
    if not name:
        return err("Введите имя.")
    if tgid and not re.fullmatch(r"\d{5,15}", tgid):
        return err("Telegram ID — это число.")
    pid, pw = (add_master if kind == "m" else add_support)(name, tgid)
    if tgid:
        tg(tgid, "💅 <b>Вас добавили в систему салона!</b>\nНажмите /start, чтобы открыть меню." if kind == "m"
           else "🛟 <b>Вас добавили в службу поддержки салона!</b>\nНажмите /start, чтобы открыть меню.")
    return jsonify(ok=True, id=pid, password=pw)


@app.post("/api/admin/masters")
@need()
def masters_add():
    return person_add("m")


@app.delete("/api/admin/masters/<int:mid>")
@need()
def masters_remove(mid):
    r = deactivate_master(mid)
    return jsonify(ok=True, future_bookings=r[1] if r else 0)


@app.post("/api/admin/masters/<int:mid>/password")
@need()
def masters_password(mid):
    pw = new_password("masters", mid)
    return jsonify(password=pw) if pw else err("Не найден.", 404)


@app.get("/api/admin/support-staff")
@need()
def support_staff_list():
    with closing(db()) as c:
        return jsonify(staff=[dict(r) for r in c.execute("SELECT id, name, telegram_id FROM support_staff WHERE active=1 ORDER BY id")])


@app.post("/api/admin/support-staff")
@need()
def support_staff_add():
    return person_add("s")


@app.delete("/api/admin/support-staff/<int:sid>")
@need()
def support_staff_remove(sid):
    deactivate_support(sid)
    return jsonify(ok=True)


@app.post("/api/admin/support-staff/<int:sid>/password")
@need()
def support_staff_password(sid):
    pw = new_password("support_staff", sid)
    return jsonify(password=pw) if pw else err("Не найден.", 404)


@app.get("/api/admin/chat")
@need()
def chat_get():
    return jsonify(link=get_setting("chat_link"), group_connected=bool(group_id()))


@app.post("/api/admin/chat")
@need()
def chat_set():
    link = str(body().get("link") or "").strip()
    if link and not re.match(r"https?://\S+$", link):
        return err("Ссылка должна начинаться с https://")
    set_setting("chat_link", link)  # пустая ссылка = «временно недоступна» для мастеров
    return jsonify(ok=True)


@app.get("/api/admin/stats")
@need()
def stats():
    start, end = period_range(request.args.get("period", "week"))
    return jsonify(start=start.isoformat(), end=end.isoformat(), rows=hours_stats(start, end))


@app.get("/api/admin/days-off")
@need()
def days_off_list():
    return jsonify(days_off=days_off_rows())


@app.post("/api/admin/days-off")
@need()
def days_off_add():
    d = body()
    kind = d.get("kind") or "master"
    try:
        pid = int(d.get("person_id") or d.get("master_id"))
    except (TypeError, ValueError):
        return err("Выберите сотрудника.")
    if kind not in DO:
        return err("Выберите сотрудника.")
    ok, msg, n = add_days_off(kind, pid, d.get("date"), d.get("date_to") or None)
    return jsonify(ok=True, conflicts=n) if ok else err(msg)  # записи мастера на эти дни не удаляются автоматически


@app.delete("/api/admin/days-off/<kind>/<int:did>")
@need()
def days_off_remove(kind, did):
    if kind not in DO:
        return err("Не найдено.", 404)
    day_off_remove(kind, did)
    return jsonify(ok=True)


@app.get("/api/admin/bookings")
@need()
def bookings_list():
    with closing(db()) as c:
        rows = c.execute("""SELECT b.id, b.date, b.time, b.name, b.phone, b.tg_username, b.comment, b.master_id,
                                   b.client_chat_id IS NOT NULL AS notified, m.name AS master
                            FROM bookings b JOIN masters m ON m.id=b.master_id
                            WHERE b.date >= ? ORDER BY b.date, b.time, m.name""", (now_local().date().isoformat(),))
        return jsonify(bookings=[dict(r) for r in rows], slots=SLOTS)


@app.patch("/api/admin/bookings/<int:bid>")
@need()
def booking_edit(bid):
    d = body()
    ok, msg = move_booking(bid, d.get("master_id"), d.get("date"), d.get("time"))
    return jsonify(ok=True) if ok else err(msg, 409)


@app.delete("/api/admin/bookings/<int:bid>")
@need()
def booking_cancel(bid):
    cancel_booking(bid)
    return jsonify(ok=True)


@app.errorhandler(Exception)
def on_error(e):
    code = getattr(e, "code", 500)
    return err("Не найдено." if code == 404 else "Ошибка сервера.", code if isinstance(code, int) else 500)


if os.environ.get("BOT_POLLING", "1") == "1":
    start_bot()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
